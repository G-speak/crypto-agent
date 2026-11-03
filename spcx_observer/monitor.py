"""Independent stock candidate evaluation; no AI, messaging or execution."""
from datetime import datetime,timezone
from pathlib import Path
import time
import math
from market import details,database,NY,regular
ROOT=Path(__file__).resolve().parent

def evaluate(bars,meta,now):
    result={'symbol':'SPCX','status':'暂不评估','checked_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),
            'rule_version':'gate-closed-hour-v2','win_rate':'未经验证','execution':'仅观察，不下单','bars':len(bars)}
    def blocked(reason):result['reason']=reason;return result
    if meta.get('trade_status')!='open' or not regular(now):return blocked('非已确认常规交易时段')
    if len(bars)<60:return blocked('积累有效完整小时线：'+str(len(bars))+'/60')
    bars=bars[-60:]
    for bar in bars:
        if len(bar)!=7 or not all(math.isfinite(x) for x in bar) or any(x<=0 for x in bar[2:]):return blocked('K线数值非法')
        if not bar[4]<=min(bar[2],bar[5])<=max(bar[2],bar[5])<=bar[3]:return blocked('K线价格关系非法')
    # Require sequential hours within each day; reset readiness on large outages.
    for a,b in zip(bars,bars[1:]):
        if b[0]<=a[0] or b[0]-a[0]>4*86400:return blocked('K线乱序或跨越长时间数据缺口')
        if datetime.fromtimestamp(a[0],NY).date()==datetime.fromtimestamp(b[0],NY).date() and b[0]-a[0]!=3600:return blocked('日内完整K线缺失')
    if not 0<=now-bars[-1][1]<=4500:return blocked('最新已收盘K线过旧或时间超前')
    closes=[b[5] for b in bars];last,prev=bars[-1],bars[-2]
    ma20=sum(closes[-20:])/20;ma50=sum(closes[-50:])/50;old=sum(closes[-21:-1])/20
    vr=last[6]/(sum(b[6] for b in bars[-21:-1])/20)
    result.update(close=last[5],ma20=ma20,ma50=ma50,volume_ratio=vr,bar_timestamp=last[0],
                  signal_id='SPCX:'+str(last[0]),source='Gate股票公开已收盘小时K线',
                  fee_rate=meta.get('commission_rate'),platform_slippage_rate=meta.get('slippage_rate'),
                  min_quantity=meta.get('min_order_volume'))
    candidate=(last[5]>ma50 and ma20>ma50 and ma20>old and prev[5]<=old and last[5]>ma20 and last[5]>prev[3] and vr>=1.2)
    result.update(status='候选机会，待人工研究' if candidate else '继续观察',
                  reason='趋势回调恢复与放量同时满足，仍需核对新闻财报和可执行价差' if candidate else '趋势、回调恢复与放量尚未同时满足')
    return result

def check():
    try:
        meta=details();path=ROOT/'state'/'market.sqlite3'
        if not path.exists():return {'symbol':'SPCX','status':'暂不评估','reason':'尚未积累股票K线','bars':0}
        db=database(path)
        try:
            bars=db.execute('SELECT * FROM bars ORDER BY t').fetchall()
            health=db.execute('SELECT message FROM health WHERE id=1').fetchone()
        finally:db.close()
        if health and health[0].startswith('error:'):return {'symbol':'SPCX','status':'暂不评估','reason':'行情采集异常：'+health[0]}
        return evaluate(bars,meta,time.time())
    except Exception as exc:return {'symbol':'SPCX','status':'暂不评估','reason':'行情校验失败：'+type(exc).__name__}

if __name__=='__main__':
    import json
    print(json.dumps(check(),ensure_ascii=False))
