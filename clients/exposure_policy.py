"""BUY-only spot allocation policy. No exchange writes or trading decisions."""
from decimal import Decimal, InvalidOperation
import time

MAX_COIN_EXPOSURE_PCT = Decimal('60')
MIN_FREE_CASH_PCT = Decimal('20')
MAX_QUOTE_AGE = 60
# Budget only, not an assertion about the user's actual fee tier.
# Charge the greater of the reported taker rate and this conservative reserve.
FEE_RESERVE_FLOOR = Decimal('0.005')


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid_valuation') from None
    if not result.is_finite() or result < 0:
        raise ValueError('invalid_valuation')
    return result


def fresh_price(ticker, now=None):
    now = time.time() if now is None else now
    stamp = number(ticker.get('timestamp')) / 1000
    price = number(ticker.get('last'))
    if price <= 0 or not -2 <= Decimal(str(now)) - stamp <= MAX_QUOTE_AGE:
        raise ValueError('stale_or_invalid_quote')
    return price


def normalized_balances(balance):
    """CCXT aggregate maps or normalized per-asset records. Never double count."""
    output = {}
    if isinstance(balance.get('total'), dict):
        assets = set(balance['total']) | set(balance.get('free') or {}) | set(balance.get('used') or {})
        source = {a: {k: (balance.get(k) or {}).get(a) for k in ('free','used','total')} for a in assets}
    else:
        source = {a: row for a, row in balance.items() if isinstance(row, dict) and 'total' in row}
    for asset, row in source.items():
        total = number(row.get('total'))
        free, used = number(row.get('free')), number(row.get('used'))
        if abs(total-free-used) > Decimal('0.00000001'):
            raise ValueError('inconsistent_balance')
        if total:
            if not isinstance(asset, str) or not asset.isascii() or not asset.isalnum():
                raise ValueError('invalid_asset')
            output[asset] = {'total': total, 'free': free, 'used': used}
    return output


def calculate_total_net_worth(balance, ticker_for, now=None):
    """Spot account only, all nonzero assets including locked/dust; missing = error."""
    rows = normalized_balances(balance)
    usdt = rows.get('USDT', {'total': Decimal(0), 'free': Decimal(0)})
    values = {'USDT': usdt['total']}
    for asset, row in rows.items():
        if asset != 'USDT':
            values[asset] = row['total'] * fresh_price(ticker_for(asset+'/USDT'), now)
    total = sum(values.values(), Decimal(0))
    if total <= 0:
        raise ValueError('empty_valuation')
    return {'total': total, 'free_usdt': usdt['free'], 'asset_values': values}


def buy_budget(snapshot, coin, requested, fee_rate):
    total, free = number(snapshot['total']), number(snapshot['free_usdt'])
    value = number(snapshot['asset_values'].get(coin, 0))
    fee = max(number(fee_rate), FEE_RESERVE_FLOOR)
    if fee >= Decimal('0.1'):
        raise ValueError('invalid_fee_rate')
    # Conservative denominator: deduct the full quote fee reserve from NAV,
    # even when the actual exchange fee is taken in base or a discount token.
    coin_room = (total*MAX_COIN_EXPOSURE_PCT/100-value)/(1+MAX_COIN_EXPOSURE_PCT/100*fee)
    cash_room = (free-total*MIN_FREE_CASH_PCT/100)/(1+fee)
    if coin_room <= 0:
        return Decimal(0), 'max_exposure_reached'
    if cash_room <= 0:
        return Decimal(0), 'cash_reserve_reached'
    return min(number(requested), coin_room, cash_room), 'ok'
