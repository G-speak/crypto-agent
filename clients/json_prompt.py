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
            from clients.gateio_trade import _get_holdings
            h = _get_holdings()
            coin_base = coin_name.upper().split("/")[0]
            qty = h.get(coin_base, {}).get("quantity", 0.0)
            holdings_text = f"{coin_name}: {'有持仓' if qty > 0 else '空仓（无持仓）'}"
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
- 铁律：如果你当前是空仓，绝对不允许给出 SELL 建议，只能 BUY 或 HOLD。
- 铁律：如果你当前有持仓，绝对不允许给出 BUY 建议（不能再加仓），只能 SELL 或 HOLD。
- 只有当技术面出现强支撑（如超卖且触底）且空仓时，才可判定为 BUY。
- 只有当技术面破位或超买且有持仓时，才可判定为 SELL。
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


# ── 免费模型全局冷却（与 crypto_monitor.py 共用）──
try:
    from crypto_monitor import _FREE_MODEL_BLOCKED_UNTIL
except ImportError:
    _FREE_MODEL_BLOCKED_UNTIL = 0.0


def ask_ai_json(prompt: str, model: str = "gpt-4.1-nano-free") -> dict:
    """
    调用 AI（双平台动态路由 + 多模型轮询 + 每模型3次重试 + 免费冷却），
    返回解析后的 {"action": ..., "reason": ...}。
    """
    import requests, json, os, time

    from wechat_config import AI_API_KEY, YUNWU_API_KEY
    APP_CODE = os.environ.get("AIHUBMIX_APP_CODE", "")
    _log = print

    # MODEL_POOL：免费优先，付费兜底
    JSON_MODEL_POOL = [
        "gpt-4.1-nano-free",    # [AIHubMix免费] 主力
        "gpt-4.1-mini-free",    # [AIHubMix免费] 备用
        "step-3.7-flash-free",  # [AIHubMix免费] 阶跃星辰
        "deepseek-v4-flash",    # [Yunwu付费] 付费兜底
        "deepseek-v3.2",        # [Yunwu付费] JSON 稳定性最佳
        "MAI-DS-R1",            # [Yunwu付费] 推理兜底
    ]

    if model == "auto":
        candidate_models = JSON_MODEL_POOL
    else:
        candidate_models = [model]

    # 检查免费模型冷却期
    if time.time() < _FREE_MODEL_BLOCKED_UNTIL:
        candidate_models = [m for m in candidate_models if "free" not in m.lower()]
        _log(f"[ask_ai_json] 免费模型冷却至 {time.strftime('%H:%M', time.localtime(_FREE_MODEL_BLOCKED_UNTIL))}，跳过免费模型")

    free_all_429 = True

    for use_model in candidate_models:
        if "free" in use_model.lower():
            api_url = "https://api.aihubmix.com/v1/chat/completions"
            api_key = AI_API_KEY
        else:
            api_url = "https://api.openlux.ai/v1/chat/completions"
            api_key = YUNWU_API_KEY

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if "aihubmix" in api_url and APP_CODE:
            headers["APP-Code"] = APP_CODE

        payload = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": JSON_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 300,
        }

        for attempt in range(1, 4):
            try:
                resp = requests.post(api_url, headers=headers, json=payload, timeout=60)
                resp.raise_for_status()
                reply = resp.json()["choices"][0]["message"]["content"]
                if "free" in use_model.lower():
                    free_all_429 = False
                return parse_json_reply(reply)
            except requests.exceptions.HTTPError as e:
                if resp is not None and resp.status_code == 429 and attempt < 3:
                    _log(f"[ask_ai_json] 模型 {use_model} 429限流，等待3秒后第{attempt+1}次重试...")
                    time.sleep(3)
                    continue
                _log(f"[ask_ai_json] 模型 {use_model} HTTP错误: {e}，切换至下一模型")
                break
            except Exception as e:
                _log(f"[ask_ai_json] 模型 {use_model} 请求异常: {e}")
                if attempt < 3:
                    time.sleep(3)
                    continue
                break

    # 如果所有免费模型都 429，更新冷却（与 crypto_monitor 共享变量）
    if free_all_429 and any("free" in m.lower() for m in candidate_models):
        _FREE_MODEL_BLOCKED_UNTIL = time.time() + 3 * 3600
        _log(f"[ask_ai_json] 所有免费模型均429，冻结免费模型至 {time.strftime('%H:%M', time.localtime(_FREE_MODEL_BLOCKED_UNTIL))}")

    return {"action": "HOLD", "reason": "所有模型调用失败"}


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
