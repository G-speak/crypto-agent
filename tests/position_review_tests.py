import ast
import contextlib
from decimal import Decimal as D
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clients.position_review import (cost_evidence, history, position_context,
    position_text, validate_account, ReviewSchedule, review_cycle)
from clients.trade_policy import parse_command


def fill(id='1', amount='1', price='100', fee='.001', unit='SOL', side='buy', **kw):
    return dict(id=id, amount=amount, price=price, fee=fee, fee_currency=unit,
                currency_pair='SOL_USDT', side=side, create_time=id, **kw)


class CostTests(unittest.TestCase):
    def test_current_real_fill_fixture(self):
        rows=[fill('1','.047','108.14','.000047'), fill('2','.054','108.13','.000054')]
        x=cost_evidence(rows,'SOL','.100899','100.53')
        self.assertEqual(x['cost_status'],'queried_history_estimate')
        self.assertEqual(D(x['cost_usdt']),D('10.9216'))
        self.assertEqual(D(x['unrealized_usdt']),D('-.77822353'))
        self.assertEqual(cost_evidence(rows+rows,'SOL','.100899','100.53'),x)

    def test_quote_fee_and_partial_sell(self):
        rows=[fill(fee='1',unit='USDT'),fill('2','.4','120','0',side='sell')]
        x=cost_evidence(rows,'SOL','.6','110')
        self.assertEqual(D(x['cost_usdt']),D('60.6'))
        self.assertEqual(D(x['avg_cost']),D('101'))

    def test_unknown_cost_not_zero(self):
        for rows,qty in [([],1),([fill()],2),([fill(unit='GT')],'.999'),
            ([fill(side='sell')],1),([fill(),fill(price='200')],'.999'),
            ([fill(price='NaN')],'.999'),([fill(gt_fee='.1')],'.999')]:
            with self.subTest(rows=rows):
                x=cost_evidence(rows,'SOL',qty,100)
                self.assertEqual(x['cost_status'],'unknown')
                self.assertNotIn('avg_cost',x)

    def test_cost_text_labels_estimate_and_unknown(self):
        self.assertIn('成本未知',position_text('SOL',dict(quantity='1',cost_status='unknown')))
        x=cost_evidence([fill()],'SOL','.999',90);x['quantity']='.999'
        self.assertIn('不能排除历史划转',position_text('SOL',x))

    def test_history_window_has_no_missing_seconds(self):
        reader=Mock();reader._get.return_value=[]
        self.assertEqual(history(reader,'SOL',now=10000000),[])
        windows=[c.args[1] for c in reader._get.call_args_list]
        for left,right in zip(windows,windows[1:]):self.assertEqual(left['to']+1,right['from'])
        self.assertEqual(windows[-1]['to'],10000000)
        self.assertTrue(all(c.args[0]=='/spot/my_trades' for c in reader._get.call_args_list))

    def test_cash_zero_allows_sell_review_but_not_buy(self):
        x=dict(quantity='1',available_usdt='0')
        validate_account(x);validate_account(x,'SELL')
        with self.assertRaises(ValueError):validate_account(x,'BUY')
        with self.assertRaises(ValueError):validate_account(dict(quantity='0',available_usdt='NaN'))

    def test_live_context_reconciles_readonly_history(self):
        balance={'SOL':{'total':'.999'},'USDT':{'free':'0'}}
        trade=types.SimpleNamespace(DRY_RUN=False,get_real_balance=Mock(return_value=balance),
                                    _load_env=Mock(return_value={}))
        with patch.dict(sys.modules,{'clients.gateio_trade':trade}), \
             patch('clients.position_review.history',return_value=[fill()]):
            x=position_context('SOL',90)
        self.assertEqual(x['cost_status'],'queried_history_estimate')
        self.assertEqual(x['available_usdt'],'0')
        self.assertEqual(D(x['cost_usdt']),D('100'))

    def test_balance_read_failure_never_becomes_empty_account(self):
        trade=types.SimpleNamespace(DRY_RUN=False,get_real_balance=Mock(return_value=None))
        with patch.dict(sys.modules,{'clients.gateio_trade':trade}),self.assertRaises(ValueError):
            position_context('SOL',90)

    def test_balance_change_invalidates_cost(self):
        first={'SOL':{'total':'.999'},'USDT':{'free':'0'}}
        second={'SOL':{'total':'.5'},'USDT':{'free':'40'}}
        trade=types.SimpleNamespace(DRY_RUN=False,get_real_balance=Mock(side_effect=[first,second]),
                                    _load_env=Mock(return_value={}))
        with patch.dict(sys.modules,{'clients.gateio_trade':trade}), \
             patch('clients.position_review.history',return_value=[fill()]):
            x=position_context('SOL',90)
        self.assertEqual(x['cost_status'],'unknown')
        self.assertNotIn('avg_cost',x)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state'/'reviews.db'
        self.s=ReviewSchedule(self.path)

    def test_persisted_cooldown_and_independent_coins(self):
        self.assertTrue(self.s.claim('SOL',100))
        self.assertFalse(ReviewSchedule(self.path).claim('SOL',101))
        self.assertTrue(self.s.claim('ETH',101))
        self.assertTrue(self.s.claim('SOL',14500))
        self.assertFalse(self.s.claim('SOL',99))
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)

    def test_shallow_hold_does_not_block_review_and_no_empty_positions(self):
        run=Mock(return_value=True);event=Mock()
        watch=[('SOL','solusdt'),('ETH','ethusdt')]
        review_cycle(watch,{'SOL':{'quantity':'.1'}},self.s,100,run,event)
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs['review_only'])
        self.assertEqual(run.call_args.kwargs['trigger'],'position_review')

    def test_failed_review_does_not_retry_every_scan(self):
        from clients.committee_coordination import coordinated_run
        runner=Mock(side_effect=RuntimeError('fake'));event=Mock()
        def run(*args,**kw):
            return coordinated_run(*args,kw['scan_id'],False,kw['trigger'],runner,event)
        with patch.dict(os.environ,{'HOME':self.tmp.name}),patch('clients.committee_coordination.time.time',return_value=100):
            for now in (100,101):review_cycle([('SOL','solusdt')],{'SOL':{'quantity':1}},self.s,now,run,event)
        self.assertEqual(runner.call_count,1)


class CommitteeIntegrationTests(unittest.TestCase):
    def run_chain(self,action='SELL',cash='0',review_only=True,guard=None):
        # Compile the real patched entry points, excluding module startup/config.
        tree=ast.parse((ROOT/'seven_roles_committee.py').read_text())
        selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('run_committee','_run_committee')]
        ns=dict(os=os,CommitteeUnavailable=type('CommitteeUnavailable',(Exception,),{}),
            dlog=None,_ROLE_CALLS=[],_ev=Mock(),send_simple_message=Mock(return_value=True),
            fetch_real_crypto_data=Mock(return_value=('market evidence',90)),
            fetch_real_crypto_news=Mock(return_value='news'),ROLE_MODELS=dict.fromkeys(
                ['tech_analyst','fund_analyst','sent_analyst','bull_researcher','bear_researcher','risk_manager','trader'],'fake'),
            require_role=lambda role,result:result,parse_json_safely=parse_command,
            execute_order=Mock(return_value={'success':False,'detail':'offline fixture'}),
            trade_executor=types.SimpleNamespace(DRY_RUN=False),DRY_RUN=False,
            WECHAT_MAX_BYTES=1900,_clip=lambda text,n:text[:n])
        prompts=[]
        def ask(role,prompt,*args,**kwargs):
            prompts.append((role,prompt))
            return json.dumps(dict(action=action,percentage=100 if action=='SELL' else 10 if action=='BUY' else 0,reason='test')) if role=='trader' else 'role analysis'
        ns['ask_agent']=ask
        exec(compile(ast.Module(body=selected,type_ignores=[]),'real_committee','exec'),ns)
        context=dict(quantity='1',available_usdt=cash,cost_status='queried_history_estimate',
                     avg_cost='100',cost_usdt='100',unrealized_usdt='-10',unrealized_pct='-10')
        with patch.dict(sys.modules,{'crypto_monitor':types.SimpleNamespace(analyze_symbol=lambda *a:({'rsi':30},None))}), \
             patch('clients.position_review.position_context',return_value=context),contextlib.redirect_stdout(io.StringIO()):
            if guard is None:
                result=ns['run_committee']('SOL','solusdt','test',scan_id='offline-1',review_only=review_only)
            else:
                result=ns['_run_committee']('SOL','solusdt','test',scan_id='offline-1',
                                           review_only=review_only,execution_guard=guard)
        return result,ns,prompts

    def test_seven_role_review_with_zero_cash_and_position_cost(self):
        result,ns,prompts=self.run_chain()
        self.assertTrue(result);self.assertEqual(len(prompts),7)
        self.assertIn('剩余成本 100',prompts[0][1])
        self.assertIn('浮动盈亏 -10',dict(prompts)['risk_manager'])
        self.assertIn('剩余成本 100',dict(prompts)['trader'])
        ns['execute_order'].assert_not_called()
        self.assertIn('未提交订单',ns['send_simple_message'].call_args.args[0])

    def test_existing_sell_path_does_not_require_cash(self):
        result,ns,_=self.run_chain(review_only=False)
        self.assertTrue(result);ns['execute_order'].assert_called_once()
        self.assertEqual(ns['execute_order'].call_args.args[1],'SELL')

    def test_zero_cash_buy_blocked_before_order(self):
        result,ns,_=self.run_chain(action='BUY',review_only=False)
        self.assertFalse(result);ns['execute_order'].assert_not_called()

    def test_hold_review_never_orders(self):
        result,ns,_=self.run_chain(action='HOLD')
        self.assertTrue(result);ns['execute_order'].assert_not_called()

    def test_seven_role_sell_uses_guard_before_executor_and_reconciles_return(self):
        guard=Mock();guard.observe.return_value={'success':False,'status':'unconfirmed','exchange_confirmed':False}
        result,ns,_=self.run_chain(review_only=False,guard=guard)
        self.assertTrue(result)
        guard.prepare.assert_called_once_with('SOL/USDT','SELL',100,'committee:offline-1:SOL:SELL')
        ns['execute_order'].assert_called_once()
        guard.observe.assert_called_once()
        self.assertFalse(next(c for c in ns['_ev'].call_args_list if c.args[0]=='order_result').kwargs['exchange_confirmed'])


if __name__=='__main__':
    def blocked(*a,**kw):raise AssertionError('network forbidden in offline tests')
    socket.socket.connect=blocked;socket.create_connection=blocked;socket.getaddrinfo=blocked
    unittest.main()
