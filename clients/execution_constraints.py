"""GET-only, indicative execution constraints. No trading recommendations."""
from decimal import Decimal, ROUND_DOWN


def constraints_text(row, free_qty, price):
    qty, price = Decimal(str(free_qty)), Decimal(str(price))
    minimum = Decimal(str(row['min_quote_amount']))
    base_min = Decimal(str(row['min_base_amount']))
    precision = int(row['amount_precision'])
    if not all(x.is_finite() and x >= 0 for x in (qty,price,minimum,base_min)) or price <= 0 or not 0 <= precision <= 18:
        raise ValueError('invalid_constraints')
    step = Decimal(1).scaleb(-precision)
    eligible = []
    for pct in range(1,101):
        amount = (qty * pct / 100).quantize(step, rounding=ROUND_DOWN)
        if amount > 0 and amount >= base_min and amount * price >= minimum:
            eligible.append(pct)
    feasible = f'{eligible[0]}%–100%' if eligible else '无（当前可用余额不足最低订单要求）'
    return (f'交易所最低金额 {minimum} USDT，最低数量 {base_min}，数量小数位 {precision}；'
            f'可用数量 {qty}；按当前价格和整数百分比估算，可执行卖出比例 {feasible}。'
            '此范围仅说明成交门槛，不是卖出建议；不得为凑最低金额提高原定卖出比例。'
            '行情与余额会变化，最终由提交前检查决定；未计费用和滑点。')


def fetch_constraints(coin, free_qty, price):
    try:
        from clients.gate_readonly import GateReadOnly
        rows = GateReadOnly()._get('/spot/currency_pairs')
        row = next(r for r in rows if r['id'] == coin + '_USDT')
        return constraints_text(row, free_qty, price)
    except Exception:
        return '交易所成交门槛暂不可用；不得猜测可执行比例，最终以提交前检查为准。'
