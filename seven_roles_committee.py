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
from clients.gateio_trade import execute_order, _get_holdings, DRY_RUN

# 从配置文件读取 Yunwu API KEY
YUNWU_API_KEY = "sk-fkyjAQt1zP02Q5YZ3TQM3CEQ0hYcGwpWotcPSpY26zV0GDgW"
try:
    from wechat_config import YUNWU_API_KEY as _cfg_key
    if _cfg_key: YUNWU_API_KEY = _cfg_key
except:
    pass

YUNWU_URL = "https://yunwu.ai/v1/chat/completions"

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
输出要求：给出你最终拍板的决策（BUY/SELL/HOLD）以及深度思考理由，不超过200字。""",

    "trader": """你是一个没有感情的API交易执行机器。
你的任务：阅读风控经理的最终裁决，将其严格转化为JSON格式。
输出格式要求：{"action": "BUY"或"SELL"或"HOLD", "reason": "一句话理由"}
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
    try:
        url = "https://cointelegraph.com/rss"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        root = ET.fromstring(resp.content)
        items = root.findall('./channel/item')
        
        relevant_news = []
        for item in items:
            title = item.find('title').text if item.find('title') is not None else ""
            if coin_name.upper() in title.upper():
                relevant_news.append(title)
            if len(relevant_news) >= 3: break
                
        if not relevant_news:
            for item in items[:2]:
                title = item.find('title').text if item.find('title') is not None else ""
                relevant_news.append(title)
                
        return "\n".join([f"- {t}" for t in relevant_news]) if relevant_news else "暂无新闻"
    except:
        return "暂无新闻"

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
    
    # 转换 symbol 格式给 Gate 数据抓取用
    gate_symbol = symbol.replace("usdt", "_USDT").upper()
    market_data, current_price = fetch_real_crypto_data(gate_symbol)
    if not market_data: 
        print("❌ 数据抓取失败，委员会解散")
        return
        
    news_data = fetch_real_crypto_news(coin_name)
    
    # 从 Hermes 原生账本获取真实持仓
    holdings = _get_holdings()
    coin_base = coin_name.upper()
    current_qty = holdings.get(coin_base, {}).get("quantity", 0.0)
    mock_position = f"已持仓 (数量: {current_qty})" if current_qty > 0 else "空仓 (0)"
    
    comprehensive_prompt = f"【数据】\n{market_data}\n\n【新闻】\n{news_data}"

    # ===== 开始开会 =====
    tech = ask_agent("tech_analyst", market_data, "deepseek-v4-flash")
    fund = ask_agent("fund_analyst", comprehensive_prompt, "deepseek-v4-flash")
    sent = ask_agent("sent_analyst", comprehensive_prompt, "deepseek-v4-flash")
    
    combined = f"技术面：{tech}\n基本面：{fund}\n情绪面：{sent}"
    bull = ask_agent("bull_researcher", combined, "deepseek-v3.2")
    bear = ask_agent("bear_researcher", combined, "deepseek-v3.2")
    
    debate = f"多头：{bull}\n空头：{bear}\n当前状态：{mock_position}。请严格遵守铁律。"
    risk = ask_agent("risk_manager", debate, "deepseek-v3.2")
    
    trade_cmd = parse_json_safely(ask_agent("trader", risk, "deepseek-v3.2"))
    action = trade_cmd.get("action", "HOLD").upper()
    reason = trade_cmd.get("reason", "无")
    
    # ===== 执行决策并记录账本 =====
    trade_result = {"action": "HOLD", "pnl_pct": 0, "pnl_usdt": 0}
    pnl_msg = f"⚪ 投研判定风险过高，维持观望。\n(初筛理由: {radar_reason})"
    
    if action in ["BUY", "SELL"]:
        # 真正调用 Hermes 的原生下单接口（DRY_RUN 会拦截并写账本）
        trade_result = execute_order(symbol, action, amount_usdt=10, coin_name=coin_name)
        
        dr_note = " (DRY RUN 模拟)" if DRY_RUN else ""
        if action == "BUY":
            pnl_msg = f"🟢 深度判定通过！已下达买单{dr_note}"
        elif action == "SELL":
            pnl_pct = trade_result.get("pnl_pct", 0)
            sign = "+" if pnl_pct > 0 else ""
            pnl_msg = f"🔴 深度判定卖出！已下达卖单{dr_note}\n💸 模拟平仓收益: {sign}{pnl_pct:.2f}%"
    
    # ===== 组装微信报告 =====
    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(action, "⚪")
    wechat_text = (
        f"🤖 7角色深度投研报告 [{coin_name}]\n"
        f"----------------------\n"
        f"🐂 多头核心逻辑:\n{bull[:120]}...\n\n"
        f"🐻 空头核心逻辑:\n{bear[:120]}...\n"
        f"----------------------\n"
        f"⚖️ 风控最终拍板:\n{emoji} 决策: {action}\n"
        f"💡 理由: {reason}\n\n"
        f"💼 账户动态:\n{pnl_msg}"
    )
    
    # 推送给微信
    send_simple_message(wechat_text)
    print(f"✅ {coin_name} 委员会决议已推送。")

if __name__ == "__main__":
    # 本地跑单测
    run_committee("ETH", "ethusdt", "单测运行")