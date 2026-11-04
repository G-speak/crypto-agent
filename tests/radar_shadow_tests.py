import ast
from datetime import datetime, timezone, timedelta
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clients.radar_shadow import record_candidate, VERSION
spec = importlib.util.spec_from_file_location('evaluate_shadow', ROOT/'tools/evaluate_radar_shadow.py')
evaluator = importlib.util.module_from_spec(spec); spec.loader.exec_module(evaluator)


def candidate():
    return dict(event='radar_shadow_candidate', rule_version=VERSION, candidate_id='s:BTC',
                scan_id='s', coin='BTC', price=100, ts='2026-10-26T00:00:00+08:00')


def scan(hours, price=101):
    return dict(event='scan', ts=(datetime(2026,10,26,tzinfo=timezone(timedelta(hours=8)))+
                                timedelta(hours=hours)).isoformat(),
                coins=[dict(coin='BTC',price=price)])


class ShadowTests(unittest.TestCase):
    def test_only_valid_hold_and_no_mutation(self):
        emit=Mock(); data={'price':100}; dec={'action':'HOLD','reason':'指标冲突'}
        self.assertTrue(record_candidate(emit,'s','BTC',data,dec))
        self.assertEqual(data,{'price':100});self.assertEqual(dec,{'action':'HOLD','reason':'指标冲突'})
        self.assertEqual(emit.call_args.kwargs['execution'],'observation_only')
        for action in ('BUY','SELL','WAIT',None):
            emit.reset_mock()
            self.assertFalse(record_candidate(emit,'s','BTC',data,dict(action=action,reason='reason')))
            emit.assert_not_called()

    def test_failures_and_invalid_inputs_excluded(self):
        emit=Mock()
        for reason in ('AI 输出解析失败，默认持有','所有模型调用失败','',None):
            self.assertFalse(record_candidate(emit,'s','BTC',{'price':100},dict(action='HOLD',reason=reason)))
        for price in (0,-1,float('nan'),float('inf'),None,True):
            self.assertFalse(record_candidate(emit,'s','BTC',{'price':price},dict(action='HOLD',reason='conflict')))
        emit.assert_not_called()

    def test_first_future_scan_no_cherry_picking(self):
        report=evaluator.evaluate([candidate(),scan(3.99,999),scan(4.01,101),scan(4.05,200)])
        result=report['candidates'][0]['horizons']['4']
        self.assertEqual(result['price'],101)
        self.assertAlmostEqual(result['hypothetical_long_net_pct'],(1.01*.999**2/1.001**2-1)*100)
        self.assertEqual(report['candidates'][0]['horizons']['24']['status'],'pending')

    def test_late_missing_and_duplicates(self):
        report=evaluator.evaluate([candidate(),candidate(),scan(5),scan(25)])
        self.assertEqual(report['candidate_count'],1)
        self.assertEqual(report['candidates'][0]['horizons']['4']['status'],'missing_data')
        self.assertEqual(report['candidates'][0]['horizons']['24']['status'],'missing_data')

    def test_cost_and_bad_prices(self):
        for cost in (-1,float('nan'),float('inf'),10000):
            with self.assertRaises(ValueError):evaluator.evaluate([],cost)
        report=evaluator.evaluate([candidate(),scan(4,float('nan')),scan(4.01,100)])
        self.assertLess(report['candidates'][0]['horizons']['4']['hypothetical_long_net_pct'],0)

    def run_push(self, action, broken=False):
        source=ast.parse((ROOT/'alert_monitor.py').read_text())
        fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='push_alerts')
        ns=dict(os=os, datetime=datetime, timezone=timezone,timedelta=timedelta,
                MAJOR_COINS=['BTC'],COIN_ALERT_PUSH_ENABLED=False,
                _is_yunwu_ready=lambda:True,log=Mock(),_notify_ai_down_once=Mock(),
                _fetch_coin_news=Mock(return_value=''),_ai_health_reset=Mock(),_ev=Mock())
        execute=Mock(side_effect=AssertionError('unexpected direct trade'));ns['_execute_trade']=execute
        send=Mock(); committee=Mock(return_value=True); model=Mock(return_value=dict(action=action,reason='指标冲突'))
        modules={'wechat_push':types.SimpleNamespace(send_simple_message=send),
                 'crypto_monitor':types.SimpleNamespace(WATCHLIST=[('BTC','btcusdt')],build_prompt=Mock(),ask_ai=Mock(),
                                                         analyze_symbol=Mock(return_value=({'price':100},None))),
                 'clients.json_prompt':types.SimpleNamespace(build_json_prompt=Mock(return_value='test'),ask_ai_json=model),
                 'seven_roles_committee':types.SimpleNamespace(run_committee=committee)}
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'monitor','exec'),ns)
        with patch.dict(sys.modules,modules):
            if broken:
                with patch('clients.radar_shadow.record_candidate',side_effect=OSError('disk')):
                    ns['push_alerts'](['BTC alert'],'s')
            else:ns['push_alerts'](['BTC alert'],'s')
        execute.assert_not_called();model.assert_called_once()
        return ns,send,committee

    def test_hold_integration_records_without_wake_or_send(self):
        ns,send,committee=self.run_push('HOLD')
        send.assert_not_called();committee.assert_not_called()
        self.assertIn('radar_shadow_candidate',[c.args[0] for c in ns['_ev'].call_args_list])

    def test_existing_buy_sell_routes_preserved(self):
        for action in ('BUY','SELL'):
            for broken in (False,True):
                with self.subTest(action=action,broken=broken):
                    ns,send,committee=self.run_push(action,broken)
                    committee.assert_called_once();send.assert_called_once()
                    self.assertNotIn('radar_shadow_candidate',[c.args[0] for c in ns['_ev'].call_args_list])

    def test_observer_failure_isolated(self):
        ns,send,committee=self.run_push('HOLD',True)
        send.assert_not_called();committee.assert_not_called()
        self.assertIn('radar_shadow_error',[c.args[0] for c in ns['_ev'].call_args_list])

if __name__=='__main__':unittest.main()
