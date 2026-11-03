"""Atomic paper ledger, FIFO net PnL and deterministic protection.

All thresholds/fees here are paper simulation assumptions, not live risk settings.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from clients.order_journal import database, canonical, number
from clients.trade_policy import validate_command
from clients.gate_readonly import pair_name

ZERO = Decimal(0)
LOT = Decimal('0.00000001')
CNY = timezone(timedelta(hours=8))
DEFAULT_POLICY = {'position_stop': '0.05', 'max_drawdown': '0.10', 'daily_loss': '0.03', 'max_quote_age': 60}


def accounting(rows, initial):
    cash = number(initial, True)
    lots, realized, fees = {}, ZERO, ZERO
    for row in rows:
        symbol = row.get('symbol') or row.get('base',row.get('coin','')) + '/USDT'
        pair_name(symbol)
        qty, price = number(row.get('quantity'), True), number(row.get('fill_price'), True)
        fee = number(row.get('fee_usdt', 0))
        fees += fee
        if row.get('action') == 'BUY':
            cost = qty * price + fee
            cash -= cost
            lots.setdefault(symbol, []).append([qty, cost / qty])
        elif row.get('action') == 'SELL':
            remaining, basis = qty, ZERO
            queue = lots.setdefault(symbol, [])
            while remaining and queue:
                used = min(remaining, queue[0][0]); remaining -= used
                basis += used * queue[0][1]; queue[0][0] -= used
                if queue[0][0] == 0: queue.pop(0)
            if remaining > Decimal('0.000000000001'):
                raise ValueError('ledger oversell')
            proceeds = qty * price - fee
            cash += proceeds; realized += proceeds - basis
        else:
            raise ValueError('invalid ledger action')
        if cash < Decimal('-0.00000001'):
            raise ValueError('ledger cash deficit')
    positions = {}
    for symbol, queue in lots.items():
        qty = sum((item[0] for item in queue), ZERO)
        cost = sum((item[0] * item[1] for item in queue), ZERO)
        if qty:
            positions[symbol] = {'quantity': str(qty), 'cost': str(cost), 'average': str(cost/qty)}
    return {'cash': str(max(cash,ZERO)), 'positions': positions, 'realized_net': str(realized),
            'fees_usdt': str(fees), 'revision': len(rows)}


def validate_policy(policy):
    if type(policy) is not dict or set(policy) != set(DEFAULT_POLICY):
        raise ValueError('invalid protection policy')
    for key in ('position_stop','max_drawdown','daily_loss'):
        if not ZERO < number(policy[key]) < 1:
            raise ValueError('invalid protection threshold')
    if type(policy['max_quote_age']) is not int or not 1 <= policy['max_quote_age'] <= 300:
        raise ValueError('invalid quote age')
    return dict(policy)


def fresh_quote(quotes, symbol, now, max_age):
    quote = quotes.get(symbol)
    if type(quote) is not dict:
        raise ValueError('missing quote')
    price, observed = number(quote.get('price'),True), number(quote.get('observed_at'),True)
    age = Decimal(str(now)) - observed
    if age < -2 or age > max_age:
        raise ValueError('stale quote')
    return price


class PaperAccount:
    def __init__(self, path, initial, legacy=None, fee_rate='0.001', slippage='0.001', policy=None, min_quote='5'):
        self.path = str(path)
        initial, fee, slip = number(initial,True), number(fee_rate), number(slippage)
        if fee >= Decimal('0.1') or slip >= Decimal('0.1'):
            raise ValueError('invalid simulation costs')
        config = {'initial':str(initial), 'fee_rate':str(fee), 'slippage':str(slip),
                  'min_quote':str(number(min_quote)), 'policy':validate_policy(policy or DEFAULT_POLICY)}
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS paper (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, ledger TEXT NOT NULL, risk TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS paper_results (key TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT config FROM paper WHERE id=1').fetchone()
            if row:
                if json.loads(row['config']) != config:
                    raise ValueError('paper configuration changed; explicit migration required')
            else:
                rows = []
                if legacy and os.path.lexists(legacy):
                    fd=os.open(legacy,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    with os.fdopen(fd,'r',encoding='utf-8') as stream:
                        info=os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                            raise PermissionError('untrusted legacy ledger')
                        rows=json.load(stream,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite ledger')))
                    if type(rows) is not list: raise ValueError('invalid legacy ledger')
                accounting(rows,initial)
                db.execute('INSERT INTO paper VALUES (1,?,?,?)',(canonical(config),canonical(rows),'{}'))

    def _read(self, db):
        row=db.execute('SELECT * FROM paper WHERE id=1').fetchone()
        return json.loads(row['config']),json.loads(row['ledger']),json.loads(row['risk'])

    def snapshot(self):
        with database(self.path) as db:
            config,rows,risk=self._read(db)
            return {**accounting(rows,config['initial']),'ledger':rows,'risk':risk}

    def _fill(self, config, rows, symbol, action, percentage, price, reason):
        state=accounting(rows,config['initial'])
        fee_rate,slip=number(config['fee_rate']),number(config['slippage'])
        execution_price=price*(1+slip if action=='BUY' else 1-slip)
        if action=='BUY':
            budget=number(state['cash'])*percentage/100
            qty=(budget/(execution_price*(1+fee_rate))).quantize(LOT,rounding=ROUND_DOWN)
        else:
            held=number(state['positions'].get(symbol,{}).get('quantity',0))
            qty=(held*percentage/100).quantize(LOT,rounding=ROUND_DOWN)
        notional=qty*execution_price
        if qty<=0 or notional<number(config['min_quote']):
            return {'success':False,'status':'paper_rejected','detail':'模拟数量或金额低于门槛，未成交','exchange_confirmed':False}
        fee=notional*fee_rate
        row={'time':datetime.now(CNY).isoformat(timespec='seconds'),'id':uuid.uuid4().hex,
             'coin':symbol.split('/')[0],'base':symbol.split('/')[0],'symbol':symbol,'action':action,
             'fill_price':str(execution_price),'quantity':str(qty),'amount_usdt':str(notional),
             'fee_usdt':str(fee),'paper':True,'reason':reason}
        rows.append(row)
        after=accounting(rows,config['initial'])
        pnl=Decimal(after['realized_net'])-Decimal(state['realized_net'])
        basis=notional-fee-pnl if action=='SELL' else ZERO
        row['pnl_usdt']=str(pnl)
        row['pnl_pct']=str(pnl/basis*100 if basis>0 else ZERO)
        return {'success':True,'status':'paper_filled','detail':'模拟成交，含模拟手续费与滑点',
                'fill_price':float(execution_price),'quantity':float(qty),'fee_usdt':float(fee),
                'pnl_usdt':float(pnl),'pnl_pct':float(row['pnl_pct']),'exchange_confirmed':False}

    def _protect(self, config, rows, risk, quotes, now):
        state=accounting(rows,config['initial']); policy=config['policy']; prices={}; stale=[]
        for symbol in state['positions']:
            try:prices[symbol]=fresh_quote(quotes,symbol,now,policy['max_quote_age'])
            except ValueError:stale.append(symbol)
        day=datetime.fromtimestamp(now,CNY).date().isoformat()
        equity=number(state['cash'])+sum((number(pos['quantity'])*prices[symbol]
                for symbol,pos in state['positions'].items() if symbol in prices),ZERO)
        if not stale:
            risk['peak']=str(max(number(risk.get('peak',equity)),equity))
            if risk.get('day')!=day:
                risk['day'],risk['day_open']=day,str(equity)
            peak,day_open=number(risk['peak']),number(risk['day_open'])
            if peak>0 and equity<=peak*(1-number(policy['max_drawdown'])):
                risk['halted']='max_drawdown'
            if day_open>0 and equity<=day_open*(1-number(policy['daily_loss'])):
                risk['halted']='daily_loss'
        exits=[]
        for symbol,pos in state['positions'].items():
            if symbol not in prices:continue
            price=prices[symbol]
            stop=price<=number(pos['average'])*(1-number(policy['position_stop']))
            if stop or risk.get('halted'):
                result=self._fill(config,rows,symbol,'SELL',100,price,'protective_exit')
                exits.append({'symbol':symbol,'reason':'position_stop' if stop else risk['halted'],**result})
                if not result['success']:risk['halted']='protective_exit_blocked'
        risk['checked_at']=now
        risk['stale_symbols']=stale
        return {'blocked_new_risk':bool(stale or risk.get('halted')),'exits':exits,
                'halted':risk.get('halted'),'stale_symbols':stale}

    def protect(self, quotes, now=None):
        now=time.time() if now is None else now
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE'); config,rows,risk=self._read(db)
            result=self._protect(config,rows,risk,quotes,now)
            db.execute('UPDATE paper SET ledger=?,risk=? WHERE id=1',(canonical(rows),canonical(risk)))
            return result

    def place(self, symbol, action, percentage, quotes, key=None, now=None):
        pair_name(symbol)
        validate_command({'action':action,'percentage':percentage,'reason':'paper executor'})
        now=time.time() if now is None else now
        key=key or uuid.uuid4().hex
        if type(key) is not str or not 1<=len(key)<=200:raise ValueError('invalid request key')
        request=canonical({'symbol':symbol,'action':action,'percentage':percentage})
        with database(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM paper_results WHERE key=?',(key,)).fetchone()
            if old:
                if old['request']!=request:raise ValueError('idempotency key conflict')
                return json.loads(old['result'])
            config,rows,risk=self._read(db)
            protection=self._protect(config,rows,risk,quotes,now)
            try:
                price=fresh_quote(quotes,symbol,now,config['policy']['max_quote_age'])
            except ValueError:
                result={'success':False,'status':'paper_rejected','detail':'行情缺失或过期','exchange_confirmed':False}
            else:
                if action=='BUY' and protection['blocked_new_risk']:
                    result={'success':False,'status':'paper_rejected','detail':'风险熔断或行情缺失，禁止加仓','exchange_confirmed':False}
                elif action=='HOLD':result={'success':True,'status':'hold','exchange_confirmed':False}
                else:result=self._fill(config,rows,symbol,action,percentage,price,'strategy')
            result['protection']=protection
            db.execute('UPDATE paper SET ledger=?,risk=? WHERE id=1',(canonical(rows),canonical(risk)))
            db.execute('INSERT INTO paper_results VALUES (?,?,?)',(key,request,canonical(result)))
            return result
