#!/usr/bin/env python3
"""
7 角色多智能体投研委员会模块
由 alert_monitor.py 唤醒，负责对初筛信号进行深度红蓝对抗，并最终调用 gateio_trade 执行决策。
"""

import os
import sys
import json
import time
import math
import requests
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from clients.trade_policy import parse_command
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(__file__))

# 接入 Hermes 核心组件
from wechat_push import send_simple_message
from clients import gateio_trade as trade_executor
from clients.gateio_trade import execute_order, _get_holdings, DRY_RUN, get_real_balance, _get_initial_capital_usdt

# ====== 结构化决策日志（P0-5，2026-10-22）======
# 只做观测：写盘失败不影响任何决策；_ROLE_CALLS 仅记录本次委员会各角色调用的元数据。
try:
    from clients import decision_log as dlog
except Exception:
    try:
        import decision_log as dlog
    except Exception:
        dlog = None

_ROLE_CALLS = []


def _ev(kind, **fields):
    try:
        if dlog is not None:
            dlog.event(kind, **fields)
    except Exception:
        pass


def _remember(role, model, t0, status, err="", chars=0):
    """记录一次角色调用的元数据（不参与任何逻辑）。"""
    try:
        _ROLE_CALLS.append({
            "role": role, "model": model,
            "latency_ms": int((time.time() - t0) * 1000),
            "status": status, "err": str(err)[:120], "chars": chars,
        })
    except Exception:
        pass

# 从配置文件读取 Yunwu API KEY
YUNWU_API_KEY = os.environ.get("YUNWU_API_KEY", "")   # 不落真实密钥；服务器从 clients/mom.json 读取
try:
    from wechat_config import YUNWU_API_KEY as _cfg_key
    if _cfg_key: YUNWU_API_KEY = _cfg_key
except:
    pass

YUNWU_URL = "https://api.openlux.ai/v1/chat/completions"

# 单笔买入仓位上限（正常比例风控）。2026-09-30 取消“实盘测试强制满仓 100%”的旧铁律，
# 改为由风控经理按可用资金给 10%-50%；超过上限的指令在这里兜底压回上限。
MAX_BUY_PCT = 50

# ====== 1. 核心提示词矩阵 ======
AGENT_PROMPTS = {
    "tech_analyst": """你是一个冷酷无情的加密货币技术分析师。
你的任务：只看数据，不带任何情感。根据用户提供的价格、RSI、布林带等指标，指出当前的技术面状态。
输出要求：简明扼要，直接给出支撑位、阻力位和技术面结论，不超过200字。
【数值保留硬性要求】必须原样保留关键数值（现价、RSI具体值、布林带上中下轨具体价格、24h涨跌幅），严禁用模糊形容词代替数字。""",

    "fund_analyst": """你是一位资深的加密货币基本面研究员。
【铁律】：必须严格基于用户提供的【最新真实新闻】进行分析，提取核心利好或利空。请自动翻译并在脑内总结。绝对禁止编造不存在的事件！
你是一个专注且严谨的加密资产基本面分析师。请优先评估与当前分析币种（{coin_name}）直接相关的生态进展、链上数据、交易所公告或突发监管事件。如果传入的 RSS 新闻属于泛行业宏观新闻（如整个加密市场的合规、大盘趋势），请在分析时明确指出其对当前币种的间接影响，严禁生搬硬套或夸大无关新闻的利好/利空作用。请严格评估新闻与当前币种的直接关联度，剔除泛行业杂音，防止生搬硬套。
输出要求：指出该资产的核心价值支撑或近期的宏观风险，不超过150字。""",

    "sent_analyst": """你是市场情绪嗅探犬。
你的任务：结合技术面跌/涨幅以及最新新闻事件，评估当前市场情绪是恐慌、贪婪还是中性。
输出要求：给出情绪定性判断，不超过100字。""",

    "bull_researcher": """你是投资委员会的“死多头（Bull）”代表。
你的任务：阅读技术、基本面、情绪三份报告，拼命寻找应该买入（BUY）或持有 的理由！你要反驳任何悲观的观点，寻找抄底或追高的机会。
【论据硬性要求】论据中必须包含具体的支撑/阻力价格点位和指标数值；当前为纯现货交易，严禁出现‘做空/加空’等合约表述。
【信息权重】在综合多维度信息时，请遵循“量价技术面 > 短期情绪面 > 宏观基本面”的权重排序。如果基本面新闻属于与当前币种无关的行业杂音，多空双方均不得将该新闻作为核心买入或做空的核心论据。技术面量价权重高于宏观新闻，严禁将无关的行业宏观泛泛而谈作为核心多空论据。
【输出格式硬性要求】（违反会被系统截断/丢弃）
- 直接用中文写论点正文，不要复述对方观点、不要解释你在做什么、不要和读者对话或提问；
- 严禁“我帮你整理”“你这套框架”这类元话术，也严禁评价任务本身；
- 不要用 Markdown：不要 ## 标题、不要表格、不要 ** 加粗、不要分隔线；
- 只写 3-4 条要点，总长不超过 300 字。""",

    "bear_researcher": """你是投资委员会的“死空头（Bear）”代表。
你的任务：阅读技术、基本面、情绪三份报告，拼命寻找应该卖出（SELL）或观望 的理由！无情打击多头的盲目乐观。
【论据硬性要求】论据中必须包含具体的支撑/阻力价格点位和指标数值；当前为纯现货交易，严禁出现‘做空/加空’等合约表述。
【信息权重】在综合多维度信息时，请遵循“量价技术面 > 短期情绪面 > 宏观基本面”的权重排序。如果基本面新闻属于与当前币种无关的行业杂音，多空双方均不得将该新闻作为核心买入或做空的核心论据。技术面量价权重高于宏观新闻，严禁将无关的行业宏观泛泛而谈作为核心多空论据。
【输出格式硬性要求】（违反会被系统截断/丢弃）
- 直接用中文写论点正文，不要复述对方观点、不要解释你在做什么、不要和读者对话或提问；
- 严禁“我帮你整理”“你这套框架”这类元话术，也严禁评价任务本身；
- 不要用 Markdown：不要 ## 标题、不要表格、不要 ** 加粗、不要分隔线；
- 只写 3-4 条要点，总长不超过 300 字。""",

    "risk_manager": """你是公司的终极风控大脑与投资委员会主席。
你的任务：
1. 审视多头和空头的辩论。
2. 结合当前的【真实持仓情况】（空仓还是满仓）。
3. 做出最终的裁决。
铁律：如果你当前是【空仓】，绝对不允许给出 SELL 建议；如果当前【已有持仓】，绝对不允许给出 BUY 建议。矛盾或不确定时输出 HOLD。
4. 【抗磨损铁律】：真实交易所存在单边 0.1% 的手续费。如果判断上涨空间不足 3%，严禁给出 BUY 建议，宁可错过绝不做无效交易！
5. 【仓位管理】：按【当前状态】里给出的账户可用资金计算仓位。单次 BUY 的建仓比例 (percentage) 为可用资金的 10%-50%，且单笔金额不低于 5 USDT（交易所最小下单门槛）；不得以“凑门槛”为由放大到高仓位。
6. 【实盘风控红线】：严禁满仓梭哈，任何情况下 BUY 的 percentage 不得超过 50。低确定性轻仓（10%-15%）、中等确定性（20%-35%）、高确定性重仓（40%-50%）。不确定时宁可轻仓或 HOLD，保留现金。
7. 【扩大盈亏比】：严禁在只有微薄利润（如 1%）时就轻易 SELL 止盈，必须耐心持有到核心阻力位或趋势反转才可平仓。
8. 【卖出仓位】：SELL 的 percentage 表示卖出占【当前持仓量】的比例，趋势反转/止损时可以给 100（清仓），普通减仓给 30-60。
核心要求：BUY 必须给出 10-50 的仓位百分比；SELL 给出 30-100 的仓位百分比；HOLD 时为 0。
【拍板理由硬性要求】：拍板理由中必须写出具体的关键价格点位（如“需站稳 $XXX”或“失守 $XXX 风险高”），禁止只写“等待企稳”等无数字的模糊套话。
输出要求：给出你最终拍板的决策（BUY/SELL/HOLD）、仓位百分比（整数，HOLD 时为 0）以及深度思考理由，不超过200字。""",

    "trader": """你是一个没有感情的API交易执行机器。
你的任务：阅读风控经理的最终裁决，将其严格转化为JSON格式。
    输出格式要求：{"action": "BUY"或"SELL"或"HOLD", "percentage": 整数（BUY时为10-50，SELL时为30-100，HOLD时为0）, "reason": "一句话理由"}
绝对不要输出任何多余的Markdown符号，只输出字典本身！"""
}

# ====== 2. 数据抓取 ======
def wilder_rsi(closes, period=14):
    """标准 Wilder 平滑 RSI（RMA），与交易所/看盘软件口径一致。
    2026-10-17：替换原来的简化平均法（旧法只取 14 根涨跌幅的简单均值，与看盘软件有偏差）。"""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)


def format_rich_market_data(d):
    """方案B（2026-10-17）：把 crypto_monitor.analyze_symbol() 的完整指标
    （250 根 1h K 线口径）拼成给 AI 看的行情文本；缺字段显示 N/A。"""
    def f(v, prefix="$", nd=2):
        return "N/A" if v is None else f"{prefix}{v:,.{nd}f}"

    hist = d.get("macd_histogram")
    macd_state = "N/A" if hist is None else ("金叉（多头）" if hist > 0 else ("死叉（空头）" if hist < 0 else "持平"))
    base = str(d.get("symbol", "")).replace("usdt", "").replace("USDT", "").upper()
    vr = d.get("vol_ratio")

    return "\n".join([
        f"价格:{f(d.get('price'))} | 24H涨跌:{d.get('change_24h', 0):+.2f}%",
        f"24H最高:{f(d.get('high_24h'))} | 24H最低:{f(d.get('low_24h'))}",
        f"成交量:{d.get('volume_coin', 0):,.2f} {base}（{f(d.get('volume_usdt'), nd=0)} USDT） | 量比:{vr if vr is not None else 'N/A'}",
        f"1H RSI(14)（Wilder）:{d.get('rsi')}（{d.get('rsi_signal', 'N/A')}）",
        f"布林带(20,±2σ) 上/中/下轨:{f(d.get('bb_upper'))}/ {f(d.get('bb_mid'))}/ {f(d.get('bb_lower'))}",
        f"EMA20:{f(d.get('ema20'))} | EMA50:{f(d.get('ema50'))} | SMA200:{f(d.get('sma200'))} | 价格相对EMA20:{d.get('price_vs_ema20', 'unknown')}",
        f"MACD:{d.get('macd')} | 信号线:{d.get('macd_signal')} | 柱状图:{hist} → {macd_state}",
    ])


def fetch_real_crypto_data(gate_symbol, rich=None):
    """组装给 AI 的行情数据文本，返回 (market_data_str, current_price)。

    rich: crypto_monitor.analyze_symbol() 的完整指标字典（250 根 1h K 线口径）。
          传入则用它拼「豪华版」文本（方案B）；未传/字段不全时退回本函数的轻量抓取
          （21 根 1h K 线），保持向下兼容 —— 旧调用 fetch_real_crypto_data(sym) 行为不变。
    """
    if rich:
        try:
            md = format_rich_market_data(rich)
            price = float(rich.get("price") or 0)
            if md and price > 0:
                return md, price
        except Exception as e:
            print(f"⚠️ 富数据组装失败，退回轻量抓取: {e}")

    try:
        ticker_url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={gate_symbol}"
        ticker_data = requests.get(ticker_url, timeout=10).json()[0]
        current_price = float(ticker_data['last'])
        change_24h = float(ticker_data['change_percentage'])
        high_24h = float(ticker_data.get('high_24h', 0) or 0)
        low_24h = float(ticker_data.get('low_24h', 0) or 0)
        vol_usdt = float(ticker_data.get('quote_volume', 0) or 0)

        kline_url = f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={gate_symbol}&interval=1h&limit=21"
        klines = requests.get(kline_url, timeout=10).json()
        closes = [float(k[2]) for k in klines]

        closes_20 = closes[-20:]
        sma = sum(closes_20) / 20
        std_dev = math.sqrt(sum([((x - sma) ** 2) for x in closes_20]) / 20)
        upper_band = sma + 2 * std_dev
        lower_band = sma - 2 * std_dev

        rsi = wilder_rsi(closes)   # 2026-10-17：标准 Wilder 平滑（原为简化平均法）

        market_data_str = (
            f"价格:${current_price:.2f} | 24H涨跌:{change_24h:+.2f}%\n"
            f"24H最高:${high_24h:.2f} | 24H最低:${low_24h:.2f} | 24H成交额:${vol_usdt:,.0f}\n"
            f"1H RSI(14)（Wilder）:{rsi} | 布林带上中下轨:${upper_band:.2f}/ ${sma:.2f}/ ${lower_band:.2f}"
        )
        return market_data_str, current_price
    except Exception as e:
        return None, 0


# ── 新闻相关性过滤（2026-10-17）──────────────────────────────────────────
# 问题：旧版只在标题里找币名，找不到就"随便给前两条" → 分析 BNB 时会塞进 Solana /
#       以太坊的新闻，基本面分析被带偏；而且不看时间，可能把上周旧闻当今天利好。
# 现在三级过滤：① 直接相关（币种 + 生态关键词）优先 →
#              ② 行业宏观（SEC/Fed/ETF/稳定币）仅用于补足到 3 条 →
#              ③ 高度绑定竞品公链的新闻一律剔除；并过滤 24 小时以上的陈旧新闻。
NEWS_MAX_AGE_HOURS = 24
NEWS_TARGET = 3

COIN_ECOSYSTEM = {
    "BTC": ["BTC", "BITCOIN", "SATOSHI", "LIGHTNING NETWORK"],
    "ETH": ["ETH", "ETHEREUM", "VITALIK", "ERC-20", "ERC20", "EIGENLAYER", "LIDO"],
    "BNB": ["BNB", "BINANCE", "BSC", "BNB CHAIN", "CZ", "ZHAO", "PANCAKESWAP", "OPBNB"],
    "SOL": ["SOL", "SOLANA", "JUPITER", "JITO", "FIREDANCER", "SOLANA LABS"],
    "XRP": ["XRP", "RIPPLE"],
    "DOGE": ["DOGE", "DOGECOIN"],
    "ADA": ["ADA", "CARDANO"],
    "AVAX": ["AVAX", "AVALANCHE"],
    "TRX": ["TRX", "TRON"],
    "DOT": ["DOT", "POLKADOT"],
    "LTC": ["LTC", "LITECOIN"],
    "LINK": ["LINK", "CHAINLINK"],
    "POL": ["POL", "POLYGON", "MATIC"],
    "MATIC": ["POL", "POLYGON", "MATIC"],
}

MACRO_KEYWORDS = [
    "SEC", "FEDERAL RESERVE", "FED", "INTEREST RATE", "RATE CUT", "RATE HIKE", "INFLATION",
    "CPI", "TREASURY", "ETF", "STABLECOIN", "STABLE COIN", "REGULATION", "REGULATOR",
    "BLACKROCK", "LIQUIDITY", "TARIFF", "CRYPTO MARKET", "MARKET-WIDE",
]


def _kw_hit(text_up, keywords):
    """词边界匹配，避免 ETH 命中 WHETHER、SOL 命中 SOLUTION 这类误伤"""
    for k in keywords:
        if re.search(r"\b" + re.escape(k.upper()) + r"\b", text_up):
            return True
    return False


def _clean_text(html_text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")).strip()


def fetch_real_crypto_news(coin_name, max_items=NEWS_TARGET, max_age_hours=NEWS_MAX_AGE_HOURS):
    """从 CoinTelegraph RSS 抓新闻并按【当前币种】相关性过滤后再喂给 AI。
    返回带 [直接相关]/[行业宏观(间接)] 标记的文本；无可用新闻返回 ""。"""
    urls = ["https://cointelegraph.com/rss"]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    coin_up = str(coin_name or "").upper().strip()
    own_kw = COIN_ECOSYSTEM.get(coin_up, [coin_up]) if coin_up else []
    # 竞品公链词（自己除外；BTC 是大盘基准不列为竞品，其 ETF/宏观新闻归入②类）
    rival_kw = []
    for _sym, _kws in COIN_ECOSYSTEM.items():
        if _sym in (coin_up, "BTC"):
            continue
        rival_kw.extend(_kws)

    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            items = ET.fromstring(resp.content).findall("./channel/item")
        except Exception:
            continue

        pool = []
        for it in items:
            title = _clean_text(it.findtext("title"))
            if not title:
                continue
            body = (title + " " + _clean_text(it.findtext("description"))).upper()
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate") or "")
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except Exception:
                pub = None
            pool.append((pub, title, body))

        if not pool:
            continue
        # 新鲜度：只留 24h 内；若整批都解析不出时间则跳过该过滤（防 RSS 改格式导致抓不到新闻）
        if any(p for p, _t, _b in pool):
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            fresh = [x for x in pool if x[0] is None or x[0] >= cutoff]
        else:
            print("⚠️ [新闻] RSS 无可用时间字段，本次跳过新鲜度过滤")
            fresh = pool

        direct, macro = [], []
        for _p, title, body in fresh:
            if _kw_hit(body, own_kw):
                direct.append(title)              # ① 与本币直接相关
            elif _kw_hit(body, rival_kw):
                continue                          # ③ 竞品公链新闻 → 直接剔除
            elif _kw_hit(body, MACRO_KEYWORDS):
                macro.append(title)               # ② 行业宏观（间接影响）

        picked = [(t, "直接相关") for t in direct[:max_items]]
        if len(picked) < max_items:               # 精准新闻不足 3 条 → 用宏观通识补足
            picked += [(t, "行业宏观(间接)") for t in macro[:max_items - len(picked)]]

        if not picked:
            print(f"⚠️ [新闻] {coin_up}: 24h 内没有与 {coin_up} 或其行业宏观相关的新闻"
                  f"（已剔除 {len(fresh)} 条不相关），本次不喂新闻")
            return ""

        print(f"✅ [新闻] {coin_up}: 直接相关 {len(direct)} 条 / 行业宏观 {len(macro)} 条 / "
              f"24h候选 {len(fresh)} 条 → 采用 {len(picked)} 条")
        head = (f"（以下 {len(picked)} 条已按与 {coin_up} 的相关性过滤，只保留 {max_age_hours} 小时内的新闻；"
                f"标注[行业宏观(间接)]的属于泛行业信息，对本币只有间接影响，不得当作直接利好/利空）")
        return head + "\n" + "\n".join(f"- [{tag}] {t}" for t, tag in picked)

    return ""

# ====== 3. AI 调度 ======
# 模型后备链（2026-10-15 加）：v4-flash 偶发 429/超时/空回复时，依次换 v4.1-flash、v3-1。
# 旧版是"单模型 3 次重试"，一旦失败该角色就变成"[xxx 分析失败]"，整段投研被迫中止。
AGENT_MODEL_CHAIN = ["deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-v3-1"]

# ===== 角色 → 模型绑定（2026-10-16「实盘 V1.0」异构替换）=====
# 原则：一次只改一个变量 —— 本轮只做底层模型异构替换 + max_tokens 上调，
#       AGENT_PROMPTS 里的系统提示词一个字都没有改动。
# 选型依据：skill crypto-bot-repair/references/model-benchmark-openlux-2026-10.md（实测 20+ 模型）
#   gpt-5.6-luna    最快最省（$0.0007/次），JSON 干净、给具体价位 → 基础分析 + 交易员
#   gpt-4.1         3s 出干净 JSON，文字简洁 → 多头
#   claude-sonnet-5 论证最细（分批区间/止损位）→ 空头
#   gpt-5.2         谨慎、有止损止盈思路 → 风控
# 注：任一角色主模型失败时，仍按 AGENT_MODEL_CHAIN 顺序降级兜底。
ROLE_MODELS = {
    "tech_analyst":    "gpt-5.6-luna",
    "fund_analyst":    "gpt-5.6-luna",
    "sent_analyst":    "gpt-5.6-luna",
    "bull_researcher": "gpt-4.1",
    "bear_researcher": "claude-sonnet-5",
    "risk_manager":    "gpt-5.2",
    "trader":          "gpt-5.6-luna",
}

# 单次 AI 调用的输出上限（2026-10-16 由 800 上调至 1500：
# 思考型模型的 reasoning token 会吃掉预算，800 时实测出现正文空白/截断）
AGENT_MAX_TOKENS = 1500


@dataclass(frozen=True)
class RoleResult:
    ok: bool
    text: str = ""
    error: str = ""


class CommitteeUnavailable(Exception):
    pass


def require_role(role, result):
    if (type(result) is not RoleResult or result.ok is not True
            or type(result.text) is not str or not result.text.strip()):
        raise CommitteeUnavailable("必需角色失败: " + role)
    return result.text


def ask_agent(role_name, prompt, model=None, max_retries=2, coin_name=None):
    # 未显式指定模型时，按角色名查 ROLE_MODELS（默认 gpt-5.6-luna）
    if not model:
        model = ROLE_MODELS.get(role_name, "gpt-5.6-luna")
    # 系统提示词里的 {coin_name} 占位符按本次分析的币种替换（用 replace 而非 format，
    # 避免和 trader 提示词里的 JSON 花括号冲突）
    sys_prompt = AGENT_PROMPTS[role_name]
    if "{coin_name}" in sys_prompt:
        sys_prompt = sys_prompt.replace("{coin_name}", str(coin_name or "当前币种"))
    headers = {"Authorization": f"Bearer {YUNWU_API_KEY}", "Content-Type": "application/json"}
    models = [model] + [m for m in AGENT_MODEL_CHAIN if m != model]
    last_err = ""
    _t_agent = time.time()
    for use_model in models:
        payload = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": AGENT_MAX_TOKENS
        }
        for attempt in range(max_retries):
            try:
                response = requests.post(YUNWU_URL, headers=headers, json=payload, timeout=30)
                if response.status_code != 200:
                    last_err = f"HTTP {response.status_code}"
                    break              # 明确的 HTTP 错误（429/404/5xx）→ 直接换下一个模型
                txt = (response.json()["choices"][0]["message"]["content"] or "").strip()
                if txt:
                    _remember(role_name, use_model, _t_agent, "ok", chars=len(txt))
                    return RoleResult(True, txt)
                last_err = "空回复"
            except Exception as e:
                last_err = type(e).__name__
                time.sleep((attempt + 1) * 2)
        print(f"⚠️ [7角色] {role_name} 模型 {use_model} 失败: {last_err}")
    print(f"❌ [7角色] {role_name} 全部模型失败: {last_err}")
    _remember(role_name, model, _t_agent, "failed", err=last_err)
    return RoleResult(False, error="role_unavailable")


def parse_json_safely(text):
    """Compatibility name; accepts only a complete, unambiguous strict command."""
    return parse_command(text)


# ── 企业微信推送长度/markdown 保护（2026-10-15 加）──────────────
# 企业微信 text 消息上限 2048 字节（中文 3 字节/字），超长会被静默截断；
# 而且企业微信不渲染 Markdown（**加粗**、## 标题、表格会原样显示）。
WECHAT_MAX_BYTES = 1900


def _strip_md(t) -> str:
    """去掉企业微信不渲染的 Markdown 记号。"""
    if not t:
        return ""
    s = str(t)
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)                     # **加粗**
    s = re.sub(r'^\s{0,3}#{1,6}\s*', '', s, flags=re.M)        # ## 标题
    s = re.sub(r'^\s*\|?[\s:|-]+\|[\s:|-]*$', '', s, flags=re.M)  # |---|---| 表格分隔行
    s = s.replace('|', ' ')                                    # 表格竖线
    s = re.sub(r'^\s*[-*_]{3,}\s*$', '', s, flags=re.M)        # --- 分隔线
    s = re.sub(r'^\s*>\s?', '', s, flags=re.M)                 # > 引用
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _clip(t, n: int) -> str:
    """去 markdown + 限长（超长加省略号）。"""
    t = _strip_md(t)
    return t if len(t) <= n else t[:n].rstrip() + "…"


# ====== 4. 主调用入口 ======
def run_committee(coin_name, symbol, radar_reason="", scan_id=None):
    try:
        return _run_committee(coin_name, symbol, radar_reason, scan_id)
    except CommitteeUnavailable:
        _ev("committee_blocked", scan_id=scan_id, coin=coin_name,
            reason="必需数据、角色或交易指令校验失败，未发送新订单")
        return False


def _run_committee(coin_name, symbol, radar_reason="", scan_id=None):
    """由 alert_monitor 触发的深度投研"""
    if trade_executor.DRY_RUN is not True:
        raise CommitteeUnavailable("实盘路径未开放")
    committee_id = dlog.new_id("c") if dlog else "c-local"
    try:
        _ROLE_CALLS.clear()
    except Exception:
        pass
    _ev("committee_start", committee_id=committee_id, scan_id=scan_id,
        coin=coin_name, symbol=symbol, radar_reason=str(radar_reason)[:200])
    print(f"🚀 [7角色委员会] 被唤醒，开始深度评估 {coin_name}...")
    send_simple_message(f"🚀 {coin_name} 触发雷达信号，7 角色投研委员会启动...")

    # 转换 symbol 格式给 Gate 数据抓取用
    gate_symbol = symbol.replace("usdt", "_USDT").upper()

    # ── 方案B（2026-10-17）：复用浅层雷达 crypto_monitor.analyze_symbol() 的完整指标
    #    （250 根 1h K 线：EMA20/50、SMA200、MACD、量比、24h高低…），
    #    解决"雷达算好了却全扔掉、7 角色只有 RSI+布林"的口径分裂问题。
    #    失败绝不影响主流程：自动退回本文件的轻量抓取（21 根 K 线）。
    rich = None
    try:
        from crypto_monitor import analyze_symbol as _analyze_symbol
        _rich, _ = _analyze_symbol(coin_name, symbol)
        if _rich and "error" not in _rich:
            rich = _rich
            print(f"✅ [7角色] 已复用雷达完整指标: RSI={_rich.get('rsi')} 量比={_rich.get('vol_ratio')} "
                  f"MACD柱={_rich.get('macd_histogram')} SMA200={_rich.get('sma200')}")
        else:
            print(f"⚠️ [7角色] 雷达指标不可用，退回轻量抓取: {( _rich or {}).get('error')}")
    except Exception as _e:
        print(f"⚠️ [7角色] 调用 analyze_symbol 失败，退回轻量抓取: {_e}")

    market_data, current_price = fetch_real_crypto_data(gate_symbol, rich=rich)
    if not market_data:
        send_simple_message(f"❌ {coin_name} 数据抓取失败，委员会解散")
        print("❌ 数据抓取失败，委员会解散")
        return

    news_data = fetch_real_crypto_news(coin_name)

    # 从 Hermes 原生账本获取真实持仓
    holdings = _get_holdings()
    coin_base = coin_name.upper()
    current_qty = holdings.get(coin_base, {}).get("quantity", 0.0)
    mock_position = f"已持仓 (数量: {current_qty})" if current_qty > 0 else "空仓 (0)"

    # Only paper capital is used here; live committee is blocked above.
    try:
        avail_usdt = float(_get_initial_capital_usdt())
    except (ValueError, TypeError):
        raise CommitteeUnavailable("账户资金不可用") from None
    if not math.isfinite(avail_usdt) or avail_usdt <= 0:
        raise CommitteeUnavailable("账户资金不可用")

    comprehensive_prompt = f"【数据】\n{market_data}\n\n【新闻】\n{news_data}"

    # ── 方案A 升级版（2026-10-17）：穿透传递 —— 把拼好的完整原始行情数值文本
    #    直接注入多头/空头/风控的输入，不让后面的角色玩"传话筒"（原先他们只能看到
    #    前三个角色的文字结论，看不到任何数字，导致论据全是定性套话）。
    raw_block = f"【原始行情数据（硬指标，论据中必须引用其中的具体数值）】\n{market_data}\n\n"

    # ===== ⏳ 第一层：情报搜集分析中... =====
    send_simple_message(f"⏳ 第一层：情报搜集分析中...\n📈【技术分析师】🔍 分析 {coin_name} 技术指标...\n📰【基本面分析师】🔍 解读最新消息面...\n🎭【情绪分析师】🔍 嗅探市场情绪...")
    tech = require_role("tech_analyst", ask_agent("tech_analyst", market_data, ROLE_MODELS["tech_analyst"], coin_name=coin_name))
    fund = require_role("fund_analyst", ask_agent("fund_analyst", comprehensive_prompt, ROLE_MODELS["fund_analyst"], coin_name=coin_name))
    sent = require_role("sent_analyst", ask_agent("sent_analyst", comprehensive_prompt, ROLE_MODELS["sent_analyst"], coin_name=coin_name))

    # ===== ⏳ 第二层：红蓝激烈对抗中... =====
    send_simple_message(f"⏳ 第二层：红蓝激烈对抗中...\n🐂【多头研究员】🛡️ 构建做多逻辑\n🐻【空头研究员】⚔️ 构建做空逻辑")
    combined = f"技术面：{tech}\n基本面：{fund}\n情绪面：{sent}"
    bull = require_role("bull_researcher", ask_agent("bull_researcher", raw_block + combined, ROLE_MODELS["bull_researcher"], coin_name=coin_name))
    bear = require_role("bear_researcher", ask_agent("bear_researcher", raw_block + combined, ROLE_MODELS["bear_researcher"], coin_name=coin_name))

    # ===== ⏳ 第三层：风控大脑思考中... =====
    send_simple_message(f"⏳ 第三层：风控大脑思考中...\n⚖️【风控经理】🧠 综合多空辩论与持仓状态进行裁决")
    debate = (f"{raw_block}多头：{bull}\n空头：{bear}\n当前状态：{mock_position}。"
              f"账户可用资金约 {avail_usdt:.2f} USDT。请严格遵守铁律。")
    risk = require_role("risk_manager", ask_agent("risk_manager", debate, ROLE_MODELS["risk_manager"], coin_name=coin_name))

    # ===== ⏳ 第四层：交易员执行... =====
    send_simple_message(f"⏳ 第四层：交易员执行...\n👨‍💻【交易员】⚙️ 将风控裁决转化为交易指令")
    trader_raw = require_role("trader", ask_agent("trader", risk, ROLE_MODELS["trader"], coin_name=coin_name))
    try:
        trade_cmd = parse_json_safely(trader_raw)
    except (ValueError, TypeError):
        raise CommitteeUnavailable("交易指令无效") from None
    action = trade_cmd.get("action", "HOLD").upper()
    reason = trade_cmd.get("reason", "无")

    percentage = trade_cmd["percentage"]
    _ev("committee_decision", committee_id=committee_id, scan_id=scan_id, coin=coin_name,
        action=action, percentage=percentage, reason=str(reason)[:300],
        roles=list(_ROLE_CALLS))

    # ===== 执行决策并记录账本 =====
    trade_result = {"action": "HOLD", "pnl_pct": 0, "pnl_usdt": 0}
    pnl_msg = f"⚪ 投研判定风险过高，维持观望。\n(初筛理由: {radar_reason})"

    if action in ["BUY", "SELL"]:
        # P0-D（2026-10-22 第三轮）：下单异常必须被记录成"失败/结果未知"。
        # 注意：exchange_confirmed 恒为 False —— 现在拿到的只是"下单函数返回值"，
        # 不是交易所确认的成交（成交核实属 P0-4，未授权）。不要把它当成已成交。
        try:
            trade_result = execute_order(symbol, action, amount_usdt=10, coin_name=coin_name,
                                         percentage=percentage, request_id=("committee:" + str(scan_id) + ":" + coin_name + ":" + action) if scan_id else None)
        except Exception as _oe:
            _ev("order_result", committee_id=committee_id, scan_id=scan_id, coin=coin_name,
                action=action, percentage=percentage, dry_run=DRY_RUN,
                status="exception", success=False, exchange_confirmed=False,
                error=("%s: %s" % (type(_oe).__name__, _oe))[:300],
                detail="下单函数抛出异常，成交结果未知（未经交易所核实）")
            raise
        _ok_flag = bool((trade_result or {}).get("success"))
        _ev("order_result", committee_id=committee_id, scan_id=scan_id, coin=coin_name, action=action,
            percentage=percentage, dry_run=DRY_RUN,
            status=("returned_success" if _ok_flag else "returned_failure"),
            success=_ok_flag, exchange_confirmed=False,
            detail=str((trade_result or {}).get("detail"))[:400],
            fill_price=(trade_result or {}).get("fill_price"),
            quantity=(trade_result or {}).get("quantity"))

        dr_note = " (DRY RUN 模拟)" if DRY_RUN else ""
        ok = bool(trade_result.get("success"))
        detail = str(trade_result.get("detail") or "").strip()
        fill_price = trade_result.get("fill_price", 0) or 0
        qty = trade_result.get("quantity", 0) or 0
        act_cn = "买入" if action == "BUY" else "卖出"
        act_emoji = "🟢" if action == "BUY" else "🔴"

        if not ok:
            # 下单失败必须如实说明并带上原因，绝不能显示成"已成交 0"
            pnl_msg = (f"⚠️ 深度判定{act_cn}，但未能模拟成交（拟用仓位 {percentage}%）{dr_note}\n"
                       f"❗ 原因: {detail or '下单返回失败，详见服务器日志'}")
        elif trade_executor.DRY_RUN is not True and trade_result.get("exchange_confirmed") is not True:
            pnl_msg = "订单结果未知，未经交易所核实；不计入已成交或收益。"
        elif action == "BUY":
            pnl_msg = (f"{act_emoji} 深度判定买入，模拟成交！\n"
                       f"   动用仓位 {percentage}% ｜ 成交价 ${fill_price:,.2f} ｜ 数量 {qty} ｜ 约 {fill_price * qty:.2f} USDT{dr_note}")
        else:
            pnl_pct = trade_result.get("pnl_pct", 0) or 0
            sign = "+" if pnl_pct > 0 else ""
            tail = f"\n💸 模拟平仓收益: {sign}{pnl_pct:.2f}%" if (DRY_RUN and pnl_pct != 0) else ""
            pnl_msg = (f"{act_emoji} 深度判定卖出，模拟成交！\n"
                       f"   动用仓位 {percentage}% ｜ 成交价 ${fill_price:,.2f} ｜ 数量 {qty}{dr_note}{tail}")

    # ===== 组装最终微信报告 =====
    # 2026-10-15：企业微信单条文本上限 2048 字节（中文 3 字节/字），超了会被微信静默截断
    # （用户实测：多头写了一大段 Markdown，结果报告尾部"风控拍板/账户动态"整段丢失）。
    # 现在做两层保护：① 去掉微信不渲染的 Markdown；② 限长 + 自动收紧，保证整条 ≤1900 字节。
    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(action, "⚪")

    def _build(bull_n, reason_n):
        return (
            f"🤖 7角色深度投研报告 [{coin_name}]\n"
            f"----------------------\n"
            f"🐂 多头核心逻辑:\n{_clip(bull, bull_n)}\n\n"
            f"🐻 空头核心逻辑:\n{_clip(bear, bull_n)}\n"
            f"----------------------\n"
            f"⚖️ 风控最终拍板:\n{emoji} 决策: {action}\n"
            f"💡 理由: {_clip(reason, reason_n)}\n\n"
            f"💼 账户动态:\n{pnl_msg}"
        )

    wechat_text = _build(200, 150)
    for _bn, _rn in ((120, 120), (80, 80), (40, 60)):
        if len(wechat_text.encode("utf-8")) <= WECHAT_MAX_BYTES:
            break
        wechat_text = _build(_bn, _rn)

    send_simple_message(wechat_text)
    print(f"✅ {coin_name} 委员会决议已推送（{len(wechat_text.encode('utf-8'))} 字节）。")
    return True

if __name__ == "__main__":
    # P0-1（2026-10-22）：本入口会走完整链路并下单，默认禁用。
    # 需显式 SELFTEST=1 才运行，且强制纸面模式 + 屏蔽微信推送，避免误触实盘/打扰用户。
    import os as _os
    if _os.environ.get("SELFTEST") != "1":
        print("⛔ 已禁用：该入口会触发真实下单。设置 SELFTEST=1 才会运行（且强制 DRY_RUN=True）。")
        raise SystemExit(0)
    try:
        import clients.gateio_trade as _gt
        _gt.DRY_RUN = True
        DRY_RUN = True
        send_simple_message = lambda *a, **k: print("[selftest] 已屏蔽微信推送")
    except Exception as _e:
        print(f"⛔ 自测模式初始化失败，为安全中止: {_e}")
        raise SystemExit(2)
    print("⚠️ 自测模式：DRY_RUN 强制为 True，微信推送已屏蔽")
    run_committee("ETH", "ethusdt", "单测运行")