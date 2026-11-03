import contextlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
HOME=tempfile.TemporaryDirectory(prefix='p2_tests_');os.environ['HOME']=HOME.name
from clients.order_journal import OrderJournal, normalize, number
from clients.paper_account import PaperAccount, accounting, DEFAULT_POLICY
from clients.gate_readonly import GateReadOnly, ReadOnlyError


def order(status='closed',finish='filled',qty='1',cost='100',left='0'):
    return dict(id='100',currency_pair='BTC_USDT',account='spot',side='buy',status=status,
                finish_as=finish,filled_amount=qty,filled_total=cost,left=left)


def trade(id='1',qty='1',price='100',fee='0.1',unit='USDT'):
    return dict(id=id,order_id='100',currency_pair='BTC_USDT',side='buy',amount=qty,
                price=price,fee=fee,fee_currency=unit,gt_fee='0',point_fee='0')


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.j=OrderJournal(Path(self.tmp.name)/'state'/'orders.db')
        self.j.prepare('intent','test-account','BTC/USDT','buy',{'quote_amount':'100'})
        self.j.attach('intent','100')

    def test_idempotent_intent_and_conflict(self):
        self.assertEqual(self.j.prepare('intent','test-account','BTC/USDT','buy',{'quote_amount':'100'})['state'],'submitted')
        with self.assertRaises(ValueError):self.j.prepare('intent','test-account','BTC/USDT','buy',{'quote_amount':'101'})
        with self.assertRaises(ValueError):self.j.attach('intent','101')

    def test_price_comes_from_fills_not_ticker_or_native_fill_price(self):
        raw=order();raw['fill_price']='99999'
        row=self.j.observe('intent',raw,[trade()])
        self.assertEqual(row['state'],'filled')
        self.assertEqual(D(row['evidence']['average']),D('100'))
        self.assertTrue(row['evidence']['fully_filled'])
        self.assertEqual(row['evidence']['fees'],{'USDT':'0.1'})

    def test_partial_cancel_and_fee_currency_preserved(self):
        row=self.j.observe('intent',order('open','open','.5','50','.5'),[trade(qty='.5',fee='.0001',unit='BTC')])
        self.assertEqual(row['state'],'partially_filled')
        self.assertFalse(row['evidence']['fully_filled'])
        row=self.j.observe('intent',order('cancelled','cancelled','.5','50','.5'),[trade(qty='.5',fee='.0001',unit='BTC')])
        self.assertEqual(row['state'],'cancelled')
        self.assertEqual(row['evidence']['fees'],{'BTC':'0.0001'})

    def test_cancel_without_fill_is_not_confirmed_fill(self):
        row=self.j.observe('intent',order('cancelled','ioc','0','0','1'),[])
        self.assertFalse(row['evidence']['exchange_confirmed'])
        self.assertFalse(row['evidence']['fully_filled'])

    def test_missing_fields_wrong_identity_and_nan_rejected(self):
        mutations=[dict(id='101'),dict(currency_pair='ETH_USDT'),dict(side='sell'),dict(account='unified'),
                   dict(filled_amount='NaN'),dict(filled_total=None),dict(left='-1'),dict(trade_quote='USDC'),
                   dict(status='closed',finish_as='filled',left='1')]
        for changes in mutations:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                normalize({**order(),**changes},[trade()],'100','BTC/USDT','buy')
        with self.assertRaises(ValueError):normalize(order(),[trade(qty='.5')],'100','BTC/USDT','buy')

    def test_duplicate_fill_never_double_counts(self):
        self.j.observe('intent',order(),[trade(),trade()])
        self.j.observe('intent',order(),[trade()])
        self.assertEqual(len(self.j.fills('test-account')),1)
        with self.assertRaises(ValueError):self.j.observe('intent',order(),[trade(),trade(fee='.2')])
        second=OrderJournal(Path(self.tmp.name)/'other'/'db')
        second.prepare('intent','test-account','BTC/USDT','buy',{})
        second.attach('intent','100')
        half=[trade(id='1',qty='.5'),trade(id='2',qty='.5')]
        second.observe('intent',order(),half)
        second.observe('intent',order(),list(reversed(half)))
        self.assertEqual(len(second.fills('test-account')),2)

    def test_timeout_is_unknown_and_never_resubmitted(self):
        reader=Mock();reader.order.side_effect=TimeoutError('contains secret')
        row=self.j.reconcile('intent',reader)
        self.assertEqual(row['state'],'unknown')
        self.assertEqual(self.j.unresolved('test-account','BTC/USDT'),1)
        reader.order.assert_called_once();reader.trades.assert_not_called()
        self.assertNotIn('secret',row['error'])
        self.assertFalse(hasattr(GateReadOnly,'create_order'))

    def test_snapshot_changing_while_querying_is_unknown(self):
        reader=Mock();reader.order.side_effect=[order('open','open','0','0','1'),order()];reader.trades.return_value=[trade()]
        self.assertEqual(self.j.reconcile('intent',reader)['state'],'unknown')
        self.assertEqual(self.j.fills('test-account'),[])

    def test_restart_reconcile_recovers_without_duplicate_fill(self):
        self.j.observe('intent',order(),[trade()])
        restored=OrderJournal(self.j.path);reader=Mock()
        reader.order.return_value=order();reader.trades.return_value=[trade()]
        self.assertEqual(restored.reconcile('intent',reader)['state'],'filled')
        self.assertEqual(len(restored.fills('test-account')),1)

    def test_regression_and_mutated_trade_rejected_atomically(self):
        self.j.observe('intent',order('open','open','.5','50','.5'),[trade(qty='.5')])
        with self.assertRaises(ValueError):self.j.observe('intent',order('open','open','0','0','1'),[])
        with self.assertRaises(ValueError):self.j.observe('intent',order(),[trade(id='2')])
        self.assertEqual(len(self.j.fills('test-account')),1)
        self.assertEqual(self.j.get('intent')['state'],'partially_filled')

    def test_third_currency_and_gt_fees_are_not_dropped(self):
        raw=trade(unit='BNB');raw['gt_fee']='.02'
        row=self.j.observe('intent',order(),[raw])
        self.assertEqual(row['evidence']['fees'],{'BNB':'0.1','GT':'0.02'})

    def test_multiworker_replays_same_fill_once(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _:self.j.observe('intent',order(),[trade()]),range(10)))
        self.assertEqual(len(self.j.fills('test-account')),1)


class PaperRiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state'/'paper.db';self.now=1700000000
        self.p=PaperAccount(self.path,1000,fee_rate='0',slippage='0',min_quote='0')

    def quotes(self,price=100,stamp=None):
        return {'BTC/USDT':{'price':price,'observed_at':self.now if stamp is None else stamp}}

    def buy(self,pct=50,price=100,key='buy'):
        return self.p.place('BTC/USDT','BUY',pct,self.quotes(price),key=key,now=self.now)

    def test_cash_is_not_mark_to_market_and_fifo_partial_sell(self):
        self.buy();self.buy(pct=20,price=110,key='buy2')
        snapshot=self.p.snapshot();self.assertAlmostEqual(float(snapshot['cash']),400,places=5)
        # FIFO against first lot; no last-buy price and no multiplication by full buy quantity.
        sold=self.p.place('BTC/USDT','SELL',30,self.quotes(110),key='sell',now=self.now)
        self.assertTrue(sold['success'])
        self.assertAlmostEqual(sold['pnl_usdt'],sold['quantity']*10,places=5)
        expected=D('400')+D(str(sold['quantity']))*110
        self.assertAlmostEqual(float(self.p.snapshot()['cash']),float(expected),places=5)

    def test_fee_and_slippage_reduce_roundtrip_net(self):
        path=Path(self.tmp.name)/'costs'/'paper.db'
        p=PaperAccount(path,1000,min_quote='0')
        buy=p.place('BTC/USDT','BUY',50,self.quotes(),now=self.now)
        sell=p.place('BTC/USDT','SELL',100,self.quotes(),now=self.now)
        self.assertGreater(buy['fee_usdt'],0)
        self.assertGreater(buy['fill_price'],100)
        self.assertLess(sell['fill_price'],100)
        self.assertLess(sell['pnl_usdt'],0)
        self.assertLess(D(p.snapshot()['cash']),1000)

    def test_atomic_idempotency_concurrent_and_restart(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            outcomes=list(pool.map(lambda _:self.buy(),range(10)))
        self.assertTrue(all(item==outcomes[0] for item in outcomes))
        self.assertEqual(self.p.snapshot()['revision'],1)
        restored=PaperAccount(self.path,1000,fee_rate='0',slippage='0',min_quote='0')
        self.assertEqual(restored.place('BTC/USDT','BUY',50,self.quotes(),key='buy',now=self.now),outcomes[0])
        with self.assertRaises(ValueError):self.p.place('BTC/USDT','BUY',20,self.quotes(),key='buy',now=self.now)

    def test_stop_runs_without_ai_and_bypasses_ordinary_cooldown(self):
        self.buy()
        result=self.p.protect(self.quotes(94),now=self.now)
        self.assertEqual(len(result['exits']),1)
        self.assertTrue(result['exits'][0]['success'])
        self.assertEqual(self.p.snapshot()['positions'],{})
        self.assertEqual(self.p.protect(self.quotes(94),now=self.now)['exits'],[])

    def test_stale_data_blocks_new_buys_but_does_not_invent_zero_price(self):
        self.buy();revision=self.p.snapshot()['revision']
        for quotes in ({},self.quotes(100,self.now-61)):
            result=self.p.protect(quotes,now=self.now)
            self.assertTrue(result['blocked_new_risk']);self.assertEqual(result['exits'],[])
            self.assertEqual(self.p.snapshot()['revision'],revision)
        result=self.p.place('ETH/USDT','BUY',20,{'ETH/USDT':{'price':100,'observed_at':self.now}},now=self.now)
        self.assertFalse(result['success'])

    def test_missing_one_quote_does_not_block_another_fresh_stop(self):
        self.buy()
        both={**self.quotes(),'ETH/USDT':{'price':100,'observed_at':self.now}}
        self.p.place('ETH/USDT','BUY',20,both,key='eth',now=self.now)
        result=self.p.protect(self.quotes(94),now=self.now)
        self.assertEqual([x['symbol'] for x in result['exits']],['BTC/USDT'])
        self.assertIn('ETH/USDT',result['stale_symbols'])
        self.assertEqual(set(self.p.snapshot()['positions']),{'ETH/USDT'})

    def test_drawdown_latch_persists_after_restart_and_next_day(self):
        self.buy();self.p.protect(self.quotes(150),now=self.now)
        result=self.p.protect(self.quotes(120),now=self.now)
        self.assertEqual(result['halted'],'max_drawdown')
        p=PaperAccount(self.path,1000,fee_rate='0',slippage='0',min_quote='0')
        later=self.now+86400
        self.assertFalse(p.place('BTC/USDT','BUY',20,self.quotes(100,later),now=later)['success'])
        self.assertEqual(p.snapshot()['risk']['halted'],'max_drawdown')

    def test_daily_loss_limit_closes_positions_and_blocks_new_risk(self):
        self.buy()
        result=self.p.protect(self.quotes(94),now=self.now)
        self.assertEqual(result['halted'],'daily_loss')
        self.assertFalse(self.buy(key='after_loss')['success'])

    def test_invalid_policy_and_nonfinite_price_fail_closed(self):
        with self.assertRaises(ValueError):PaperAccount(Path(self.tmp.name)/'bad'/'db',1000,policy={**DEFAULT_POLICY,'position_stop':'0'})
        for price in (float('nan'),float('inf'),-1,0):
            with self.subTest(price=price):
                result=self.p.place('BTC/USDT','BUY',20,self.quotes(price),now=self.now)
                self.assertFalse(result['success'])
                self.assertFalse(result['exchange_confirmed'])
        self.assertEqual(self.p.snapshot()['ledger'],[])

    def test_corrupt_legacy_ledger_is_not_reset(self):
        legacy=Path(self.tmp.name)/'legacy.json';legacy.write_text('{broken')
        with self.assertRaises(ValueError):PaperAccount(Path(self.tmp.name)/'migration'/'db',1000,legacy=legacy)
        self.assertEqual(legacy.read_text(),'{broken')

    def test_legacy_oversell_and_cash_deficit_rejected(self):
        for rows in ([{'symbol':'BTC/USDT','action':'SELL','quantity':1,'fill_price':100}],
                     [{'symbol':'BTC/USDT','action':'BUY','quantity':100,'fill_price':100}]):
            with self.assertRaises(ValueError):accounting(rows,1000)

    def test_state_symlink_does_not_modify_victim(self):
        directory=Path(self.tmp.name)/'attack';directory.mkdir(mode=0o700)
        victim=Path(self.tmp.name)/'victim';victim.write_text('KEEP')
        os.symlink(victim,directory/'db')
        with self.assertRaises(OSError):PaperAccount(directory/'db',1000)
        self.assertEqual(victim.read_text(),'KEEP')


class AdapterAndWorkerTests(unittest.TestCase):
    def test_adapter_has_only_fixed_get_routes_and_no_redirect(self):
        session=Mock();session.request.return_value.status_code=200;session.request.return_value.json.return_value=[]
        reader=GateReadOnly('FAKE_KEY','FAKE_SECRET',session)
        reader.accounts();call=session.request.call_args
        self.assertEqual(call.args[0],'GET');self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(call.args[1],'https://api.gateio.ws/api/v4/spot/accounts')
        self.assertIn('SIGN',call.kwargs['headers'])
        with self.assertRaises(ReadOnlyError):reader._get('/spot/orders/../withdrawals')
        with self.assertRaises(ValueError):reader.order('../1','BTC/USDT')
        with self.assertRaises(ValueError):reader.ticker('BTC/USDT?evil=1')

    def test_api_errors_do_not_echo_secrets(self):
        session=Mock();session.request.side_effect=RuntimeError('FAKE_SECRET')
        with self.assertRaises(ReadOnlyError) as caught:GateReadOnly('FAKE_KEY','FAKE_SECRET',session).accounts()
        self.assertNotIn('FAKE_SECRET',str(caught.exception))

    def test_pagination_and_saturation_are_explicit(self):
        reader=GateReadOnly('key','secret',Mock())
        with patch.object(reader,'_get',side_effect=[[{}]*1000,[{}]]) as get:
            self.assertEqual(len(reader.trades('1','BTC/USDT')),1001)
            self.assertEqual(get.call_count,2)
        with patch.object(reader,'_get',return_value=[{}]*1000):
            with self.assertRaises(ReadOnlyError):reader.trades('1','BTC/USDT')

    def test_protection_worker_runs_even_when_strategy_ai_is_stuck(self):
        import threading
        import alert_monitor
        import clients.gateio_trade as gt
        stop=threading.Event();ran=threading.Event()
        def protect():ran.set();return {'exits':[]}
        with patch.object(gt,'run_protection_cycle',side_effect=protect):
            worker=alert_monitor.start_protection_worker(stop)
            self.assertTrue(ran.wait(2))
            stop.set();worker.join(2);self.assertFalse(worker.is_alive())

    def test_real_executor_paper_path_and_independent_stop_end_to_end(self):
        import clients.gateio_trade as gt
        with tempfile.TemporaryDirectory() as temp, patch.object(gt,'_PAPER_LOG',str(Path(temp)/'paper.json')), \
                patch.object(gt,'_fetch_current_price',return_value=100) as quote, \
                patch.object(gt,'_load_env') as secrets:
            buy=gt.execute_order('btcusdt','BUY',percentage=20,request_id='e2e-scan')
            self.assertTrue(buy['success']);self.assertEqual(buy['status'],'paper_filled')
            quote.return_value=94
            protection=gt.run_protection_cycle()
            self.assertEqual(len(protection['exits']),1)
            self.assertTrue(protection['exits'][0]['success'])
            self.assertEqual(gt._paper_account().snapshot()['positions'],{})
            self.assertLess(float(gt._paper_account().snapshot()['realized_net']),0)
            self.assertEqual(gt.run_protection_cycle()['exits'],[])
            secrets.assert_not_called()

    def test_report_does_not_turn_missing_quote_into_zero_or_real_profit(self):
        import clients.gateio_trade as gt
        with tempfile.TemporaryDirectory() as temp, patch.object(gt,'_PAPER_LOG',str(Path(temp)/'paper.json')), \
                patch.object(gt,'_fetch_current_price',return_value=100) as quote:
            self.assertTrue(gt.execute_order('btcusdt','BUY',percentage=20)['success'])
            quote.return_value=0
            report=gt.generate_ledger_summary()
            self.assertIn('净值未知',report)
            self.assertIn('非真实收益',report)

    def test_committee_errors_never_echo_credential_canary(self):
        import seven_roles_committee as committee
        canary='Bearer FAKE_CREDENTIAL_DO_NOT_LOG_123456'
        response=Mock(status_code=401,text=canary)
        for failure in (response,RuntimeError(canary)):
            output=io.StringIO()
            with patch.object(committee.requests,'post') as post, patch.object(committee.time,'sleep'), \
                    contextlib.redirect_stdout(output):
                if isinstance(failure,Exception):post.side_effect=failure
                else:post.return_value=failure
                result=committee.ask_agent('risk_manager','fixture',max_retries=1)
            self.assertFalse(result.ok)
            self.assertNotIn('FAKE_CREDENTIAL_DO_NOT_LOG_123456',output.getvalue())

    def test_health_email_disabled_without_explicit_authorization(self):
        import health_checker
        with patch.object(health_checker.smtplib,'SMTP') as smtp:
            self.assertFalse(health_checker.send_email('fixture','fixture'))
            smtp.assert_not_called()

    def test_notifications_disabled_before_token_or_send_http(self):
        import wechat_push
        with patch.object(wechat_push.requests,'get') as get,patch.object(wechat_push.requests,'post') as post:
            with self.assertRaises(RuntimeError):wechat_push.get_token()
            with self.assertRaises(RuntimeError):wechat_push.send_text('fake','body')
            get.assert_not_called();post.assert_not_called()

if __name__=='__main__':unittest.main(verbosity=2)
