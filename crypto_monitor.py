#!/usr/bin/env python3
"""
虚拟币监控 + AI 分析助手
数据源: 火币 (Huobi) — 国内可访问
生成走势图 + AI分析文字
"""

import os
import io
import json
import time
import requests
import numpy as np
from datetime import datetime, timezone, timedelta

# ==================== 导入画图库 (容错) ====================
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import FancyBboxPatch
    
    CHART_ENABLED = True
except ImportError:
    CHART_ENABLED = False

# ==================== 配置 ====================
from wechat_config import AI_API_KEY

HUOBI_BASE = "https://api.huobi.pro"

# (显示名, 火币交易对)
DEFAULT_WATCHLIST = [
    ("BTC",  "btcusdt"),
    ("ETH",  "ethusdt"),
    ("SOL",  "solusdt"),
    ("BNB",  "bnbusdt"),
]

def load_watchlist():
    """从配置文件加载监控列表，如果没有则用默认值"""
    cfg_path = os.path.expanduser("~/.hermes/user_watchlist.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                data = json.load(f)
            return [(k, v) for k, v in data.items()]
        except Exception:
            pass
    return list(DEFAULT_WATCHLIST)

WATCHLIST = load_watchlist()

CHART_DIR = os.path.expanduser("~/.hermes/crypto_charts")
os.makedirs(CHART_DIR, exist_ok=True)

LOG_FILE = os.path.expanduser("~/.hermes/logs/crypto_api.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(f"{msg}\n")


# Gate.io API (京东云可访问，免费无限制)
GATE_BASE = "https://api.gateio.ws/api/v4"

# Gate.io 交易对映射
SYMBOL_TO_GATE = {
    "btcusdt": "BTC_USDT",
    "ethusdt": "ETH_USDT",
    "solusdt": "SOL_USDT",
    "bnbusdt": "BNB_USDT",
    "ltcusdt": "LTC_USDT",
    "dogeusdt": "DOGE_USDT",
    "xrpusdt": "XRP_USDT",
    "adausdt": "ADA_USDT",
    "dotusdt": "DOT_USDT",
    "linkusdt": "LINK_USDT",
}


# ==================== 数据获取 ====================

def huobi_get(path, params=None):
    """请求火币 API (带重试)"""
    url = f"{HUOBI_BASE}{path}"
    last_error = None
    
    for attempt in range(2):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "ok":
                raise Exception(f"Huobi API error: {data}")
            return data
        except requests.Timeout:
            last_error = f"超时(第{attempt+1}次)"
            log(f"⏳ 火币请求超时，重试 {attempt+1}/3")
            time.sleep(2)
        except Exception as e:
            last_error = str(e)
            if attempt < 2:
                time.sleep(1)
    
    # 火币失败，使用 CryptoCompare
    log(f"火币失败，切换到 CryptoCompare")
    return None


def gate_get_ticker(symbol):
    """从 Gate.io 获取实时价格和K线（国内可访问，免费无限制）"""
    gate_pair = SYMBOL_TO_GATE.get(symbol, symbol.replace("usdt", "_USDT").upper())
    try:
        # 获取实时 ticker
        resp = requests.get(
            f"{GATE_BASE}/spot/tickers",
            params={"currency_pair": gate_pair},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        if not data or len(data) == 0:
            raise Exception(f"Gate.io no ticker data")
        ticker_data = data[0]
        price = float(ticker_data["last"])
        change_pct = float(ticker_data["change_percentage"])
        high_24h = float(ticker_data["high_24h"])
        low_24h = float(ticker_data["low_24h"])
        vol = float(ticker_data["base_volume"])
        vol_usdt = float(ticker_data["quote_volume"])
        
        # 获取K线数据（用于RSI、布林带计算）
        resp2 = requests.get(
            f"{GATE_BASE}/spot/candlesticks",
            params={"currency_pair": gate_pair, "interval": "1h", "limit": 100},
            timeout=10
        )
        resp2.raise_for_status()
        candles = resp2.json()
        
        klines = []
        for c in candles:
            klines.append({
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "open": float(c[1]),
                "volumefrom": float(c[6]),
                "volumeto": float(c[1]) * float(c[6]),  # approximate
            })
        
        ticker = {
            "close": price,
            "open": float(ticker_data.get("open", 0) or price),
            "high": high_24h,
            "low": low_24h,
            "amount": vol,
            "vol": vol_usdt,
        }
        
        return ticker, klines, change_pct
        
    except Exception as e:
        raise Exception(f"Gate.io 获取失败: {e}")


def get_ticker(symbol):
    data = huobi_get("/market/detail/merged", {"symbol": symbol})
    t = data["tick"]
    return {
        "open": t["open"], "close": t["close"],
        "high": t["high"], "low": t["low"],
        "amount": t["amount"], "vol": t["vol"],
        "bid": t["bid"][0], "ask": t["ask"][0],
    }


def get_klines(symbol, period="60min", size=100):
    data = huobi_get("/market/history/kline", {
        "symbol": symbol, "period": period, "size": size,
    })
    return data["data"]


# ==================== 技术指标 ====================

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period+1):])
    gains = deltas[deltas > 0].sum() if len(deltas[deltas > 0]) > 0 else 0
    losses = -deltas[deltas < 0].sum() if len(deltas[deltas < 0]) > 0 else 0
    if losses == 0:
        return 100.0
    return round(100 - (100 / (1 + gains / losses)), 1)


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = alpha * c + (1 - alpha) * ema
    return round(ema, 2)


def calc_ema_series(closes, period):
    """返回完整的EMA序列用于画图"""
    if len(closes) < period:
        return []
    alpha = 2 / (period + 1)
    ema = closes[0]
    result = []
    for i, c in enumerate(closes):
        if i == 0:
            result.append(c)
            continue
        ema = alpha * c + (1 - alpha) * ema
        result.append(ema)
    return result


def calc_macd(closes):
    if len(closes) < 26:
        return None, None, None
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return None, None, None
    macd = round(ema12 - ema26, 2)
    signal = round(np.mean([macd]), 2)
    hist = round(macd - signal, 2)
    return macd, signal, hist


def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return round(np.mean(closes[-period:]), 2)


def calc_bollinger(closes, period=20, std_dev=2):
    if len(closes) < period:
        return None, None, None
    sma = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return round(sma + std_dev * std, 2), round(sma, 2), round(sma - std_dev * std, 2)


def calc_volume_ratio(volumes, period=20):
    if len(volumes) < period + 1:
        return None
    avg_vol = np.mean(volumes[-(period+1):-1])
    if avg_vol == 0:
        return None
    return round(volumes[-1] / avg_vol, 2)


# ==================== 画图 ====================

def draw_chart(name, symbol, klines, data):
    """生成走势图，返回图片路径"""
    if not CHART_ENABLED:
        return None

    # 解析K线 (兼容火币和CryptoCompare格式)
    def k_get(k, field):
        """兼容获取K线字段"""
        return k.get(field, k.get({"close":"close","high":"high","low":"low","open":"open","id":"id","amount":"volumefrom"}.get(field, field), 0))
    
    times = [datetime.fromtimestamp(k.get("id", k.get("time", 0)), tz=timezone(timedelta(hours=8))) for k in klines]
    closes = [k.get("close", 0) for k in klines]
    highs = [k.get("high", 0) for k in klines]
    lows = [k.get("low", 0) for k in klines]
    opens = [k.get("open", 0) for k in klines]
    vols = [k.get("volumefrom", k.get("amount", 0)) for k in klines]

    # 布林带
    bb_u, bb_m, bb_l = calc_bollinger(np.array(closes))
    ema20_series = calc_ema_series(np.array(closes), 20)
    ema50_series = calc_ema_series(np.array(closes), 50)

    # 创建图标 - 深色主题
    plt.style.use("dark_background")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6),
                                     gridspec_kw={"height_ratios": [3, 1]},
                                     sharex=True)

    fig.patch.set_facecolor("#1a1a2e")
    ax1.set_facecolor("#16213e")
    ax2.set_facecolor("#16213e")

    # --- 主图: K线 + 均线 + 布林带 ---
    x = range(len(closes))

    # 布林带填充
    if bb_u and bb_l:
        ax1.fill_between(x, [bb_u] * len(x), [bb_l] * len(x),
                         alpha=0.08, color="#4fc3f7", label="BB")

    # EMA20 / EMA50
    if len(ema20_series) > 20:
        ax1.plot(x, ema20_series, color="#fbbf24", linewidth=1.2, alpha=0.8, label="EMA20")
    if len(ema50_series) > 50:
        ax1.plot(x, ema50_series, color="#f472b6", linewidth=1.2, alpha=0.8, label="EMA50")

    # 价格线 (用收盘价连成的线，更清晰)
    ax1.plot(x, closes, color="#60a5fa", linewidth=1.8, alpha=0.9, label=f"{name}")

    # 当前价格标注
    last_price = closes[-1]
    ax1.axhline(y=last_price, color="#60a5fa", linewidth=0.8, linestyle="--", alpha=0.4)
    ax1.annotate(f"${last_price:,.2f}",
                 xy=(len(closes) - 1, last_price),
                 xytext=(len(closes) - 1, last_price),
                 fontsize=11, color="#60a5fa", fontweight="bold",
                 va="bottom", ha="right")

    # 标题 - 全英文避免中文字体问题
    change_str = f"{data['change_24h']:+.2f}%"
    change_color = "#22c55e" if data['change_24h'] >= 0 else "#ef4444"
    ax1.set_title(f"  {name}/{symbol.upper()}  |  ${last_price:,.2f}  |  24h: ",
                  color="#e2e8f0", fontsize=13, fontweight="bold", loc="left")
    ax1.text(0.5, 0.975, change_str, transform=ax1.transAxes,
             fontsize=13, color=change_color, fontweight="bold",
             va="top", ha="left")

    # 图例 — 用英文避免乱码
    ax1.legend(loc="upper left", fontsize=8, facecolor="#1e293b",
               edgecolor="none", labelcolor="#cbd5e1")

    # Y轴格式
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax1.tick_params(colors="#64748b", labelsize=9)
    ax1.grid(True, alpha=0.15, color="#475569")

    # --- 副图: RSI ---
    rsi_values = []
    for i in range(len(closes)):
        rsi_values.append(calc_rsi(np.array(closes[:i+1])))

    ax2.plot(x, rsi_values, color="#a78bfa", linewidth=1.5, alpha=0.9, label="RSI(14)")
    ax2.axhline(y=70, color="#ef4444", linewidth=0.8, linestyle="--", alpha=0.5)
    ax2.axhline(y=30, color="#22c55e", linewidth=0.8, linestyle="--", alpha=0.5)
    ax2.fill_between(x, rsi_values, 50, where=np.array(rsi_values) >= 50,
                     color="#ef4444", alpha=0.06)
    ax2.fill_between(x, rsi_values, 50, where=np.array(rsi_values) < 50,
                     color="#22c55e", alpha=0.06)

    ax2.set_ylim(0, 100)
    ax2.set_ylabel("RSI", color="#64748b", fontsize=9)
    ax2.tick_params(colors="#64748b", labelsize=9)
    ax2.grid(True, alpha=0.15, color="#475569")
    ax2.legend(loc="upper left", fontsize=8, facecolor="#1e293b",
               edgecolor="none", labelcolor="#cbd5e1")

    # X轴时间标签
    n = len(x)
    if n > 20:
        step = max(1, n // 6)
        ax2.set_xticks(range(0, n, step))
        tick_labels = [times[i].strftime("%m/%d\n%H:%M") for i in range(0, n, step)]
        ax2.set_xticklabels(tick_labels, fontsize=8)

    plt.tight_layout(pad=1.5)

    # 保存
    filename = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    filepath = os.path.join(CHART_DIR, filename)
    plt.savefig(filepath, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)

    return filepath


# ==================== 分析 ====================
def analyze_symbol(name, symbol):
    """分析一个币种"""
    try:
        cc_ticker, cc_klines, cc_change = gate_get_ticker(symbol)
        ticker = cc_ticker
        klines = cc_klines
    except Exception as e:
        return {"name": name, "symbol": symbol, "error": f"Gate:{e}"}, None

    closes = np.array([k["close"] for k in klines])
    volumes = np.array([k.get("volumefrom", k.get("amount", 0)) for k in klines])

    current_price = ticker["close"]
    open_price = ticker["open"]
    high_24h = ticker["high"]
    low_24h = ticker["low"]
    change_pct = round((current_price - open_price) / open_price * 100, 2)

    rsi = calc_rsi(closes)
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    sma200 = calc_sma(closes, 200)
    macd, macd_sig, macd_hist = calc_macd(closes)
    bb_u, bb_m, bb_l = calc_bollinger(closes)
    vol_ratio = calc_volume_ratio(volumes)

    if rsi >= 70:
        rsi_sig = "超买 🚨"
    elif rsi >= 60:
        rsi_sig = "偏强 📈"
    elif rsi >= 40:
        rsi_sig = "中性 ⚖️"
    elif rsi >= 30:
        rsi_sig = "偏弱 📉"
    else:
        rsi_sig = "超卖 🆘"

    data = {
        "name": name, "symbol": symbol,
        "price": current_price, "open": open_price,
        "change_24h": change_pct,
        "high_24h": high_24h, "low_24h": low_24h,
        "volume_coin": ticker["amount"], "volume_usdt": ticker["vol"],
        "rsi": rsi, "rsi_signal": rsi_sig,
        "ema20": ema20, "ema50": ema50, "sma200": sma200,
        "macd": macd, "macd_signal": macd_sig, "macd_histogram": macd_hist,
        "bb_upper": bb_u, "bb_mid": bb_m, "bb_lower": bb_l,
        "vol_ratio": vol_ratio,
        "price_vs_ema20": "above" if ema20 and current_price > ema20 else "below" if ema20 else "unknown",
    }

    # 画图
    chart_path = draw_chart(name, symbol, klines, data)

    return data, chart_path


# ==================== AI 分析 ====================

def build_prompt(data):
    s = data
    def fmt(v, prefix="$", suffix=""):
        if v is None:
            return "N/A"
        if isinstance(v, float):
            return f"{prefix}{v:,.2f}{suffix}"
        return f"{prefix}{v}{suffix}"

    return f"""你是一个专业的加密货币技术分析师。请根据以下 {s['name']} ({s['symbol'].upper()}) 的实时数据，给出简洁的行情解读和操作建议。

【行情数据】
当前价格: ${s['price']:,.2f}
24h涨跌: {s['change_24h']:+.2f}%
24h最高: ${s['high_24h']:,.2f}
24h最低: ${s['low_24h']:,.2f}

【技术指标】
RSI(14): {s['rsi']} ({s['rsi_signal']})
EMA20: {fmt(s['ema20'])}
EMA50: {fmt(s['ema50'])}
SMA200: {fmt(s['sma200'])}
MACD: {fmt(s['macd'], suffix="")}
布林带上轨: {fmt(s['bb_upper'])}
布林带中轨: {fmt(s['bb_mid'])}
布林带下轨: {fmt(s['bb_lower'])}
成交量比(20期均值): {s['vol_ratio']}x

请用以下格式回复，控制在200字以内：

📊 {s['name']} 行情解读
[2-3句话描述趋势]

🎯 操作建议
建议: [买入/卖出/持有]
参考价位: [如有]
止损参考: [如有]
理由: [一句话]

⚠️ 风险提示
[一句话]"""


def ask_ai(prompt, model="auto"):
    """调用AI分析，支持自动模型路由"""
    from wechat_config import AI_API_KEY, DEEPSEEK_API_KEY
    APP_CODE = os.environ.get("AIHUBMIX_APP_CODE", "")

    # 智能路由
    if model == "auto":
        prompt_lower = prompt.lower()
        # 需要实时信息的场景 -> Grok
        if any(kw in prompt_lower for kw in ["今日", "今天", "最近", "刚刚", "最新", "动态", "发生了什么", "新闻", "实时", "消息", "事件"]):
            use_model = "grok-4.3"
            api_url = "https://api.aihubmix.com/v1/chat/completions"
            api_key = AI_API_KEY
        else:
            use_model = "deepseek-chat"
            api_url = "https://api.deepseek.com/v1/chat/completions"
            api_key = DEEPSEEK_API_KEY
    else:
        use_model = model
        if model == "deepseek-chat":
            api_url = "https://api.deepseek.com/v1/chat/completions"
            api_key = DEEPSEEK_API_KEY
        else:
            api_url = "https://api.aihubmix.com/v1/chat/completions"
            api_key = AI_API_KEY

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    # 调用 AIHubMix 时加上 APP-Code 头（享受10%优惠）
    if "aihubmix" in api_url and APP_CODE:
        headers["APP-Code"] = APP_CODE
    payload = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": "你是一个专业的加密货币技术分析师。\n\n铁律：\n1. 所有价格、涨跌幅、RSI等数据必须严格使用用户提供的数据，绝对不要自己编造或凭记忆猜测\n2. 如果用户提到一个币种但你没有它的实时数据，就说\"暂无该币种实时数据\"\n3. 回复要简洁有用，控制在150字内\n4. 任何分析和建议都不构成投资建议，请提醒用户自行判断风险。使用中文回复。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 500
    }

    try:
        resp = requests.post(
            api_url,
            headers=headers, json=payload, timeout=15
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠️ AI 分析调用失败: {e}"
# ==================== 主程序 ====================

def main():
    bj_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(bj_tz)

    print("=" * 60)
    print(f"  🔮 虚拟币监控 AI 助手")
    print(f"  📅 {now_bj.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print(f"  📡 数据源: 火币 (Huobi)")
    print(f"  🖼️  走势图: {'✅ 已启用' if CHART_ENABLED else '❌ 未安装matplotlib'}")
    print("=" * 60)
    print()

    charts = []

    for name, symbol in WATCHLIST:
        print(f"─── {name}/{symbol.upper()} ───")
        print(f"  📡 获取数据...", end=" ", flush=True)

        data, chart_path = analyze_symbol(name, symbol)

        if "error" in data:
            print(f"❌ 错误: {data['error']}")
            print()
            continue

        print("✅", end="")
        msg = f"  💰 ${data['price']:,.2f}  |  24h: {data['change_24h']:+.2f}%"
        print(msg)

        # 显示图路径
        if chart_path:
            charts.append(chart_path)
            print(f"  🖼️  走势图: {chart_path}")

        print(f"  📊 RSI: {data['rsi']} ({data['rsi_signal']})  |  成交量比: {data['vol_ratio']}x")
        print()

        # AI 分析
        print(f"  🤖 AI 分析中...", end=" ", flush=True)
        analysis = ask_ai(build_prompt(data))
        print("✅")
        print()
        print(analysis)
        print()
        print()

    print("=" * 60)
    print("  分析完成 ✅")
    print(f"  共生成 {len(charts)} 张走势图")
    print("=" * 60)


def run_and_export():
    """运行分析，返回 (分析文字列表, 图片路径列表) 供外部调用"""
    results = []
    charts = []

    for name, symbol in WATCHLIST:
        data, chart_path = analyze_symbol(name, symbol)
        if "error" in data:
            continue
        if chart_path:
            charts.append(chart_path)
        analysis = ask_ai(build_prompt(data))
        results.append(analysis)

    return results, charts


if __name__ == "__main__":
    main()
