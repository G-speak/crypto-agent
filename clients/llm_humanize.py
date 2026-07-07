#!/usr/bin/env python3
"""
将行情数值翻译为文字状态，降低大模型数学计算负担。
供 crypto_monitor.py 的 build_prompt() 调用。
"""

def humanize_market_state(data: dict) -> dict:
    """
    输入：analyze_symbol() 返回的行情字典
    输出：三个纯文本结论

    参数 data 必须包含字段：
        price, ema20, ema50, rsi, bb_lower, bb_upper
    """
    price = data.get("price")
    ema20 = data.get("ema20")
    ema50 = data.get("ema50")
    rsi = data.get("rsi")
    bb_lower = data.get("bb_lower")
    bb_upper = data.get("bb_upper")
    bb_mid = data.get("bb_mid")

    # ── 趋势状态（价格 vs 均线）──
    if price is not None and ema20 is not None and ema50 is not None:
        above_ema20 = price > ema20
        above_ema50 = price > ema50
        if above_ema20 and above_ema50:
            trend = "短线偏多（价格站上EMA20和EMA50）"
        elif not above_ema20 and not above_ema50:
            trend = "短线偏空（价格跌破EMA20和EMA50）"
        elif above_ema20 and not above_ema50:
            trend = "趋势不明（价格位于EMA20与EMA50之间，短期偏多、中期偏空）"
        else:
            trend = "趋势不明（价格位于EMA20与EMA50之间，短期偏空、中期偏多）"
    else:
        trend = "趋势状态：数据不足，无法判断"

    # ── 震荡状态（RSI）──
    if rsi is not None:
        if rsi > 70:
            osc = f"超买（RSI={rsi:.1f} > 70，短期回调风险增加）"
        elif rsi < 30:
            osc = f"超卖（RSI={rsi:.1f} < 30，短期反弹概率增加）"
        else:
            osc = f"中性（RSI={rsi:.1f}，处于30-70正常区间）"
    else:
        osc = "震荡状态：RSI数据不足"

    # ── 支撑压力（布林带）──
    if price is not None and bb_lower is not None and bb_upper is not None and bb_mid is not None:
        if price <= bb_lower:
            sr = "触及下轨支撑（价格接近或跌破布林下轨，下方空间受限）"
        elif price >= bb_upper:
            sr = "触及上轨压力（价格接近或突破布林上轨，上方空间受限）"
        elif price < bb_mid:
            sr = "处于下轨至中轨之间（偏弱震荡，关注中轨压力）"
        else:
            sr = "处于中轨至上轨之间（偏强震荡，关注中轨支撑）"
    else:
        sr = "支撑压力：布林带数据不足"

    return {
        "trend_status": trend,
        "oscillator_status": osc,
        "support_resistance_status": sr,
    }


# ── 自测（直接运行 python clients/llm_humanize.py）──
if __name__ == "__main__":
    mock = {
        "price": 65000,
        "ema20": 64000,
        "ema50": 62000,
        "rsi": 65.3,
        "bb_lower": 60000,
        "bb_mid": 64500,
        "bb_upper": 69000,
    }
    result = humanize_market_state(mock)
    for k, v in result.items():
        print(f"{k}: {v}")
