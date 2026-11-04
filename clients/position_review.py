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


DUST_USDT = 3.0   # 与 Gate spot cost.min 对齐；低于该估值的持仓视为不可交易灰尘


def is_dust_position(quantity, price):
    """灰尘判定：有数量但按现价估值不足交易所最小下单额。价格未知时不判定（fail-open）。"""
    try:
        q, p = decimal(quantity), decimal(price)
    except Exception:
        return False
    return bool(q > 0 and p > 0 and q * p < decimal(str(DUST_USDT)))


def _allocation_context(result, coin, balance=None, account=None):
    from clients import gateio_trade as trade
    try:
        if account is not None:
            positions=account['positions']; quotes=trade._quotes_for(trade._paper_account())
            from clients.paper_account import fresh_quote
            values={p.split('/')[0]:decimal(pos['quantity'])*fresh_quote(quotes,p,time.time(),60)
                    for p,pos in positions.items()}
            total=decimal(account['cash'])+sum(values.values(),Decimal(0))
            snap={'total':total,'asset_values':values}
        else:
            snap=trade.calculate_total_net_worth(balance=balance)
        total=snap['total']; value=snap['asset_values'].get(coin,Decimal(0))
        if total<=0: raise ValueError('empty_account')
        result.update(valuation_status='ok', net_worth_usdt=str(total),
                      market_value_usdt=str(value), exposure_pct=str(value/total*100))
    except Exception:
        result.update(valuation_status='unknown')
    return result


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
        result['is_dust'] = is_dust_position(qty, price)
        return _allocation_context(result,coin,account=account)
    balance = trade.get_real_balance()
    if balance is None:
        raise ValueError('balance_unavailable')
    qty = decimal(balance.get(coin, {}).get('total', 0))
    cash = decimal(balance.get('USDT', {}).get('free', 0))
    result = dict(quantity=str(qty), available_usdt=str(cash), cost_status='unknown')
    dust = is_dust_position(qty, price)
    result['is_dust'] = dust
    if dust:
        # 灰尘不查历史成本：既卖不掉，也不该参与决策，省一次交易历史查询
        result.update(cost_status='dust', cost_reason='below_min_notional')
    elif qty:
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
    result['is_dust'] = dust
    from clients.execution_constraints import fetch_constraints
    result['execution_constraints'] = fetch_constraints(coin, balance.get(coin, {}).get('free', 0), price)
    return _allocation_context(result,coin,balance=balance)


def position_text(coin, context):
    if context.get('valuation_status')=='ok':
        allocation=(f"；持仓市值约 {decimal(context['market_value_usdt']):.4f} USDT"
                    f"（占现货账户总净值 {decimal(context['exposure_pct']):.2f}%）"
                    f"；全账户净值 {decimal(context['net_worth_usdt']):.4f} USDT（含冻结余额与灰尘）")
    else:
        allocation='；全账户估值未知，不得猜测仓位占比；执行器须重新估值后方可 BUY'
    allocation+='；新增BUY限单币60%且保留20%可用USDT，不自动卖出'
    qty = decimal(context['quantity'])
    if context.get('is_dust'):
        return (f"{coin}: 空仓（无有效持仓；账户另有不可交易灰尘 {qty}，"
                f"估值 < {DUST_USDT:g} USDT，决策时按空仓处理，可正常 BUY，严禁 SELL）" + allocation)
    text = f"{coin}: 数量 {qty}；" + ('有持仓' if qty else '空仓')
    if context['cost_status'] in ('queried_history_estimate', 'paper_ledger'):
        text += f"；均摊成本 {context['avg_cost']} USDT；剩余成本 {context['cost_usdt']} USDT"
        if 'unrealized_usdt' in context:
            text += f"；浮动盈亏 {context['unrealized_usdt']} USDT（{context['unrealized_pct']}%）"
        text += '；成本来自已查询成交历史估算，不能排除历史划转；未扣未来卖出费用'
    else:
        text += '；成本未知，不得按零成本计算收益'
    text += '；' + context.get('execution_constraints', '成交门槛未提供')
    return text + allocation


def validate_account(context, action=None):
    if action == 'SELL' and context.get('is_dust'):
        raise ValueError('dust_position_cannot_sell')
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


_DUST_LOGGED = set()


def review_cycle(watchlist, holdings, schedule, now, run, event):
    """Independent of shallow HOLD; cadence is enforced by the shared entry."""
    for coin, symbol in watchlist:
        try:
            qty = holdings.get(coin, {}).get('quantity', 0)
            if decimal(qty) <= 0:
                continue
            # 灰尘仓位：估值不足交易所最小下单额 → 决策层视同空仓，跳过定期复评
            try:
                from clients.gateio_trade import _fetch_current_price
                dust_price = _fetch_current_price(symbol.upper().replace('USDT', '/USDT'))
            except Exception:
                dust_price = 0
            if is_dust_position(qty, dust_price):
                if coin not in _DUST_LOGGED:
                    _DUST_LOGGED.add(coin)
                    event('position_review_dust_skipped', coin=coin,
                          detail='估值低于交易所最小下单额，按空仓处理，不再定期复评')
                continue
            scan_id = f'position-review-{coin}-{int(now)}'
            event('position_review_start', coin=coin, scan_id=scan_id)
            ok = run(coin, symbol, '定期持仓复评；无预设买卖结论', scan_id=scan_id,
                     review_only=False, trigger='position_review')
            event('position_review_result', coin=coin, scan_id=scan_id, success=ok is True)
        except Exception as exc:
            event('position_review_error', coin=coin, error_type=type(exc).__name__)
