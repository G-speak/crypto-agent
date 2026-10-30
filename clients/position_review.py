"""Read-only position evidence and persistent review scheduling; no order API."""
from decimal import Decimal, InvalidOperation
import math
import sqlite3
from pathlib import Path
import os
import time


def decimal(value):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid_number') from None
    if not n.is_finite() or n < 0:
        raise ValueError('invalid_number')
    return n


def cost_evidence(fills, coin, quantity, price):
    """Moving average including base/quote fees; unmatched history stays unknown.

    Matching the queried fills to today's quantity is not proof that transfers or
    older trades never occurred. Always label this as a queried-history estimate.
    """
    result = {'cost_status': 'unknown', 'cost_reason': 'history_not_reconciled'}
    try:
        target, mark = decimal(quantity), decimal(price)
        unique = {}
        for row in fills:
            identity = str(row['id'])
            if identity in unique and unique[identity] != row:
                raise ValueError('conflicting_fill')
            unique[identity] = row
        qty = cost = Decimal(0)
        for row in sorted(unique.values(), key=lambda r: (decimal(r['create_time']), str(r['id']))):
            if row['currency_pair'] != coin + '_USDT':
                raise ValueError('wrong_pair')
            amount, rate, fee = map(decimal, (row['amount'], row['price'], row['fee']))
            if amount <= 0 or rate <= 0:
                raise ValueError('invalid_fill')
            if decimal(row.get('gt_fee', 0)) or decimal(row.get('point_fee', 0)):
                raise ValueError('third_currency_fee')
            unit = row['fee_currency']
            if fee and unit not in (coin, 'USDT'):
                raise ValueError('third_currency_fee')
            if row['side'] == 'buy':
                received = amount - (fee if unit == coin else 0)
                if received <= 0:
                    raise ValueError('invalid_fee')
                qty += received
                cost += amount * rate + (fee if unit == 'USDT' else 0)
            elif row['side'] == 'sell':
                removed = amount + (fee if unit == coin else 0)
                if removed > qty or qty == 0:
                    raise ValueError('missing_opening_inventory')
                cost -= cost * removed / qty
                qty -= removed
            else:
                raise ValueError('wrong_side')
        if target <= 0 or abs(qty - target) > Decimal('0.000000000001'):
            raise ValueError('quantity_mismatch')
        result = dict(cost_status='queried_history_estimate', cost_reason='90_day_fills_quantity_matched',
                      quantity=str(target), cost_usdt=str(cost), avg_cost=str(cost / target))
        if mark > 0:
            result.update(mark_price=str(mark), unrealized_usdt=str(target * mark - cost),
                          unrealized_pct=str((target * mark / cost - 1) * 100))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    return result


def history(reader, coin, now=None):
    """Bounded, paginated GET-only query. Failure never fabricates zero cost."""
    end = int(time.time() if now is None else now)
    start = end - 90 * 86400
    rows = []
    while start <= end:
        last = min(end, start + 29 * 86400 - 1)
        for page in range(1, 101):
            batch = reader._get('/spot/my_trades', {'currency_pair': coin + '_USDT',
                'from': start, 'to': last, 'limit': 1000, 'page': page}, private=True)
            if not isinstance(batch, list):
                raise ValueError('invalid_history')
            rows.extend(batch)
            if len(batch) < 1000:
                break
        else:
            raise ValueError('history_incomplete')
        start = last + 1
    return rows


def position_context(coin, price=0):
    from clients import gateio_trade as trade
    if not coin.isalpha() or not coin.isascii() or coin != coin.upper():
        raise ValueError('invalid_coin')
    if trade.DRY_RUN:
        holding = trade._get_holdings().get(coin, {})
        qty = decimal(holding.get('quantity', 0))
        account = trade._paper_account().snapshot()
        cash = decimal(account['cash'])
        result = dict(quantity=str(qty), available_usdt=str(cash), cost_status='unknown')
        if qty and decimal(holding.get('total_cost', 0)) > 0:
            cost = decimal(holding['total_cost'])
            result.update(cost_status='paper_ledger', cost_usdt=str(cost), avg_cost=str(cost / qty))
        return result
    balance = trade.get_real_balance()
    if balance is None:
        raise ValueError('balance_unavailable')
    qty = decimal(balance.get(coin, {}).get('total', 0))
    cash = decimal(balance.get('USDT', {}).get('free', 0))
    result = dict(quantity=str(qty), available_usdt=str(cash), cost_status='unknown')
    if qty:
        try:
            from clients.gate_readonly import GateReadOnly
            config = trade._load_env()
            reader = GateReadOnly(config.get('GATEIO_API_KEY', ''), config.get('GATEIO_API_SECRET', ''))
            result.update(cost_evidence(history(reader, coin), coin, qty, price))
            after = trade.get_real_balance()
            if after is None or decimal(after.get(coin, {}).get('total', 0)) != qty:
                raise ValueError('balance_changed')
        except Exception:
            result = dict(quantity=str(qty), available_usdt=str(cash), cost_status='unknown',
                          cost_reason='history_unavailable_or_balance_changed')
    from clients.execution_constraints import fetch_constraints
    result['execution_constraints'] = fetch_constraints(coin, balance.get(coin, {}).get('free', 0), price)
    return result


def position_text(coin, context):
    qty = decimal(context['quantity'])
    text = f"{coin}: 数量 {qty}；" + ('有持仓' if qty else '空仓')
    if context['cost_status'] in ('queried_history_estimate', 'paper_ledger'):
        text += f"；均摊成本 {context['avg_cost']} USDT；剩余成本 {context['cost_usdt']} USDT"
        if 'unrealized_usdt' in context:
            text += f"；浮动盈亏 {context['unrealized_usdt']} USDT（{context['unrealized_pct']}%）"
        text += '；成本来自已查询成交历史估算，不能排除历史划转；未扣未来卖出费用'
    else:
        text += '；成本未知，不得按零成本计算收益'
    text += '；' + context.get('execution_constraints', '成交门槛未提供')
    return text


def validate_account(context, action=None):
    qty, cash = decimal(context['quantity']), decimal(context['available_usdt'])
    if action == 'BUY' and cash <= 0:
        raise ValueError('no_cash_for_buy')
    if action == 'SELL' and qty <= 0:
        raise ValueError('no_position_for_sell')
    if action is None and cash <= 0 and qty <= 0:
        raise ValueError('empty_account')


class ReviewSchedule:
    """At most one attempt per coin per interval, persisted across restarts."""
    def __init__(self, path, interval=14400):
        if not math.isfinite(interval) or interval < 300:
            raise ValueError('invalid_interval')
        self.interval = interval
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.path = str(p)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reviews (coin TEXT PRIMARY KEY, attempted REAL)')

    def claim(self, coin, now):
        if not math.isfinite(now):
            raise ValueError('invalid_time')
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT attempted FROM reviews WHERE coin=?', (coin,)).fetchone()
            if row and now - row[0] < self.interval:
                return False
            db.execute('INSERT OR REPLACE INTO reviews VALUES (?,?)', (coin, now))
            return True

    def completed(self, coin, now):
        if not math.isfinite(now):
            raise ValueError('invalid_time')
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute('UPDATE reviews SET attempted=MAX(attempted,?) WHERE coin=?', (now, coin))


def review_cycle(watchlist, holdings, schedule, now, run, event):
    """Independent of shallow HOLD; cadence is enforced by the shared entry."""
    for coin, symbol in watchlist:
        try:
            if decimal(holdings.get(coin, {}).get('quantity', 0)) <= 0:
                continue
            scan_id = f'position-review-{coin}-{int(now)}'
            event('position_review_start', coin=coin, scan_id=scan_id)
            ok = run(coin, symbol, '定期持仓复评；无预设买卖结论', scan_id=scan_id,
                     review_only=False, trigger='position_review')
            event('position_review_result', coin=coin, scan_id=scan_id, success=ok is True)
        except Exception as exc:
            event('position_review_error', coin=coin, error_type=type(exc).__name__)
