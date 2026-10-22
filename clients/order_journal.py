"""Durable reconciliation of existing orders; intentionally cannot submit orders."""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from clients.gate_readonly import pair_name


def number(value, positive=False):
    if type(value) not in (str, int, float, Decimal):
        raise ValueError('invalid number')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('invalid number') from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError('invalid number')
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def normalize(order, trades, order_id, symbol, side):
    """Accept only consistent private order and complete private trade evidence."""
    pair = pair_name(symbol)
    if (type(order) is not dict or type(trades) is not list or str(order.get('id')) != order_id
            or order.get('currency_pair') != pair or order.get('side') != side
            or order.get('account') != 'spot' or order.get('trade_quote', 'USDT') != 'USDT'):
        raise ValueError('order identity mismatch')
    filled = number(order.get('filled_amount'))
    cost = number(order.get('filled_total'))
    remaining = number(order.get('left'))
    result, seen = [], {}
    for trade in trades:
        if (type(trade) is not dict or str(trade.get('order_id')) != order_id
                or trade.get('currency_pair') != pair or trade.get('side') != side
                or trade.get('trade_quote', 'USDT') != 'USDT'):
            raise ValueError('trade identity mismatch')
        trade_id = str(trade.get('id', ''))
        if not trade_id.isdigit() or len(trade_id) > 32:
            raise ValueError('invalid trade id')
        qty, price = number(trade.get('amount'), True), number(trade.get('price'), True)
        fee, currency = number(trade.get('fee')), trade.get('fee_currency')
        if type(currency) is not str or not currency.isalnum() or len(currency) > 20:
            raise ValueError('invalid fee currency')
        fees = {currency: str(fee)}
        for field, unit in (('gt_fee', 'GT'), ('point_fee', 'POINT')):
            extra = number(trade.get(field, '0'))
            if extra:
                fees[unit] = str(number(fees.get(unit, '0')) + extra)
        item = {'id': trade_id, 'order_id': order_id, 'symbol': symbol, 'side': side,
                'quantity': str(qty), 'price': str(price), 'cost': str(qty * price), 'fees': fees}
        if trade_id in seen:
            if seen[trade_id] != item:
                raise ValueError('conflicting duplicate trade')
            continue
        seen[trade_id] = item
        result.append(item)
    result.sort(key=lambda item: int(item['id']))
    trade_qty = sum((number(t['quantity']) for t in result), Decimal(0))
    trade_cost = sum((number(t['cost']) for t in result), Decimal(0))
    if abs(trade_qty - filled) > Decimal('0.000000000001') or abs(trade_cost - cost) > Decimal('0.00000001'):
        raise ValueError('incomplete or inconsistent fill evidence')
    status, finish = order.get('status'), order.get('finish_as')
    if status == 'open' and finish in (None, '', 'open'):
        state = 'partially_filled' if filled else 'open'
    elif status == 'closed' and finish == 'filled' and filled > 0 and remaining == 0:
        state = 'filled'
    elif status in ('cancelled', 'closed') and finish in ('cancelled', 'ioc', 'stp', 'unified_check_failed'):
        state = 'cancelled'
    else:
        raise ValueError('unsupported or ambiguous order status')
    if bool(filled) != bool(cost):
        raise ValueError('invalid fill totals')
    average = cost / filled if filled else Decimal(0)
    fees = {}
    for trade in result:
        for currency, fee in trade['fees'].items():
            fees[currency] = str(number(fees.get(currency, '0')) + number(fee))
    return {'state': state, 'quantity': str(filled), 'cost': str(cost), 'average': str(average),
            'fees': fees, 'trades': result, 'fully_filled': state == 'filled',
            'exchange_confirmed': filled > 0}


@contextmanager
def database(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError('untrusted state directory')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError('untrusted state file')
    finally:
        os.close(fd)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA synchronous=FULL')
        with db:
            yield db
    finally:
        db.close()


class OrderJournal:
    def __init__(self, path):
        self.path = str(path)
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS intents (key TEXT PRIMARY KEY, account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, request TEXT NOT NULL, order_id TEXT, state TEXT NOT NULL, evidence TEXT, error TEXT, updated REAL NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS venue_order ON intents(account,symbol,order_id)')
            db.execute('CREATE TABLE IF NOT EXISTS fills (account TEXT NOT NULL, symbol TEXT NOT NULL, trade_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account,symbol,trade_id))')

    def prepare(self, key, account, symbol, side, request):
        pair_name(symbol)
        if (type(key) is not str or not 1 <= len(key) <= 128 or not account
                or side not in ('buy', 'sell') or type(request) is not dict):
            raise ValueError('invalid intent')
        payload = canonical(request)
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM intents WHERE key=?', (key,)).fetchone()
            if row:
                if (row['account'],row['symbol'],row['side'],row['request']) != (account,symbol,side,payload):
                    raise ValueError('idempotency key conflict')
            else:
                db.execute('INSERT INTO intents VALUES (?,?,?,?,?,NULL,?,NULL,NULL,?)',
                           (key,account,symbol,side,payload,'prepared',time.time()))
        return self.get(key)

    def attach(self, key, order_id):
        if type(order_id) is not str or not order_id.isdigit() or len(order_id) > 32:
            raise ValueError('invalid venue id')
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM intents WHERE key=?',(key,)).fetchone()
            if not row or (row['order_id'] and row['order_id'] != order_id):
                raise ValueError('order identity conflict')
            if row['order_id'] is None:
                db.execute('UPDATE intents SET order_id=?, state=?, updated=? WHERE key=?',
                           (order_id,'submitted',time.time(),key))
        return self.get(key)

    def get(self, key):
        with database(self.path) as db:
            row = db.execute('SELECT * FROM intents WHERE key=?',(key,)).fetchone()
            if not row:
                raise ValueError('unknown intent')
            out = dict(row)
            out['evidence'] = json.loads(out['evidence']) if out['evidence'] else None
            return out

    def unknown(self, key):
        with database(self.path) as db:
            db.execute('UPDATE intents SET state=?,error=?,updated=? WHERE key=?',
                       ('unknown','reconciliation_required',time.time(),key))
        return self.get(key)

    def observe(self, key, order, trades):
        row = self.get(key)
        evidence = normalize(order,trades,row['order_id'],row['symbol'],row['side'])
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT * FROM intents WHERE key=?',(key,)).fetchone()
            old = json.loads(latest['evidence']) if latest['evidence'] else None
            if old:
                if number(evidence['quantity']) < number(old['quantity']):
                    raise ValueError('fill regression')
                if old['state'] in ('filled','cancelled') and evidence != old:
                    raise ValueError('terminal evidence changed')
                old_trades = {t['id']:t for t in old['trades']}
                new_trades = {t['id']:t for t in evidence['trades']}
                if any(new_trades.get(k) != v for k,v in old_trades.items()):
                    raise ValueError('previous fill mutated')
            for trade in evidence['trades']:
                existing = db.execute('SELECT payload FROM fills WHERE account=? AND symbol=? AND trade_id=?',
                                      (row['account'],row['symbol'],trade['id'])).fetchone()
                payload = canonical(trade)
                if existing and existing['payload'] != payload:
                    raise ValueError('fill identity conflict')
                db.execute('INSERT OR IGNORE INTO fills VALUES (?,?,?,?)',
                           (row['account'],row['symbol'],trade['id'],payload))
            db.execute('UPDATE intents SET state=?,evidence=?,error=NULL,updated=? WHERE key=?',
                       (evidence['state'],canonical(evidence),time.time(),key))
        return self.get(key)

    def reconcile(self, key, reader):
        row = self.get(key)
        if not row['order_id']:
            return self.unknown(key)
        try:
            first = reader.order(row['order_id'],row['symbol'])
            trades = reader.trades(row['order_id'],row['symbol'])
            second = reader.order(row['order_id'],row['symbol'])
            fields = ('id','currency_pair','side','account','status','finish_as','filled_amount','filled_total','left')
            if any(first.get(f) != second.get(f) for f in fields):
                raise ValueError('unstable order snapshot')
            return self.observe(key,second,trades)
        except Exception:
            return self.unknown(key)

    def unresolved(self, account, symbol):
        with database(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM intents WHERE account=? AND symbol=? AND state NOT IN ('filled','cancelled','rejected')",
                              (account,symbol)).fetchone()[0]

    def fills(self, account):
        with database(self.path) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM fills WHERE account=? ORDER BY symbol,trade_id',(account,))]
