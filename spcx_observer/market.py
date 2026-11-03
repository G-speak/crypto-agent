"""Public Gate stock-only feed. Persistent closed bars; never sends private requests."""
import json,math,sqlite3,time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
SYMBOL='US_STOCK_SPCX_USDT'
WS='wss://api.gateio.ws/ws/crossex/public'
NY=ZoneInfo('America/New_York')

def details():
    r=requests.get('https://api.gateio.ws/api/v4/stock/symbols/detail',params={'symbols':'SPCX','exchange':'us'},timeout=(5,12),allow_redirects=False)
    r.raise_for_status();data=r.json()
    row=data['data']['list']
    if len(row)!=1 or row[0]['symbol']!='SPCX' or row[0]['asset_type']!='STOCK':raise ValueError('wrong_asset')
    if not 0<=time.time()-float(data['timestamp'])/1000<=300:raise ValueError('stale_gate_metadata')
    return row[0]

def database(path):
    db=sqlite3.connect(path,timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS bars(t INTEGER PRIMARY KEY, end REAL, o REAL,h REAL,l REAL,c REAL,v REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY CHECK(id=1), received REAL, message TEXT)')
    return db

def regular(ts):
    d=datetime.fromtimestamp(ts,NY)
    return d.weekday()<5 and 570<=d.hour*60+d.minute<960

def parse_bar(message,now):
    if message.get('event')!='update' or message.get('channel')!='kline_1h':return None
    r=message['result']
    if r.get('s')!=SYMBOL:raise ValueError('wrong_symbol')
    if r.get('x') is not True and not (type(r.get('x')) is int and r['x']==1):return None
    t,end=float(r['t'])/1000,float(r['T'])/1000
    if not all(math.isfinite(x) for x in (t,end)) or not 3598<=end-t<=3600 or not 0<=now-end<=300:raise ValueError('invalid_bar_time')
    if not regular(t) or not regular(end):return None
    if datetime.fromtimestamp(t,NY).date()!=datetime.fromtimestamp(end,NY).date():return None
    vals=[float(r[k]) for k in ('o','h','l','c','v')]
    if not all(math.isfinite(v) and v>0 for v in vals):raise ValueError('invalid_bar_value')
    o,h,l,c,v=vals
    if not l<=min(o,c)<=max(o,c)<=h:raise ValueError('invalid_ohlc')
    return (int(t),end,o,h,l,c,v)

def save(db,bar):
    old=db.execute('SELECT * FROM bars WHERE t=?',(bar[0],)).fetchone()
    if old is not None and tuple(old)!=tuple(bar):raise ValueError('conflicting_closed_bar')
    with db:
        db.execute('INSERT OR IGNORE INTO bars VALUES(?,?,?,?,?,?,?)',bar)
        db.execute('DELETE FROM bars WHERE t NOT IN (SELECT t FROM bars ORDER BY t DESC LIMIT 500)')

def probe():
    import websocket
    meta=details()
    ws=websocket.create_connection(WS,timeout=12)
    try:
        ws.send(json.dumps({'time':int(time.time()),'channel':'kline_1h','event':'subscribe','payload':[SYMBOL]}))
        msg=json.loads(ws.recv())
        if msg.get('event')!='subscribe' or msg.get('channel')!='kline_1h' or msg.get('result',{}).get('status')!='success':raise ValueError('subscription_not_acknowledged')
    finally:ws.close()
    return {'symbol':'SPCX','session':meta.get('trade_status'),'subscription':'acknowledged','historical_bars':'collect_after_deployment','orders':0}

def collect(path,stop,log):
    import websocket
    db=database(path)
    while not stop.is_set():
        ws=None
        try:
            ws=websocket.create_connection(WS,timeout=10)
            ws.send(json.dumps({'time':int(time.time()),'channel':'kline_1h','event':'subscribe','payload':[SYMBOL]}))
            ping=time.monotonic()
            while not stop.is_set():
                if time.monotonic()-ping>=20:ws.ping();ping=time.monotonic()
                try:raw=ws.recv()
                except websocket.WebSocketTimeoutException:continue
                if not raw:raise ValueError('feed_closed')
                now=time.time();msg=json.loads(raw)
                if msg.get('error'):raise ValueError('feed_error')
                bar=parse_bar(msg,now)
                if bar:
                    # A wall-clock guess alone cannot certify an exchange session.
                    if details().get('trade_status')=='open':save(db,bar)
                with db:db.execute('INSERT OR REPLACE INTO health VALUES(1,?,?)',(now,'connected'))
        except Exception as exc:
            log.error('feed_error type=%s',type(exc).__name__)
            with db:db.execute('INSERT OR REPLACE INTO health VALUES(1,?,?)',(time.time(),'error:'+type(exc).__name__))
        finally:
            if ws is not None:ws.close()
        stop.wait(30)
    db.close()
