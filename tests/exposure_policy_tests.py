"""Offline behavioral coverage: money, precision, serialization, crash latch, prompts."""
import sys, os, time, tempfile, threading, types, unittest, ast
from pathlib import Path
from decimal import Decimal, ROUND_DOWN
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from clients import exposure_policy as policy, gateio_trade as gt
from clients.execution_lane import execution_lane
from clients.committee_coordination import ExecutionGuard, NO_SUBMISSION
from clients.paper_account import PaperAccount
from clients.order_journal import database

D=Decimal

def balance(cash=100, assets=None, locked=0):
    rows={'USDT':{'free':cash,'used':locked,'total':cash+locked}}
    for asset, qty in (assets or {}).items():rows[asset]={'free':qty,'used':0,'total':qty}
    return rows

class FakeExchange:
    def __init__(self, balances=None):
        self.balances=balances or balance();self.calls=[];self.rate='0.001'
        self.price=100;self.stale=False;self.fail_asset=None;self.pending=False
        self.minimum=3;self.lot=D('0.001');self.failure=False;self.open=[]
        self.markets={p:{'limits':{'cost':{'min':3},'amount':{'min':.001}}} for p in ['BTC/USDT','ETH/USDT','SOL/USDT']}
    def load_markets(self,*a):
        for row in self.markets.values():row['limits']['cost']['min']=self.minimum
    def fetch_balance(self):return self.balances
    def fetch_open_orders(self):return self.open
    def fetch_ticker(self,symbol):
        if symbol.startswith(str(self.fail_asset)+'/'):raise TimeoutError('fixture')
        return {'last':self.price,'timestamp':(time.time()-(90 if self.stale else 0))*1000}
    def fetch_order_book(self,symbol,limit=5):
        t=self.fetch_ticker(symbol)
        return {'timestamp':t['timestamp'],'bids':[[t['last'],1]],'asks':[[t['last'],1]]}
    def fetch_trading_fee(self,symbol):return {'taker':self.rate,'percentage':True}
    def amount_to_precision(self,symbol,value):return str(D(str(value)).quantize(self.lot,rounding=ROUND_DOWN))
    def cost_to_precision(self,symbol,value):return str(D(str(value)).quantize(D('.01'),rounding=ROUND_DOWN))
    def create_market_order(self,**kw):
        self.calls.append(kw)
        if self.failure:raise TimeoutError('unknown after submit')
        if kw['side']=='buy':
            cost=D(str(kw['params']['cost']));qty=cost/D(str(self.price));coin=kw['symbol'].split('/')[0]
            fee=max(D(self.rate),D('0.005'))
            c=self.balances['USDT'];c['free']=float(D(str(c['free']))-cost*(1+fee));c['total']=c['free']+c['used']
            row=self.balances.setdefault(coin,{'free':0,'total':0,'used':0})
            row['free']=row['total']=float(D(str(row['total']))+qty)
        return {'id':'fake-'+str(len(self.calls))}
    def fetch_order(self,oid,symbol):return {'id':oid,'status':'open' if self.pending else 'closed','filled':self.calls[-1]['amount'],'average':100,'cost':self.calls[-1]['amount']*100}

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=patch.dict(os.environ,{'HOME':self.temp.name});self.home.start();self.addCleanup(self.home.stop)
        self.ex=FakeExchange();self.env=patch.object(gt,'_load_env',return_value={'GATEIO_API_KEY':'FAKE','GATEIO_API_SECRET':'FAKE'});self.env.start();self.addCleanup(self.env.stop)
        self.ccxt=patch.dict(sys.modules,{'ccxt':types.SimpleNamespace(gate=lambda *a,**k:self.ex)})
        self.ccxt.start();self.addCleanup(self.ccxt.stop)
        self.sleep=patch.object(gt.time,'sleep',return_value=None);self.sleep.start();self.addCleanup(self.sleep.stop)
    def buy(self,coin='BTC',percentage=50,key='test-1'):
        return gt._live_trade(coin+'/USDT',coin,'BUY',10,percentage=percentage,client_oid=key)
    def test_existing_position_can_buy(self):
        self.ex.balances=balance(90,{'BTC':.1})
        r=self.buy();self.assertTrue(r['success']);self.assertEqual(len(self.ex.calls),1)
    def test_at_sixty_rejected_no_submission(self):
        self.ex.balances=balance(40,{'BTC':.6})
        r=self.buy();self.assertEqual(r['status'],'max_exposure_reached');self.assertFalse(self.ex.calls)
        self.assertIn(r['status'],NO_SUBMISSION)
    def test_clip_sixty_with_fee_denominator(self):
        self.ex.balances=balance(50,{'BTC':.5})
        r=self.buy();self.assertTrue(r['success']);cost=D(str(self.ex.calls[0]['params']['cost']))
        self.assertLess(cost,D('10'));self.assertGreater(cost,D('9'))
        snap=policy.calculate_total_net_worth(self.ex.balances,self.ex.fetch_ticker)
        self.assertLessEqual(snap['asset_values']['BTC']/snap['total'],D('.60'))
    def test_multi_coin_cash_clip(self):
        self.ex.balances=balance(25,{'ETH':.75})
        r=self.buy();self.assertTrue(r['success']);self.assertLessEqual(D(str(self.ex.calls[0]['params']['cost'])),D('5')/D('1.005'))
        snap=policy.calculate_total_net_worth(self.ex.balances,self.ex.fetch_ticker)
        self.assertGreaterEqual(snap['free_usdt']/snap['total'],D('.20'))
    def test_cash_at_floor_rejected(self):
        self.ex.balances=balance(20,{'ETH':.8})
        self.assertEqual(self.buy()['status'],'cash_reserve_reached');self.assertFalse(self.ex.calls)
    def test_clip_below_dynamic_min(self):
        self.ex.balances=balance(24,{'ETH':.76});self.ex.minimum=6
        self.assertEqual(self.buy()['status'],'below_min_cost');self.assertFalse(self.ex.calls)
    def test_rounded_base_min_rechecked(self):
        self.ex.lot=D('1')
        self.assertEqual(self.buy()['status'],'zero_amount');self.assertFalse(self.ex.calls)
    def test_other_asset_quote_missing(self):
        self.ex.balances=balance(90,{'ETH':.1});self.ex.fail_asset='ETH'
        self.assertEqual(self.buy()['status'],'valuation_unavailable');self.assertFalse(self.ex.calls)
    def test_gate_ticker_without_timestamp_still_buys_with_fresh_book(self):
        original=self.ex.fetch_ticker
        def no_stamp(pair):
            t=original(pair);t['timestamp']=None;return t
        self.ex.fetch_ticker=no_stamp
        self.ex.fetch_order_book=lambda *a,**k:{'timestamp':time.time()*1000,'bids':[[100,1]],'asks':[[100,1]]}
        self.assertTrue(self.buy()['success'])
    def test_book_missing_timestamp_rejected(self):
        self.ex.fetch_order_book=lambda *a,**k:{'bids':[[100,1]],'asks':[[100,1]]}
        self.assertEqual(self.buy()['status'],'valuation_unavailable');self.assertFalse(self.ex.calls)
    def test_book_crossed_rejected(self):
        self.ex.fetch_order_book=lambda *a,**k:{'timestamp':time.time()*1000,'bids':[[101,1]],'asks':[[100,1]]}
        self.assertEqual(self.buy()['status'],'valuation_unavailable')
    def test_book_wide_rejected(self):
        self.ex.fetch_order_book=lambda *a,**k:{'timestamp':time.time()*1000,'bids':[[100,1]],'asks':[[105,1]]}
        self.assertEqual(self.buy()['status'],'valuation_unavailable')
    def test_stale_quote(self):
        self.ex.stale=True
        self.assertEqual(self.buy()['status'],'valuation_unavailable');self.assertFalse(self.ex.calls)
    def test_quote_nan(self):
        self.ex.price=float('nan')
        self.assertEqual(self.buy()['status'],'valuation_unavailable');self.assertFalse(self.ex.calls)
    def test_open_orders_block(self):
        self.ex.open=[{'id':'old'}]
        self.assertEqual(self.buy()['status'],'open_orders_pending');self.assertFalse(self.ex.calls)
    def test_fee_above_floor(self):
        self.ex.balances=balance(25,{'ETH':.75});self.ex.rate='0.01'
        r=self.buy();self.assertEqual(r['allocation']['fee_reserve_rate'],'0.01')
        snap=policy.calculate_total_net_worth(self.ex.balances,self.ex.fetch_ticker)
        self.assertGreaterEqual(snap['free_usdt']/snap['total'],D('.20'))
    def test_sequential_buy_revalues(self):
        self.assertTrue(self.buy(key='a')['success']);self.assertTrue(self.buy('ETH',key='b')['success'])
        self.assertTrue(self.buy('SOL',key='c')['success'])
        snap=policy.calculate_total_net_worth(self.ex.balances,self.ex.fetch_ticker)
        self.assertGreaterEqual(snap['free_usdt']/snap['total'],D('.20'))
        self.assertTrue(all(v/snap['total']<=D('.60') for a,v in snap['asset_values'].items() if a!='USDT'))
    def test_duplicate_key_no_second_submission(self):
        self.assertTrue(self.buy()['success']);r=self.buy()
        self.assertEqual(r['status'],'duplicate_submission');self.assertEqual(len(self.ex.calls),1)
    def test_timeout_latches_across_calls(self):
        self.ex.failure=True
        self.assertEqual(self.buy()['status'],'exception')
        self.assertEqual(self.buy('ETH',key='b')['status'],'allocation_pending');self.assertEqual(len(self.ex.calls),1)
    def test_pending_follows_proven_committee_terminal(self):
        import json
        self.ex.failure=True;self.buy(key='t-proven')
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        g=ExecutionGuard(path);g.prepare('BTC/USDT','BUY',20,'old')
        evidence={'state':'filled','exchange_confirmed':True,'quantity':'0.1','cost':'10','average':'100','trades':[{'id':'fixture-fill','quantity':'0.1'}]}
        with database(path) as db:db.execute("UPDATE intents SET client_oid=?,state='filled',order_id='123',evidence=?",('t-proven',json.dumps(evidence)))
        self.ex.failure=False
        self.assertTrue(self.buy('ETH',key='t-new')['success'])
    def test_pending_does_not_follow_unproven_cancel_annotation(self):
        self.ex.failure=True;self.buy(key='t-unproven')
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        g=ExecutionGuard(path);g.prepare('BTC/USDT','BUY',20,'old')
        with database(path) as db:db.execute("UPDATE intents SET client_oid=?,state='cancelled',error='no_fill_found'",('t-unproven',))
        self.assertEqual(self.buy('ETH',key='t-new')['status'],'allocation_pending')
    def test_partial_open_keeps_latch(self):
        self.ex.pending=True
        self.assertEqual(self.buy()['status'],'unconfirmed')
        self.assertEqual(self.buy('ETH',key='b')['status'],'allocation_pending')
    def test_guard_safe_rejections_terminal(self):
        for i,status in enumerate(['valuation_unavailable','cash_reserve_reached','max_exposure_reached','below_min_cost']):
            g=ExecutionGuard(Path(self.temp.name)/('guard'+str(i)+'.sqlite3'))
            g.prepare('BTC/USDT','BUY',20,'key'+str(i));g.observe({'status':status})
            self.assertFalse(g.blocked())
    def test_existing_unknown_guard_preserved(self):
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        g=ExecutionGuard(path);g.prepare('SOL/USDT','SELL',100,'old')
        self.assertEqual(self.buy()['status'],'allocation_pending');self.assertFalse(self.ex.calls)
    def test_concurrent_buy_busy_then_revalues(self):
        entered=threading.Event();release=threading.Event();original=self.ex.fetch_balance
        def waiting():
            entered.set();release.wait(3);return original()
        self.ex.fetch_balance=waiting;results=[]
        thread=threading.Thread(target=lambda:results.append(self.buy(key='a')));thread.start()
        self.assertTrue(entered.wait(2))
        try:self.assertEqual(self.buy('ETH',key='b')['status'],'executor_busy')
        finally:release.set();thread.join(4)
        self.assertFalse(thread.is_alive());self.assertEqual(len(self.ex.calls),1)
        self.assertTrue(results[0]['success'])
    def test_new_guard_prepared_intent_exempt_only_for_this_call(self):
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        g=ExecutionGuard(path);g.prepare('BTC/USDT','BUY',20,'new')
        self.assertTrue(self.buy(key=g.client_oid)['success'])
    def test_other_prepared_intent_not_exempt(self):
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        g=ExecutionGuard(path);g.prepare('ETH/USDT','BUY',20,'other')
        self.assertEqual(self.buy()['status'],'allocation_pending')
    def test_stale_prepared_same_key_unknown_not_exempt(self):
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        g=ExecutionGuard(path);g.prepare('BTC/USDT','BUY',20,'new');g.journal.unknown('new')
        self.assertEqual(self.buy(key=g.client_oid)['status'],'allocation_pending')
    def test_frozen_coin_counts_for_sixty_limit(self):
        self.ex.balances=balance(40)
        self.ex.balances['BTC']={'free':.1,'used':.5,'total':.6}
        self.assertEqual(self.buy()['status'],'max_exposure_reached')
    def test_frozen_usdt_not_spendable(self):
        self.ex.balances=balance(20,locked=80)
        self.assertEqual(self.buy()['status'],'cash_reserve_reached')
    def test_missing_limits_blocks(self):
        self.ex.minimum=None
        self.assertEqual(self.buy()['status'],'valuation_unavailable')
    def test_process_lock_blocks_second_worker(self):
        import subprocess
        code="from clients.execution_lane import execution_lane; import sys;\nwith execution_lane(sys.argv[1]) as lane: print('busy' if lane is None else 'acquired')"
        path=Path(self.temp.name)/'process'/'lane.sqlite3'
        with execution_lane(path):
            child=subprocess.run([sys.executable,'-B','-c',code,str(path)],cwd=Path(__file__).resolve().parents[1],
                                 capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr);self.assertEqual(child.stdout.strip(),'busy')
    def test_pre_submit_precision_exception_safe_terminal(self):
        self.ex.amount_to_precision=lambda *a: (_ for _ in ()).throw(ValueError('bad precision'))
        self.assertEqual(self.buy()['status'],'pre_submission_failed');self.assertFalse(self.ex.calls)
    def test_actual_entry_allows_holding_buy(self):
        self.ex.balances=balance(90,{'BTC':.1})
        with patch.object(gt,'DRY_RUN',False),patch.object(gt,'get_real_holdings',return_value={'BTC':{'quantity':.1}}):
            r=gt.execute_order('btcusdt','BUY',10,'BTC',20,request_id='real-entry-fixture')
        self.assertTrue(r['success']);self.assertEqual(len(self.ex.calls),1)

class ValuationTests(unittest.TestCase):
    def test_locked_and_dust_included(self):
        b=balance(40,{'SOL':.001},locked=10);b['SOL']={'free':.0004,'used':.0006,'total':.001}
        s=policy.calculate_total_net_worth(b,lambda p:{'last':100,'timestamp':time.time()*1000})
        self.assertEqual(s['total'],D('50.100'));self.assertEqual(s['free_usdt'],D(40))
    def test_ccxt_maps_not_double_counted(self):
        b={'free':{'USDT':40,'SOL':.001},'used':{'USDT':10,'SOL':0},'total':{'USDT':50,'SOL':.001},'SOL':{'total':.001}}
        s=policy.calculate_total_net_worth(b,lambda p:{'last':100,'timestamp':time.time()*1000})
        self.assertEqual(s['total'],D('50.100'))
    def test_bad_balances_rejected(self):
        for b in [balance(-1),{'USDT':{'free':40,'used':10,'total':40}}]:
            with self.assertRaises(ValueError):policy.calculate_total_net_worth(b,lambda p:None)
    def test_future_quote_rejected(self):
        with self.assertRaises(ValueError):policy.fresh_price({'last':1,'timestamp':(time.time()+60)*1000})
    def test_missing_timestamp_rejected(self):
        with self.assertRaises(ValueError):policy.fresh_price({'last':1})
    def test_missing_asset_never_zero(self):
        with self.assertRaises(Exception):policy.calculate_total_net_worth(balance(80,{'XYZ':1}),lambda p:None)
    def test_overweight_does_not_sell(self):
        s={'total':D(100),'free_usdt':D(10),'asset_values':{'BTC':D(90)}}
        self.assertEqual(policy.buy_budget(s,'BTC',10,'.001')[1],'max_exposure_reached')

class PaperTests(unittest.TestCase):
    def test_same_limits_in_atomic_ledger_and_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            a=PaperAccount(Path(temp)/'paper.sqlite3',100,fee_rate='0.001',slippage='0')
            now=time.time();quotes={p:{'price':100,'observed_at':now} for p in ['BTC/USDT','ETH/USDT','SOL/USDT']}
            first=a.place('BTC/USDT','BUY',50,quotes,key='a',now=now)
            self.assertTrue(first['success']);self.assertEqual(a.place('BTC/USDT','BUY',50,quotes,key='a',now=now),first)
            self.assertTrue(a.place('BTC/USDT','BUY',50,quotes,key='b',now=now)['success'])
            a.place('ETH/USDT','BUY',50,quotes,key='c',now=now)
            a.place('SOL/USDT','BUY',50,quotes,key='d',now=now)
            snap=a.snapshot();value=sum(D(p['quantity'])*100 for p in snap['positions'].values());total=D(snap['cash'])+value
            self.assertGreaterEqual(D(snap['cash'])/total,D('.20'))
            self.assertLessEqual(D(snap['positions']['BTC/USDT']['quantity'])*100/total,D('.60'))

class CCXTAdapterTests(unittest.TestCase):
    def test_native_buy_quote_units_without_network(self):
        import ccxt
        x=ccxt.gate()
        x.set_markets([{'id':'BTC_USDT','symbol':'BTC/USDT','base':'BTC','quote':'USDT',
            'baseId':'BTC','quoteId':'USDT','type':'spot','spot':True,'swap':False,'future':False,
            'option':False,'contract':False,'active':True,'precision':{'amount':.000001,'price':.01},
            'limits':{'amount':{'min':.000001},'cost':{'min':3}},'info':{}}])
        req=x.create_order_request('BTC/USDT','market','buy',.0001,100000,{'cost':9.87,'text':'t-offline'})
        self.assertEqual(req['amount'],'9.87');self.assertEqual(req['side'],'buy')
        self.assertEqual(req['text'],'t-offline');self.assertEqual(req['time_in_force'],'ioc')

class PromptTests(unittest.TestCase):
    def test_only_risk_role_changed_and_buy_allowed(self):
        import seven_roles_committee as committee
        from clients.json_prompt import build_json_prompt
        import hashlib,json
        unchanged={k:v for k,v in committee.AGENT_PROMPTS.items() if k!='risk_manager'}
        self.assertEqual(hashlib.sha256(json.dumps(unchanged,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),'e5d7faa1501b4a27a31d5fe814a2e26e823b05e0084feb633e8b1ca4b9680c0d')
        self.assertIn('分批加仓',committee.AGENT_PROMPTS['risk_manager'])
        p=build_json_prompt({},'BTC',holdings_text='BTC: 有持仓')
        self.assertNotIn('不能再加仓',p);self.assertNotIn('且空仓时',p)
        self.assertIn('60%',p);self.assertIn('20%',p)
    def test_exposure_display_keeps_dust_value(self):
        from clients.position_review import _allocation_context,position_text
        ctx={'quantity':'.001','available_usdt':'40','cost_status':'dust','is_dust':True}
        with patch.object(gt,'calculate_total_net_worth',return_value={'total':D('50.1'),'asset_values':{'SOL':D('.1')}}):
            _allocation_context(ctx,'SOL',balance={})
        text=position_text('SOL',ctx)
        self.assertIn('0.1000 USDT',text);self.assertIn('50.1000 USDT',text)
        self.assertIn('严禁 SELL',text);self.assertGreater(D(ctx['exposure_pct']),0)
    def test_valuation_unknown_not_zero(self):
        from clients.position_review import _allocation_context,position_text
        ctx={'quantity':'1','available_usdt':'40','cost_status':'unknown'}
        with patch.object(gt,'calculate_total_net_worth',side_effect=ValueError()):_allocation_context(ctx,'BTC',balance={})
        self.assertEqual(ctx['valuation_status'],'unknown');self.assertNotIn('exposure_pct',ctx)
        self.assertIn('估值未知',position_text('BTC',ctx))

if __name__=='__main__':unittest.main()
