"""Pure input validation. No credentials, filesystem writes or network."""
import json
import math


def validate_command(value):
    if type(value) is not dict or set(value) != {"action", "percentage", "reason"}:
        raise ValueError("指令必须包含且仅包含 action/percentage/reason")
    action, pct, reason = value["action"], value["percentage"], value["reason"]
    if type(action) is not str or action not in ("BUY", "SELL", "HOLD"):
        raise ValueError("交易动作无效")
    if type(pct) is not int:
        raise ValueError("仓位必须为整数")
    if type(reason) is not str or not reason.strip() or len(reason) > 2000:
        raise ValueError("交易理由无效")
    limits = {"BUY": (10, 50), "SELL": (30, 100), "HOLD": (0, 0)}
    low, high = limits[action]
    if not low <= pct <= high:
        raise ValueError("仓位超出动作允许范围")
    return dict(value)


def parse_command(text):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("重复 JSON 字段")
            out[key] = value
        return out

    def invalid_constant(_):
        raise ValueError("不允许非有限数值")

    if type(text) is not str or len(text) > 16000:
        raise ValueError("交易输出无效")
    return validate_command(json.loads(text, object_pairs_hook=unique,
                                       parse_constant=invalid_constant))


def valid_amount(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False
