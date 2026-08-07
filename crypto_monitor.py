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


# ==================== 配置 ====================
from wechat_config import AI_API_KEY, WATCHLIST

HUOBI_BASE = "https://api.huobi.pro"

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



def analyze_symbol(name, symbol):
    """分析一个币种（已移除画图功能）"""
    try:
        cc_ticker, cc_klines, cc_change = gate_get_ticker(symbol)
        ticker = cc_ticker
        klines = cc_klines
    except Exception as e:
        return {"name": name, "symbol": symbol, "error": f"Gate:{e}"}, None

    closes = np.array([k["close"] for k in klines])
    volumes = np.array([k.get("volumefrom", k.get("amount", 0)) for k in klines])

    current_price = ticker["close"]
    open_price = ticker.get("open", 0)
    high_24h = ticker["high"]
    low_24h = ticker["low"]
    change_pct = round(cc_change, 2)

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
    return data, None


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
理由: [一句话]"""


# ── 免费模型全局冷却 ──
_FREE_MODEL_BLOCKED_UNTIL = 0.0  # 免费模型全部429后，在此时间戳之前都跳过

def ask_ai(prompt, model="auto"):
    """调用AI分析，支持双平台动态路由 + 多模型轮询池 + 每模型3次重试"""
    global _FREE_MODEL_BLOCKED_UNTIL

    # MODEL_POOL：免费优先（省钱），付费兜底
    # 但如果免费模型全部429触发了冷却期，则跳过免费直接走付费
    MODEL_POOL = [
        "gpt-4.1-nano-free",    # [AIHubMix免费] 主力免费模型
        "gpt-4.1-mini-free",    # [AIHubMix免费] 备用免费模型
        "step-3.7-flash-free",  # [AIHubMix免费] 阶跃星辰 Flash
        "deepseek-v4-flash",    # [Yunwu付费] 付费兜底
        "deepseek-v3.2",        # [Yunwu付费] 付费兜底
        "deepseek-r1",            # [Yunwu付费] 付费兜底
    ]

    from wechat_config import AI_API_KEY, YUNWU_API_KEY
    APP_CODE = os.environ.get("AIHUBMIX_APP_CODE", "")

    if model == "auto":
        candidate_models = MODEL_POOL
    else:
        candidate_models = [model]

    # 检查免费模型冷却期：如果还在冷却中，直接剔除免费模型
    if time.time() < _FREE_MODEL_BLOCKED_UNTIL:
        candidate_models = [m for m in candidate_models if "free" not in m.lower()]
        log(f"[ask_ai] 免费模型冷却至 {time.strftime('%H:%M', time.localtime(_FREE_MODEL_BLOCKED_UNTIL))}，跳过免费模型")

    last_exception = None
    free_all_429 = True  # 跟踪本轮是否有免费模型成功

    for use_model in candidate_models:
        # 双平台路由：含 "free" 用 AIHubMix，否则用 Yunwu
        if "free" in use_model.lower():
            api_url = "https://api.aihubmix.com/v1/chat/completions"
            api_key = AI_API_KEY
        else:
            api_url = "https://api.openlux.ai/v1/chat/completions"
            api_key = YUNWU_API_KEY

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
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

        # 每模型内部3次重试（应对429限流等瞬时错误）
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    api_url,
                    headers=headers, json=payload, timeout=60
                )
                resp.raise_for_status()
                # 免费模型成功则取消冷却标记
                if "free" in use_model.lower():
                    free_all_429 = False
                return resp.json()["choices"][0]["message"]["content"]
            except requests.exceptions.HTTPError as e:
                if resp is not None and resp.status_code == 429 and attempt < 3:
                    log(f"[ask_ai] 模型 {use_model} 429限流，等待3秒后第{attempt+1}次重试...")
                    time.sleep(3)
                    continue
                last_exception = e
                log(f"[ask_ai] 模型 {use_model} HTTP错误: {e}，切换至下一模型")
                break
            except Exception as e:
                last_exception = e
                if attempt < 3:
                    log(f"[ask_ai] 模型 {use_model} 请求异常，等待3秒后第{attempt+1}次重试...")
                    time.sleep(3)
                    continue
                log(f"[ask_ai] 模型 {use_model} 重试3次均失败，切换至下一模型")
                break

    # 如果所有免费模型都 429，设置3小时冷却
    if free_all_429 and any("free" in m.lower() for m in candidate_models):
        _FREE_MODEL_BLOCKED_UNTIL = time.time() + 3 * 3600
        log(f"[ask_ai] 所有免费模型均429，冻结免费模型至 {time.strftime('%H:%M', time.localtime(_FREE_MODEL_BLOCKED_UNTIL))}")

    # 全部模型都失败
    return f"⚠️ AI 分析调用失败: {last_exception}"
# ==================== 主程序 ====================

def main():
    bj_tz = timezone(timedelta(hours=8))
    now_bj = datetime.now(bj_tz)

    print("=" * 60)
    print(f"  🔮 虚拟币监控 AI 助手")
    print(f"  📅 {now_bj.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print(f"  📡 数据源: 火币 (Huobi)")
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
