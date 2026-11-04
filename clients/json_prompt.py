#!/usr/bin/env python3
"""
新的 AI Prompt 构建层：文字化行情 + JSON 输出。
不修改原有 build_prompt() / ask_ai()，新增独立函数。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from clients.llm_humanize import humanize_market_state

# ── 新的 System Prompt ──
JSON_SYSTEM_PROMPT = (
    "你是一个量化交易策略的执行终端。"
    "你的任务是根据系统提供的数据状态和新闻情绪，严格判断是否触发交易信号。"
    "铁律："
    "1. 不要输出任何多余的分析过程。"
    "2. 严格遵循系统提供的数据状态，绝对禁止自行计算或猜测。"
    "3. 必须且只能输出合法的 JSON 格式。"
)


def build_json_prompt(data: dict, coin_name: str, news_text: str = "",
                      holdings_text: str = "") -> str:
    """
    接收行情数据 data，用 humanize_market_state 转成文字状态，
    拼接成面向量化决策的 User Prompt，末尾要求 JSON 输出。

    参数:
        holdings_text: 当前持仓状态文字，如 "BTC (持仓: 有) 或 BTC (持仓: 空)"
    """
    state = humanize_market_state(data)

    news = news_text.strip() if news_text.strip() else "暂无重大新闻"

    # 如果没传入持仓信息，尝试自动获取
    if not holdings_text:
        try:
            from clients.position_review import position_context, position_text
            holdings_text = position_text(coin_name, position_context(coin_name.upper(), data.get('price', 0)))
        except ImportError:
            holdings_text = f"{coin_name}: 未知"

    return f"""当前币种：{coin_name}

【技术面状态】
趋势指标：{state['trend_status']}
震荡指标：{state['oscillator_status']}
支撑压力：{state['support_resistance_status']}

【消息面】
{news}

【当前持仓】
{holdings_text}

【决策规则（铁律优先）】
- 当前该币种持仓状态：{holdings_text}
- 铁律：如果你当前是空仓或不可交易灰尘，绝对不允许给出 SELL 建议，只能 BUY 或 HOLD。
- 允许分批建仓与加仓：即使已有持仓，技术面出现明确支撑、超卖反弹或突破买点时仍可判定 BUY；不能仅因下跌而补仓。持仓成本、浮盈亏及仓位占比作为风险参考。
- BUY 还须有可用资金并符合单币60%上限与账户20%可用现金保留；估值未知由执行器拒绝买入。
- 只有当技术面破位或超买且有可交易持仓（非灰尘）时，才可判定为 SELL。
- 任何信号不明确、指标冲突或铁律矛盾时，一律判定为 HOLD。

请输出如下 JSON 格式：
{{
    "action": "BUY" | "SELL" | "HOLD",
    "reason": "用一句话解释决策原因（20字以内）"
}}"""


def parse_json_reply(reply: str) -> dict:
    """
    从 AI 回复中提取第一个 { } 包裹的 JSON，兜底容错。
    支持 Markdown 代码块脱壳（```json ... ```）。
    """
    import json, re

    text = reply.strip()

    # ── Markdown 代码块脱壳 ──
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 如果 AI 裹了多余文字，用正则抓取第一个 { ... } 块
    # 使用非贪婪匹配，支持嵌套 JSON
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # 全失败则返回 HOLD
    return {"action": "HOLD", "reason": "AI 输出解析失败，默认持有"}


def ask_ai_json(prompt: str, model: str = "auto") -> dict:
    """Bounded candidate attempts; no ping, no free channel, no same-model retry."""
    from wechat_config import YUNWU_API_KEY
    from clients.ai_initial_routing import request_decision
    return request_decision(prompt, JSON_SYSTEM_PROMPT, YUNWU_API_KEY, model=model)


# ── 本地自测 ──
if __name__ == "__main__":
    mock_data = {
        "price": 65000,
        "ema20": 64000,
        "ema50": 62000,
        "rsi": 65.3,
        "bb_lower": 60000,
        "bb_mid": 64500,
        "bb_upper": 69000,
    }

    prompt = build_json_prompt(mock_data, "BTC")
    print("=" * 60)
    print("生成的 User Prompt：")
    print("=" * 60)
    print(prompt)
    print()
    print("=" * 60)
    print("模拟 AI 回复解析测试：")
    print("=" * 60)

    # 模拟几种可能的 AI 回复
    test_replies = [
        '{"action": "HOLD", "reason": "指标中性，观望"}',
        '{"action": "BUY", "reason": "超卖触底反弹"}',
        '嗯，让我分析一下...\n{"action": "SELL", "reason": "超买需回调"}',  # 裹了多余文字
        "这不是 JSON",  # 完全乱来
    ]
    for t in test_replies:
        parsed = parse_json_reply(t)
        print(f"  输入: {t[:40]}... => {parsed}")
