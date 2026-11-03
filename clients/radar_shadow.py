"""Passive observation of existing alert -> valid HOLD decisions.

No imports of configuration, models, exchange, messaging or committee code.
The candidate is a hypothesis for deeper review, never a BUY/SELL recommendation.
Existing alert cooldown determines sampling; this is not a census of all scans.
"""
import math

VERSION = 'hold-alert-v1'


def positive(value):
    if isinstance(value, bool):
        raise ValueError('boolean price')
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('invalid price')
    return value


def record_candidate(emit, scan_id, coin, data, decision):
    if decision.get('action') != 'HOLD':
        return False
    reason = decision.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        return False
    if any(marker in reason for marker in ('解析失败', '所有模型调用失败')):
        return False
    try:
        price = positive(data.get('price'))
    except (TypeError, ValueError, OverflowError):
        return False
    if not scan_id or coin not in ('BTC', 'ETH', 'SOL', 'BNB'):
        return False
    emit('radar_shadow_candidate', rule_version=VERSION,
         candidate_id=f'{VERSION}:{scan_id}:{coin}', scan_id=scan_id,
         coin=coin, price=price, shallow_action='HOLD',
         reason=reason[:200], sample='existing_alert_passed_cooldown',
         execution='observation_only')
    return True
