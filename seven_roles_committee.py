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
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

# 接入 Hermes 核心组件
from wechat_push import send_simple_message
from clients.gateio_trade import execute_order, _get_holdings, DRY_RUN, get_real_balance, _get_initial_capital_usdt

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
输出要求：简明扼要，直接给出支撑位、阻力位和技术面结论，不超过100字。""",

    "fund_analyst": """你是一位资深的加密货币基本面研究员。
【铁律】：必须严格基于用户提供的【最新真实新闻】进行分析，提取核心利好或利空。请自动翻译并在脑内总结。绝对禁止编造不存在的事件！
输出要求：指出该资产的核心价值支撑或近期的宏观风险，不超过150字。""",

    "sent_analyst": """你是市场情绪嗅探犬。
你的任务：结合技术面跌/涨幅以及最新新闻事件，评估当前市场情绪是恐慌、贪婪还是中性。
输出要求：给出情绪定性判断，不超过100字。""",

    "bull_researcher": """你是投资委员会的“死多头（Bull）”代表。
你的任务：阅读技术、基本面、情绪三份报告，拼命寻找**应该买入（BUY）或持有**的理由！你要反驳任何悲观的观点，寻找抄底或追高的机会。
输出要求：给出强有力的做多逻辑，不超过200字。""",

    "bear_researcher": """你是投资委员会的“死空头（Bear）”代表。
你的任务：阅读技术、基本面、情绪三份报告，拼命寻找**应该卖出（SELL）或观望**的理由！无情打击多头的盲目乐观。
输出要求：给出强有力的做空/避险逻辑，不超过200字。""",

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
输出要求：给出你最终拍板的决策（BUY/SELL/HOLD）、仓位百分比（整数，HOLD 时为 0）以及深度思考理由，不超过200字。""",

    "trader": """你是一个没有感情的API交易执行机器。
你的任务：阅读风控经理的最终裁决，将其严格转化为JSON格式。
    输出格式要求：{"action": "BUY"或"SELL"或"HOLD", "percentage": 整数（BUY时为10-50，SELL时为30-100，HOLD时为0）, "reason": "一句话理由"}
绝对不要输出任何多余的Markdown符号，只输出字典本身！"""
}

# ====== 2. 数据抓取 ======
def fetch_real_crypto_data(gate_symbol):
    try:
        ticker_url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={gate_symbol}"
        ticker_data = requests.get(ticker_url, timeout=10).json()[0]
        current_price = float(ticker_data['last'])
        change_24h = float(ticker_data['change_percentage'])

        kline_url = f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={gate_symbol}&interval=1h&limit=21"
        klines = requests.get(kline_url, timeout=10).json()
        closes = [float(k[2]) for k in klines] 

        closes_20 = closes[-20:]
        sma = sum(closes_20) / 20
        std_dev = math.sqrt(sum([((x - sma) ** 2) for x in closes_20]) / 20)
        upper_band = sma + 2 * std_dev
        lower_band = sma - 2 * std_dev

        closes_15 = closes[-15:]
        gains, losses = [], []
        for i in range(1, len(closes_15)):
            diff = closes_15[i] - closes_15[i-1]
            if diff > 0:
                gains.append(diff); losses.append(0)
            else:
                gains.append(0); losses.append(abs(diff))
                
        avg_gain = sum(gains) / 14 if sum(losses) != 0 else 0
        avg_loss = sum(losses) / 14 if sum(losses) != 0 else 0
        rsi = 100 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))

        market_data_str = f"价格:${current_price:.2f} | 24H涨跌:{change_24h:.2f}%\n1H RSI:{rsi:.1f} | 布林带上中下轨:${upper_band:.2f}/ ${sma:.2f}/ ${lower_band:.2f}"
        return market_data_str, current_price
    except Exception as e:
        return None, 0

def fetch_real_crypto_news(coin_name):
    """从 CoinTelegraph RSS 获取真实新闻，带多源轮询"""
    urls = [
        "https://cointelegraph.com/rss",
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall('./channel/item')
            
            relevant_news = []
            for item in items:
                title = item.find('title').text if item.find('title') is not None else ""
                # 清理 HTML 标签
                import re as _re
                clean_title = _re.sub(r'<[^>]+>', '', title).strip()
                if coin_name.upper() in clean_title.upper():
                    relevant_news.append(clean_title)
                if len(relevant_news) >= 3:
                    break
            
            if not relevant_news:
                for item in items[:2]:
                    title = item.find('title').text if item.find('title') is not None else ""
                    clean_title = _re.sub(r'<[^>]+>', '', title).strip()
                    relevant_news.append(clean_title)
            
            result = "\n".join([f"- {t}" for t in relevant_news]) if relevant_news else ""
            if result:
                return result
        except Exception as e:
            continue
    
    return ""

# ====== 3. AI 调度 ======
def ask_agent(role_name, prompt, model="deepseek-v3.2", max_retries=3):
    headers = {"Authorization": f"Bearer {YUNWU_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": AGENT_PROMPTS[role_name]},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    }
    for attempt in range(max_retries):
        try:
            response = requests.post(YUNWU_URL, headers=headers, json=payload, timeout=30)
            return response.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            time.sleep((attempt + 1) * 2)
    if role_name == "trader": return '{"action": "HOLD", "reason": "API异常，风控强制观望"}'
    return f"[{role_name} 分析失败]"

def parse_json_safely(text):
    text = text.strip()
    if text.startswith("\x60\x60\x60json"): text = text[7:]
    elif text.startswith("\x60\x60\x60"): text = text[3:]
    if text.endswith("\x60\x60\x60"): text = text[:-3]
    try: return json.loads(text.strip())
    except:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try: return json.loads(m.group())
            except: pass
    return {"action": "HOLD", "reason": "解析指令失败"}

# ====== 4. 主调用入口 ======
def run_committee(coin_name, symbol, radar_reason=""):
    """由 alert_monitor 触发的深度投研"""
    print(f"🚀 [7角色委员会] 被唤醒，开始深度评估 {coin_name}...")
    send_simple_message(f"🚀 {coin_name} 触发雷达信号，7 角色投研委员会启动...")

    # 转换 symbol 格式给 Gate 数据抓取用
    gate_symbol = symbol.replace("usdt", "_USDT").upper()
    market_data, current_price = fetch_real_crypto_data(gate_symbol)
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

    # 可用资金（供风控经理按正常比例算仓位：实盘读真实余额，失败则退回配置本金）
    try:
        if DRY_RUN:
            avail_usdt = float(_get_initial_capital_usdt())
        else:
            avail_usdt = float((get_real_balance().get("USDT") or {}).get("free", 0) or 0)
        if avail_usdt <= 0:
            avail_usdt = float(_get_initial_capital_usdt())
    except Exception as _e:
        print(f"⚠️ 读取可用资金失败，退回配置本金: {_e}")
        avail_usdt = float(_get_initial_capital_usdt())

    comprehensive_prompt = f"【数据】\n{market_data}\n\n【新闻】\n{news_data}"

    # ===== ⏳ 第一层：情报搜集分析中... =====
    send_simple_message(f"⏳ 第一层：情报搜集分析中...\n📈【技术分析师】🔍 分析 {coin_name} 技术指标...\n📰【基本面分析师】🔍 解读最新消息面...\n🎭【情绪分析师】🔍 嗅探市场情绪...")
    tech = ask_agent("tech_analyst", market_data, "deepseek-v4-flash")
    fund = ask_agent("fund_analyst", comprehensive_prompt, "deepseek-v4-flash")
    sent = ask_agent("sent_analyst", comprehensive_prompt, "deepseek-v4-flash")
    
    # ===== ⏳ 第二层：红蓝激烈对抗中... =====
    send_simple_message(f"⏳ 第二层：红蓝激烈对抗中...\n🐂【多头研究员】🛡️ 构建做多逻辑\n🐻【空头研究员】⚔️ 构建做空逻辑")
    combined = f"技术面：{tech}\n基本面：{fund}\n情绪面：{sent}"
    bull = ask_agent("bull_researcher", combined, "deepseek-v3.2")
    bear = ask_agent("bear_researcher", combined, "deepseek-v3.2")
    
    # ===== ⏳ 第三层：风控大脑思考中... =====
    send_simple_message(f"⏳ 第三层：风控大脑思考中...\n⚖️【风控经理】🧠 综合多空辩论与持仓状态进行裁决")
    debate = f"多头：{bull}\n空头：{bear}\n当前状态：{mock_position}。账户可用资金约 {avail_usdt:.2f} USDT。请严格遵守铁律。"
    risk = ask_agent("risk_manager", debate, "deepseek-v3.2")
    
    # ===== ⏳ 第四层：交易员执行... =====
    send_simple_message(f"⏳ 第四层：交易员执行...\n👨‍💻【交易员】⚙️ 将风控裁决转化为交易指令")
    trade_cmd = parse_json_safely(ask_agent("trader", risk, "deepseek-v3.2"))
    action = trade_cmd.get("action", "HOLD").upper()
    reason = trade_cmd.get("reason", "无")
    percentage = trade_cmd.get("percentage", 0)
    if not isinstance(percentage, int) or percentage < 0 or percentage > 100:
        percentage = 0
    elif action == "BUY" and percentage > MAX_BUY_PCT:
        # 正常比例风控兜底：买入仓位超上限时压回（SELL 不受限，允许 100 清仓）
        print(f"⚠️ BUY 仓位 {percentage}% 超过上限 {MAX_BUY_PCT}%，已压回")
        percentage = MAX_BUY_PCT
    
    # ===== 执行决策并记录账本 =====
    trade_result = {"action": "HOLD", "pnl_pct": 0, "pnl_usdt": 0}
    pnl_msg = f"⚪ 投研判定风险过高，维持观望。\n(初筛理由: {radar_reason})"
    
    if action in ["BUY", "SELL"]:
        trade_result = execute_order(symbol, action, amount_usdt=10, coin_name=coin_name, percentage=percentage)

        dr_note = " (DRY RUN 模拟)" if DRY_RUN else ""
        ok = bool(trade_result.get("success"))
        detail = str(trade_result.get("detail") or "").strip()
        fill_price = trade_result.get("fill_price", 0) or 0
        qty = trade_result.get("quantity", 0) or 0
        act_cn = "买入" if action == "BUY" else "卖出"
        act_emoji = "🟢" if action == "BUY" else "🔴"

        if not ok:
            # 下单失败必须如实说明并带上原因，绝不能显示成"已成交 0"
            pnl_msg = (f"⚠️ 深度判定{act_cn}，但未能成交（拟用仓位 {percentage}%）{dr_note}\n"
                       f"❗ 原因: {detail or '下单返回失败，详见服务器日志'}")
        elif action == "BUY":
            pnl_msg = (f"{act_emoji} 深度判定买入，已成交！\n"
                       f"   动用仓位 {percentage}% ｜ 成交价 ${fill_price:,.2f} ｜ 数量 {qty} ｜ 约 {fill_price * qty:.2f} USDT{dr_note}")
        else:
            pnl_pct = trade_result.get("pnl_pct", 0) or 0
            sign = "+" if pnl_pct > 0 else ""
            tail = f"\n💸 模拟平仓收益: {sign}{pnl_pct:.2f}%" if (DRY_RUN and pnl_pct != 0) else ""
            pnl_msg = (f"{act_emoji} 深度判定卖出，已成交！\n"
                       f"   动用仓位 {percentage}% ｜ 成交价 ${fill_price:,.2f} ｜ 数量 {qty}{dr_note}{tail}")
    
    # ===== 组装最终微信报告（完整输出，不截断） =====
    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(action, "⚪")
    wechat_text = (
        f"🤖 7角色深度投研报告 [{coin_name}]\n"
        f"----------------------\n"
        f"🐂 多头核心逻辑:\n{bull}\n\n"
        f"🐻 空头核心逻辑:\n{bear}\n"
        f"----------------------\n"
        f"⚖️ 风控最终拍板:\n{emoji} 决策: {action}\n"
        f"💡 理由: {reason}\n\n"
        f"💼 账户动态:\n{pnl_msg}"
    )
    
    send_simple_message(wechat_text)
    print(f"✅ {coin_name} 委员会决议已推送。")

if __name__ == "__main__":
    # 本地跑单测
    run_committee("ETH", "ethusdt", "单测运行")