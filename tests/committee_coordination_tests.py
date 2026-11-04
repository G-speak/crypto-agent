"""Conflict/receipt tests. All execution, balance and venue reads are fakes."""
import os
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from clients.committee_coordination import ExecutionGuard, committee_lane, coordinated_run


def order(status='closed',finish='filled',left='0',filled='1'):
    return dict(id='123',currency_pair='SOL_USDT',account='spot',side='sell',
                status=status,finish_as=finish,left=left,filled_amount=filled,filled_total=str(float(filled)*100))


def trade():
    return dict(id='456',order_id='123',currency_pair='SOL_USDT',side='sell',
                amount='1',price='100',fee='.1',fee_currency='USDT')


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{'HOME':self.temp.name});self.env.start();self.addCleanup(self.env.stop)
        self.path=Path(self.temp.name)/'.hermes/state/committee_orders.sqlite3'
        self.runner=Mock(return_value=True);self.event=Mock()

    def dispatch(self,trigger='radar',now=1000,coin='SOL'):
        with patch('clients.committee_coordination.time.time',return_value=now):
            return coordinated_run(coin,coin.lower()+'usdt','fixture','scan-'+str(now),
                                   False,trigger,self.runner,self.event)

    def test_radar_postpones_periodic_review_and_survives_restart(self):
        self.assertTrue(self.dispatch())
        self.assertFalse(self.dispatch('position_review',1001))
        self.assertFalse(self.dispatch('position_review',15399))
        self.assertTrue(self.dispatch('position_review',15400))
        self.assertEqual(self.runner.call_count,2)

    def test_periodic_then_radar_prevents_immediate_reverse(self):
        self.assertTrue(self.dispatch('position_review'))
        self.assertFalse(self.dispatch('radar',1299))
        self.assertTrue(self.dispatch('radar',1300))
        self.assertEqual(self.runner.call_count,2)

    def test_empty_other_coin_does_not_share_per_coin_cooldown(self):
        self.assertTrue(self.dispatch())
        self.assertTrue(self.dispatch(coin='ETH'))

    def test_thread_conflict_never_starts_second_committee(self):
        with committee_lane(self.path) as gate:
            self.assertIsNotNone(gate)
            result=[]
            thread=threading.Thread(target=lambda:result.append(self.dispatch()))
            thread.start();thread.join(timeout=3)
            self.assertFalse(thread.is_alive());self.assertEqual(result,[False])
        self.runner.assert_not_called()

    def test_separate_process_cannot_start_while_lock_held(self):
        code="from clients.committee_coordination import committee_lane; from pathlib import Path; import sys\nwith committee_lane(Path(sys.argv[1])) as gate: print('BUSY' if gate is None else 'ACQUIRED')"
        env=dict(os.environ);env['PYTHONPATH']=str(ROOT)+os.pathsep+env.get('PYTHONPATH','')
        with committee_lane(self.path):
            proc=subprocess.run([sys.executable,'-c',code,str(self.path)],capture_output=True,text=True,env=env,timeout=5)
        self.assertEqual(proc.returncode,0,proc.stderr);self.assertEqual(proc.stdout.strip(),'BUSY')

    def test_long_model_call_cooldown_starts_when_it_finishes(self):
        with patch('clients.committee_coordination.time.time',side_effect=[1000,2000]):
            self.assertTrue(coordinated_run('SOL','solusdt','fixture','scan',False,'radar',self.runner,self.event))
        self.assertFalse(self.dispatch('radar',2299))
        self.assertTrue(self.dispatch('radar',2300))

    def test_crash_after_intent_blocks_even_other_coin_and_restart(self):
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',100,'request1')
        self.assertTrue(ExecutionGuard(self.path).blocked())
        self.assertFalse(self.dispatch(coin='ETH'))
        self.runner.assert_not_called()

    def test_duplicate_intent_is_not_replayed(self):
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',100,'request1')
        guard.observe({'status':'zero_amount'})
        with self.assertRaises(ValueError):guard.prepare('SOL/USDT','SELL',100,'request1')

    def test_known_pre_submit_rejection_can_clear_latch(self):
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',100,'request1')
        guard.observe({'status':'skipped_no_holdings'})
        self.assertFalse(guard.blocked())

    def test_accepted_or_timeout_without_id_does_not_clear_latch(self):
        for index,status in enumerate(('exception','submitted','unconfirmed')):
            with self.subTest(status=status):
                guard=ExecutionGuard(self.path.with_name(str(index)+'.sqlite3'))
                guard.prepare('SOL/USDT','SELL',100,'request1')
                result=guard.observe({'status':status,'exchange_confirmed':True})
                self.assertFalse(result['exchange_confirmed'])
                self.assertTrue(guard.blocked())

    def test_execution_constraints_respect_precision(self):
        from clients.execution_constraints import constraints_text
        row={'min_quote_amount':'3','min_base_amount':'.001','amount_precision':3}
        self.assertIn('79%–100%',constraints_text(row,'.040899','95.68'))
        self.assertIn('无（',constraints_text(row,'.02','95.68'))
        self.assertIn('不得为凑最低金额',constraints_text(row,'.040899','95.68'))

    def test_small_sell_never_submits(self):
        from clients import gateio_trade as trade_module
        import sys, types
        exchange=Mock()
        exchange.markets={'SOL/USDT':{'limits':{'amount':{'min':.001},'cost':{'min':3}}}}
        exchange.fetch_ticker.return_value={'last':95.68}
        exchange.fetch_balance.return_value={'free':{'SOL':.040899}}
        exchange.amount_to_precision.return_value='0.024'
        with patch.dict(sys.modules,{'ccxt':types.SimpleNamespace(gate=Mock(return_value=exchange))}), patch.object(trade_module,'_load_env',return_value={'GATEIO_API_KEY':'fake','GATEIO_API_SECRET':'fake'}):
            result=trade_module._live_trade('SOL/USDT','SOL','SELL',0,60)
        self.assertEqual(result['status'],'below_min_cost')
        exchange.create_market_order.assert_not_called()
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',60,'small')
        guard.observe(result)
        self.assertFalse(guard.blocked())

    def test_original_error_persisted_without_clearing_unknown(self):
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',60,'diagnostic')
        result=guard.observe({'status':'exception','detail':'RequestTimeout: read timed out'})
        self.assertIn('RequestTimeout',result['detail'])
        self.assertEqual(result['execution_diagnostic']['status'],'exception')
        self.assertTrue(ExecutionGuard(self.path).blocked())
        from clients.order_journal import database
        with database(self.path) as db:
            saved=db.execute("SELECT error FROM intents WHERE key='diagnostic'").fetchone()[0]
        self.assertIn('RequestTimeout',saved)

    def test_filled_claim_requires_private_order_and_fills(self):
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',100,'request1')
        reader=Mock();reader.order.return_value=order();reader.trades.return_value=[trade()]
        with patch('clients.committee_coordination.readonly_reader',return_value=reader):
            result=guard.observe({'status':'filled','order_id':'123','exchange_confirmed':True})
        self.assertTrue(result['exchange_confirmed']);self.assertEqual(result['quantity'],1)
        self.assertEqual(reader.order.call_count,2);reader.trades.assert_called_once()
        self.assertFalse(guard.blocked())

    def test_partial_open_order_stays_blocked_until_terminal_reconciliation(self):
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',100,'request1')
        reader=Mock();reader.order.return_value=order('open','open',left='1');reader.trades.return_value=[trade()]
        with patch('clients.committee_coordination.readonly_reader',return_value=reader):
            result=guard.observe({'status':'filled','order_id':'123','exchange_confirmed':True})
            self.assertFalse(result['exchange_confirmed']);self.assertTrue(guard.blocked())
            reader.order.return_value=order()
            self.assertFalse(guard.blocked())

    def test_unstable_read_or_missing_fills_never_unblocks(self):
        guard=ExecutionGuard(self.path);guard.prepare('SOL/USDT','SELL',100,'request1')
        reader=Mock();reader.order.return_value=order();reader.trades.return_value=[]
        with patch('clients.committee_coordination.readonly_reader',return_value=reader):
            result=guard.observe({'status':'filled','order_id':'123','exchange_confirmed':True})
            self.assertFalse(result['exchange_confirmed']);self.assertTrue(guard.blocked())


if __name__=='__main__':
    def blocked(*a,**kw):raise AssertionError('offline test network forbidden')
    socket.socket.connect=blocked;socket.create_connection=blocked;socket.getaddrinfo=blocked
    unittest.main()
