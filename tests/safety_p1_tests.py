"""Behavior tests for isolated P1. Run with work/guard on PYTHONPATH."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME_TMP = tempfile.TemporaryDirectory(prefix='crypto_p1_tests_')
os.environ['HOME'] = HOME_TMP.name
for name in ('GATEIO_API_KEY', 'GATEIO_API_SECRET', 'GATEIO_ENV_PATH', 'YUNWU_API_KEY'):
    os.environ.pop(name, None)
import clients.gateio_trade as gt
import seven_roles_committee as committee
from clients.trade_policy import parse_command
from clients import decision_log


class PolicyTests(unittest.TestCase):
    def test_invalid_complete_commands(self):
        cases = [None, '', '[]', 'null', '123', 'true',
                 'BUY percentage:20', 'prefix {"action":"BUY","percentage":20,"reason":"x"}',
                 '```json\n{"action":"BUY","percentage":20,"reason":"x"}\n```',
                 '{"action":"BUY","action":"HOLD","percentage":0,"reason":"x"}']
        base = {'action': 'BUY', 'percentage': 20, 'reason': 'ok'}
        for pct in (True, False, 0, -1, 9, 51, 101, 20.0, '20', None):
            cases.append(json.dumps({**base, 'percentage': pct}))
        for action in ('buy', 'CANCEL', None, 1):
            cases.append(json.dumps({**base, 'action': action}))
        for reason in ('', '  ', None, 1):
            cases.append(json.dumps({**base, 'reason': reason}))
        cases += [json.dumps({**base, 'extra': 1}), '{"action":"BUY","percentage":NaN,"reason":"x"}',
                  '{"action":"BUY","percentage":Infinity,"reason":"x"}',
                  '{"action":"BUY","reason":"x"}']
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_command(raw)

    def test_valid_boundaries_and_hold(self):
        for action, amounts in [('BUY', (10, 50)), ('SELL', (30, 100)), ('HOLD', (0,))]:
            for pct in amounts:
                with self.subTest(action=action, percentage=pct):
                    command = {'action': action, 'percentage': pct, 'reason': 'test'}
                    self.assertEqual(parse_command(json.dumps(command)), command)
        for action, pct in [('HOLD', 1), ('SELL', 0), ('SELL', 29), ('SELL', 101)]:
            with self.assertRaises(ValueError):
                parse_command(json.dumps({'action': action, 'percentage': pct, 'reason': 'test'}))


class ExecutorTests(unittest.TestCase):
    def test_paper_default(self):
        self.assertIs(gt.DRY_RUN, True)

    def test_live_denied_before_any_account_or_credential_access(self):
        with patch.object(gt, 'DRY_RUN', False), patch.object(gt, '_paper_holdings') as holdings, \
                patch.object(gt, '_load_env') as secrets, patch.object(gt, '_get_exchange') as exchange, \
                patch.object(gt, '_paper_trade') as paper:
            for invocation in ('', 'forged-systemd-id'):
                with patch.dict(os.environ, {'INVOCATION_ID': invocation, 'GATEIO_API_KEY': 'FAKE'}):
                    result = gt.execute_order('BTC/USDT', 'BUY', percentage=20)
                    self.assertFalse(result['success'])
                    self.assertEqual(result['status'], 'blocked_live')
            for mock in (holdings, secrets, exchange, paper): mock.assert_not_called()

    def test_invalid_direct_calls_never_reach_account_or_paper(self):
        cases = [dict(action='WHAT', percentage=20), dict(action='buy', percentage=20),
                 dict(action='BUY', percentage=0), dict(action='BUY', percentage=True),
                 dict(action='BUY', percentage=-1), dict(action='BUY', percentage=51),
                 dict(action='SELL', percentage=0), dict(action='HOLD', percentage=20),
                 dict(action='BUY', percentage=20, amount_usdt=float('nan')),
                 dict(action='BUY', percentage=20, amount_usdt=float('inf')),
                 dict(action='BUY', percentage=20, amount_usdt=-1),
                 dict(action='BUY', percentage=20, amount_usdt=True),
                 dict(action='BUY', percentage=20, amount_usdt=10**400)]
        with patch.object(gt, '_paper_holdings') as holdings, patch.object(gt, '_paper_trade') as paper:
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    result = gt.execute_order('BTC/USDT', **kwargs)
                    self.assertFalse(result['success'])
                    self.assertEqual(result['status'], 'rejected_input')
            holdings.assert_not_called(); paper.assert_not_called()

    def test_valid_order_is_paper_only_even_if_global_flips(self):
        def flip_mode():
            gt.DRY_RUN = False
            return {}
        with patch.object(gt, 'DRY_RUN', True), \
                patch.object(gt, '_paper_holdings', side_effect=flip_mode), \
                patch.object(gt, '_load_ledger', return_value=[]), \
                patch.object(gt, '_paper_trade', return_value={'success': True}) as paper, \
                patch.object(gt, '_load_env') as secret:
            result = gt.execute_order('BTC/USDT', 'BUY', percentage=20)
            self.assertEqual(result['status'], 'paper_filled')
            self.assertFalse(result['exchange_confirmed'])
            paper.assert_called_once(); secret.assert_not_called()

    def test_invalid_quote_never_writes_paper_ledger(self):
        for price in (0, -1, float('nan'), float('inf'), None):
            with self.subTest(price=price), patch.object(gt, '_fetch_current_price', return_value=price), \
                    patch.object(gt, '_save_ledger') as save:
                result = gt._paper_trade('BTC/USDT', 'BTC', 'BUY', 10, percentage=20)
                self.assertFalse(result['success']); save.assert_not_called()

    def test_empty_sell_is_not_a_fill(self):
        with patch.object(gt, '_paper_holdings', return_value={}), patch.object(gt, '_paper_trade') as paper:
            result = gt.execute_order('BTC/USDT', 'SELL', percentage=100)
            self.assertFalse(result['success'])
            self.assertEqual(result['status'], 'skipped_no_holdings')
            paper.assert_not_called()


class CommitteeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        for target, name, value in [
            (committee, 'fetch_real_crypto_data', ('safe fixture', 100.0)),
            (committee, 'fetch_real_crypto_news', 'fixture news'),
            (committee, '_get_holdings', {}),
            (committee, '_get_initial_capital_usdt', 100.0)]:
            self.stack.enter_context(patch.object(target, name, return_value=value))
        import crypto_monitor
        self.stack.enter_context(patch.object(crypto_monitor, 'analyze_symbol', return_value=({'price': 100}, None)))
        self.notify = self.stack.enter_context(patch.object(committee, 'send_simple_message'))
        self.execute = self.stack.enter_context(patch.object(committee, 'execute_order', return_value={
            'success': True, 'fill_price': 100, 'quantity': 1}))
        self.stack.enter_context(patch.object(decision_log, 'BASE_DIR', HOME_TMP.name+'/decisions'))
        self.stack.enter_context(patch.object(gt, 'DRY_RUN', True))

    def role(self, name, *a, **k):
        text = '{"action":"BUY","percentage":20,"reason":"fixture"}' if name == 'trader' else 'fixture reasoning'
        return committee.RoleResult(True, text)

    def test_each_of_seven_failures_blocks_order(self):
        for failed in committee.ROLE_MODELS:
            with self.subTest(failed=failed):
                self.execute.reset_mock(); called = []
                def ask(name, *a, **k):
                    called.append(name)
                    return committee.RoleResult(False, error='fixture-timeout') if name == failed else self.role(name)
                with patch.object(committee, 'ask_agent', side_effect=ask):
                    self.assertIs(committee.run_committee('BTC', 'btcusdt'), False)
                self.execute.assert_not_called()
                self.assertEqual(called[-1], failed)

    def test_untyped_or_empty_role_cannot_be_treated_as_success(self):
        for value in ('BUY', None, committee.RoleResult(True, ''), committee.RoleResult(1, 'text')):
            with self.subTest(value=value), patch.object(committee, 'ask_agent', return_value=value):
                self.assertFalse(committee.run_committee('BTC', 'btcusdt'))
                self.execute.assert_not_called()

    def test_bad_trader_payload_cannot_reach_executor(self):
        for raw in ('[]', '{"action":"BUY","percentage":0,"reason":"bad"}',
                    '{"action":"BUY","percentage":true,"reason":"bad"}',
                    'bad JSON BUY percentage:20', '{"action":"BUY","reason":"bad"}'):
            def ask(name, *a, **k):
                return committee.RoleResult(True, raw) if name == 'trader' else self.role(name)
            with self.subTest(raw=raw), patch.object(committee, 'ask_agent', side_effect=ask):
                self.assertFalse(committee.run_committee('BTC', 'btcusdt'))
                self.execute.assert_not_called()

    def test_valid_command_reaches_executor_exactly_once(self):
        with patch.object(committee, 'ask_agent', side_effect=self.role):
            self.assertTrue(committee.run_committee('BTC', 'btcusdt'))
        self.execute.assert_called_once_with('btcusdt', 'BUY', amount_usdt=10, coin_name='BTC', percentage=20, request_id=None)

    def test_bad_capital_and_live_mode_stop_early(self):
        for capital in (0, -1, float('nan'), float('inf')):
            with self.subTest(capital=capital), patch.object(committee, '_get_initial_capital_usdt', return_value=capital), \
                    patch.object(committee, 'ask_agent') as ask:
                self.assertFalse(committee.run_committee('BTC', 'btcusdt'))
                ask.assert_not_called(); self.execute.assert_not_called()
        with patch.object(gt, 'DRY_RUN', False):
            self.notify.reset_mock()
            self.assertFalse(committee.run_committee('BTC', 'btcusdt'))
            self.notify.assert_not_called(); self.execute.assert_not_called()

    def test_transport_error_is_typed_failure_even_if_text_has_no_failure_marker(self):
        with patch.object(committee.requests, 'post', side_effect=OSError('fixture-error')), \
                patch.object(committee.time, 'sleep'):
            result = committee.ask_agent('risk_manager', 'fixture', max_retries=1)
        self.assertIs(type(result), committee.RoleResult)
        self.assertIs(result.ok, False)


class RadarThresholdTests(unittest.TestCase):
    def test_threshold_boundaries_both_directions(self):
        import alert_monitor as monitor
        import crypto_monitor
        for coin, threshold in [('BTC', 3), ('ALT', 7)]:
            for change, expected in [(threshold-.01, False), (threshold, True), (threshold+.01, True),
                                     (-(threshold-.01), False), (-threshold, True), (-(threshold+.01), True)]:
                with self.subTest(coin=coin, change=change):
                    fixture = {'price': 100+change, 'change_24h': 0, 'rsi': 50, 'bb_lower': 0, 'bb_upper': 0}
                    with patch.object(crypto_monitor, 'WATCHLIST', [(coin, coin.lower()+'usdt')]), \
                            patch.object(crypto_monitor, 'analyze_symbol', return_value=(fixture,None)), \
                            patch.object(monitor, 'load_json', side_effect=[{coin:{'price':100}},{}]), \
                            patch.object(monitor, 'save_json'), patch.object(monitor, '_ev'):
                        alerts, sid=monitor.check_alerts()
                    self.assertEqual(any('价格异动' in item for item in alerts),expected)

    def test_cooldown_exact_expiry(self):
        import alert_monitor as monitor
        with patch.object(monitor.time, 'time', return_value=10000), patch.object(monitor, '_ev'):
            state={'BTC:price_spike':10000-monitor.ALERT_COOLDOWN+.01}
            self.assertTrue(monitor.is_on_cooldown('BTC','price_spike',state))
            state={'BTC:price_spike':10000-monitor.ALERT_COOLDOWN}
            self.assertFalse(monitor.is_on_cooldown('BTC','price_spike',state))
            self.assertEqual(state['BTC:price_spike'],10000)


class DiagnosticTests(unittest.TestCase):
    def test_once_never_reads_runtime_or_opens_network_or_writes(self):
        script = '''import sys,runpy,json
sys.dont_write_bytecode=True
sys.path.insert(0,sys.argv[1])
violations=[]
def audit(event,args):
    if event in ('socket.connect','socket.getaddrinfo','subprocess.Popen','os.mkdir','os.rename','os.remove','os.chmod'):
        violations.append(event); raise AssertionError(event)
    if event=='open':
        path,mode,flags=args
        if isinstance(path,str) and (path.endswith(('.json','.env')) or '.hermes' in path):
            violations.append('runtime-read');raise AssertionError('runtime-read')
        if (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & 3):
            violations.append('write');raise AssertionError('write')
sys.addaudithook(audit)
sys.argv=['alert_monitor.py','--once']
try:runpy.run_path(sys.path[0]+'/alert_monitor.py',run_name='__main__')
except SystemExit as exc:code=exc.code
assert code==0, code
assert violations==[],violations
assert not any(n in sys.modules for n in ('wechat_config','requests','wechat_push','clients.gateio_trade','seven_roles_committee'))
print('ISOLATION_OK')
'''
        with tempfile.TemporaryDirectory() as home:
            state = Path(home)/'.hermes'; state.mkdir(); sentinel=state/'alert_cooldown_mom.json'
            sentinel.write_text('{"keep":1}')
            env = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE='1', GATEIO_ENV_PATH=str(sentinel))
            p = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT)], env=env,
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stderr+p.stdout)
            self.assertIn('ISOLATION_OK',p.stdout)
            result=json.loads(p.stdout.splitlines()[0])
            self.assertEqual(result['mode'],'offline_source_check')
            self.assertFalse(result['market_scan_performed'])
            self.assertEqual(sentinel.read_text(),'{"keep":1}')
            self.assertEqual(list(state.iterdir()),[sentinel])

    def test_broken_source_reports_failure(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'clients').mkdir()
            for name in ('offline_diagnostic.py','alert_monitor.py','seven_roles_committee.py','wechat_server.py',
                         'clients/gateio_trade.py','clients/trade_policy.py','clients/decision_log.py','clients/callback_guard.py'):
                shutil.copyfile(ROOT/name,root/name)
            (root/'clients/trade_policy.py').write_text('this is not valid python !')
            result=subprocess.run([sys.executable,'-B',str(root/'alert_monitor.py'),'--once'],
                                  capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertFalse(json.loads(result.stdout)['ok'])

if __name__=='__main__':
    unittest.main(verbosity=2)
