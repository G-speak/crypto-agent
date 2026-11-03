#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1 / P0-5 自动化测试（在 staging 目录内运行；零真实下单、零微信、零外网）

覆盖：
  T1 decision_log：脱敏 / 落盘 / 写失败隔离 / 轮转
  T2 gateio_trade 自测块：默认禁用、强制纸面、真实下单调用 0 次
  T3 seven_roles __main__：默认禁用、自测模式下纸面 + 无微信
  T4 alert_monitor --once：离线源码检查、下单与微信调用 0 次、状态无变化
  T5 单实例锁：并发第二个实例必须被拒
  T6 无行为漂移：提示词哈希 / 仓位上限 / 模型绑定 / 冷却与阈值常量不变

运行：python3 tests/p0_tests.py       （cwd = staging 根目录）
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# ── 隔离：临时 HOME（所有 ~/.hermes 状态与日志都落这里，不动生产状态）──
TMP_HOME = tempfile.mkdtemp(prefix="p0home_")
os.environ["HOME"] = TMP_HOME
os.environ["CRYPTO_CLIENT"] = "mom"

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + extra) if extra else ""))


# ── 假 ccxt：任何真实下单调用都会被计数并抛错 ──
ORDER_CALLS = []


class FakeExchange:
    def __init__(self):
        self.markets = {
            "BTC/USDT": {"limits": {"cost": {"min": 3.0}, "amount": {"min": 1e-6}},
                         "precision": {"amount": 1e-6, "price": 0.1}},
            "ETH/USDT": {"limits": {"cost": {"min": 3.0}, "amount": {"min": 0.001}},
                         "precision": {"amount": 0.0001, "price": 0.01}},
            "SOL/USDT": {"limits": {"cost": {"min": 3.0}, "amount": {"min": 0.001}},
                         "precision": {"amount": 0.001, "price": 0.01}},
            "BNB/USDT": {"limits": {"cost": {"min": 3.0}, "amount": {"min": 0.001}},
                         "precision": {"amount": 0.001, "price": 0.1}},
        }

    def load_markets(self):
        return self.markets

    def fetch_ticker(self, sym):
        return {"last": 100.0}

    def amount_to_precision(self, sym, amt):
        return amt

    def create_market_order(self, **kw):
        ORDER_CALLS.append(kw)
        raise AssertionError("REAL ORDER ATTEMPTED: %r" % (kw,))


fake_ccxt = types.ModuleType("ccxt")
fake_ccxt.gate = lambda *a, **k: FakeExchange()
sys.modules["ccxt"] = fake_ccxt

# ── 微信隔离：在导入 seven_roles 前替换 send_simple_message 的模块属性 ──
import wechat_push
SENT = []
wechat_push.send_simple_message = lambda *a, **k: SENT.append(a)

# ── 网络隔离：requests.post 一律抛错（AI/推送/探活都走它）──
import requests
_real_post = requests.post


def _no_net(*a, **k):
    raise requests.exceptions.ConnectionError("blocked-by-p0-test")


requests.post = _no_net

print("=" * 78)
print("P0 测试开始 | staging =", BASE)
print("临时 HOME =", TMP_HOME)
print("=" * 78)

# ══════════════ T1 decision_log ══════════════
from clients import decision_log as dlog

dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_test")
r1 = dlog.event("unit", coin="BTC", note="key=sk-abcdefgh12345678 token=0123456789abcdef0123456789abcdef")
check("T1a 写入成功", r1 is True)
files = os.listdir(dlog.BASE_DIR)
content = open(os.path.join(dlog.BASE_DIR, files[0]), encoding="utf-8").read()
check("T1b 已脱敏(sk-/长串)", ("sk-abcdefgh" not in content) and ("0123456789abcdef0123456789abcdef" not in content),
      content.strip()[:90])
check("T1c 产出合法 JSON", isinstance(json.loads(content.strip().splitlines()[0]), dict))

# 写失败隔离：指向不可写路径
dlog.BASE_DIR = "/proc/p0_cannot_write"
r2 = dlog.event("unit", coin="BTC")
check("T1d 写失败返回 False 且不抛异常", r2 is False)

# 轮转：超过 MAX_BYTES 时切 .1
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_rot")
dlog.MAX_BYTES = 100
for i in range(6):
    dlog.event("rot", i=i, pad="x" * 40)
rot_files = sorted(os.listdir(dlog.BASE_DIR))
check("T1e 超限自动轮转 .1", any(f.endswith(".jsonl.1") for f in rot_files), str(rot_files))
dlog.MAX_BYTES = 20 * 1024 * 1024

# ══════════════ T2 gateio_trade 自测块 ══════════════
import runpy

os.environ.pop("GATEIO_SELFTEST", None)
ORDER_CALLS.clear()
code = None
try:
    runpy.run_path(os.path.join(BASE, "clients", "gateio_trade.py"), run_name="__main__")
except SystemExit as e:
    code = e.code
check("T2a 默认禁用(退出码0)", code == 0, "exit=%r" % (code,))
check("T2b 默认禁用时真实下单=0", len(ORDER_CALLS) == 0, "calls=%d" % len(ORDER_CALLS))

os.environ["GATEIO_SELFTEST"] = "1"
ORDER_CALLS.clear()
try:
    runpy.run_path(os.path.join(BASE, "clients", "gateio_trade.py"), run_name="__main__")
except SystemExit as e:
    pass
except Exception as e:
    print("   (自测块内部异常，已忽略:", type(e).__name__, str(e)[:60], ")")
check("T2c 自测模式仍不触达交易所", len(ORDER_CALLS) == 0, "calls=%d" % len(ORDER_CALLS))

# ══════════════ T3 seven_roles __main__ ══════════════
os.environ.pop("SELFTEST", None)
ORDER_CALLS.clear()
SENT.clear()
code = None
try:
    runpy.run_path(os.path.join(BASE, "seven_roles_committee.py"), run_name="__main__")
except SystemExit as e:
    code = e.code
check("T3a 默认禁用(退出码0)", code == 0, "exit=%r" % (code,))
check("T3b 默认禁用时下单/微信=0", len(ORDER_CALLS) == 0 and len(SENT) == 0)

os.environ["SELFTEST"] = "1"
ORDER_CALLS.clear()
SENT.clear()
t0 = time.time()
try:
    runpy.run_path(os.path.join(BASE, "seven_roles_committee.py"), run_name="__main__")
except SystemExit:
    pass
except Exception as e:
    print("   (自测模式内部异常，已忽略:", type(e).__name__, str(e)[:60], ")")
check("T3c 自测模式：不触达交易所", len(ORDER_CALLS) == 0, "calls=%d" % len(ORDER_CALLS))
check("T3d 自测模式：不发微信", len(SENT) == 0, "sent=%d" % len(SENT))
check("T3e 自测模式：AI 断网下可控失败(<120s)", time.time() - t0 < 120, "%.1fs" % (time.time() - t0))
os.environ.pop("SELFTEST", None)

# ══════════════ T4 alert_monitor --once ══════════════
import crypto_monitor

FIXTURE = {
    "name": "BTC", "symbol": "btcusdt", "price": 100000.0, "open": 99000.0,
    "change_24h": 1.5, "high_24h": 101000.0, "low_24h": 98000.0,
    "volume_coin": 1000.0, "volume_usdt": 1e8, "rsi": 76.0, "rsi_signal": "超买 🚨",
    "ema20": 99500.0, "ema50": 99000.0, "sma200": 97000.0,
    "macd": 10.0, "macd_signal": 8.0, "macd_histogram": 2.0,
    "bb_upper": 101500.0, "bb_mid": 99500.0, "bb_lower": 97500.0,
    "vol_ratio": 1.2, "price_vs_ema20": "above",
}
crypto_monitor.analyze_symbol = lambda name, symbol: (dict(FIXTURE), None)

# --once now checks source integrity only; it must not create scan state.
from pathlib import Path
import contextlib
import io

def _home_snapshot():
    return {str(p.relative_to(TMP_HOME)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(TMP_HOME).rglob("*") if p.is_file()}

ORDER_CALLS.clear()
SENT.clear()
before_once = _home_snapshot()
stdout_once = io.StringIO()
code = None
saved_argv = sys.argv[:]
try:
    sys.argv = ["alert_monitor.py", "--once"]
    with contextlib.redirect_stdout(stdout_once):
        runpy.run_path(os.path.join(BASE, "alert_monitor.py"), run_name="__main__")
except SystemExit as e:
    code = e.code
finally:
    sys.argv = saved_argv
result_once = json.loads(stdout_once.getvalue())
check("T4a --once 离线检查正常退出", code == 0 and result_once["ok"] is True
      and result_once["market_scan_performed"] is False)
check("T4b --once 下单与微信调用均为零", not ORDER_CALLS and not SENT)
check("T4c --once 不改写 HOME 内任何状态文件", before_once == _home_snapshot())

# ══════════════ T5 单实例锁 ══════════════
import alert_monitor

# T4 里 --once 在本进程内拿了锁，先释放，避免干扰锁测试（仅测试代码操作私有句柄）
try:
    if getattr(alert_monitor, "_LOCK_FH", None):
        alert_monitor._LOCK_FH.close()
        alert_monitor._LOCK_FH = None
except Exception as e:
    print("   (释放测试进程锁失败，忽略:", e, ")")

ACQ = ("import sys; sys.path.insert(0, %r); import alert_monitor as a; "
       "print('GOT' if a.acquire_single_instance_lock() else 'BLOCKED')" % BASE)
HOLD = ("import sys, time; sys.path.insert(0, %r); import alert_monitor as a; "
        "print('GOT' if a.acquire_single_instance_lock() else 'BLOCKED', flush=True); time.sleep(3)" % BASE)
LOCK_HOME = tempfile.mkdtemp(prefix="p0lock_")
ENV = dict(os.environ)
ENV["HOME"] = LOCK_HOME

p1 = subprocess.run([sys.executable, "-c", ACQ], capture_output=True, text=True, env=ENV, timeout=60)
check("T5a 无占用时新进程可拿锁", "GOT" in p1.stdout, (p1.stdout + p1.stderr).strip()[-100:])

holder = subprocess.Popen([sys.executable, "-c", HOLD], stdout=subprocess.PIPE, text=True, env=ENV)
time.sleep(1.2)
p2 = subprocess.run([sys.executable, "-c", ACQ], capture_output=True, text=True, env=ENV, timeout=60)
check("T5b 持锁期间第二个实例被拒", "BLOCKED" in p2.stdout, (p2.stdout + p2.stderr).strip()[-100:])
try:
    holder.wait(timeout=20)
except Exception:
    holder.kill()

p3 = subprocess.run([sys.executable, "-c", ACQ], capture_output=True, text=True, env=ENV, timeout=60)
check("T5c 持锁进程退出后锁自动释放", "GOT" in p3.stdout, (p3.stdout + p3.stderr).strip()[-100:])

# ══════════════ T6 无行为漂移 ══════════════
import ast
src = open(os.path.join(BASE, "seven_roles_committee.py"), encoding="utf-8").read()
tree = ast.parse(src)
vals = {}
for n in tree.body:
    if isinstance(n, ast.Assign):
        nm = getattr(n.targets[0], "id", "")
        if nm in ("AGENT_PROMPTS", "ROLE_MODELS", "AGENT_MAX_TOKENS", "AGENT_MODEL_CHAIN", "MAX_BUY_PCT"):
            vals[nm] = ast.literal_eval(n.value)
h = hashlib.md5(json.dumps(vals["AGENT_PROMPTS"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
check("T6a 七个提示词逐字未变", h == "5184c14083e2473835a53a5c2794eca6", "md5=" + h)
check("T6b MAX_BUY_PCT 未变", vals["MAX_BUY_PCT"] == 50, str(vals["MAX_BUY_PCT"]))
check("T6c 角色→模型绑定未变",
      vals["ROLE_MODELS"] == {"tech_analyst": "gpt-5.6-luna", "fund_analyst": "gpt-5.6-luna",
                             "sent_analyst": "gpt-5.6-luna", "bull_researcher": "gpt-4.1",
                             "bear_researcher": "claude-sonnet-5", "risk_manager": "gpt-5.2",
                             "trader": "gpt-5.6-luna"}, str(vals["ROLE_MODELS"]))
check("T6d 降级链未变", vals["AGENT_MODEL_CHAIN"] == ["deepseek-v4-flash", "deepseek-v4.1-flash", "deepseek-v3-1"])
import wechat_config as wc
check("T6e 冷却/巡检间隔未变", wc.ALERT_COOLDOWN == 7200 and wc.MONITOR_INTERVAL == 300,
      "cooldown=%s interval=%s" % (wc.ALERT_COOLDOWN, wc.MONITOR_INTERVAL))
check("T6f 主流币名单未变（阈值另有行为测试）",
      wc.MAJOR_COINS == ["BTC", "ETH", "SOL", "BNB"], str(wc.MAJOR_COINS))
am = open(os.path.join(BASE, "alert_monitor.py"), encoding="utf-8").read()
check("T6g 预警推送开关仍为 False（未被顺手打开）", "COIN_ALERT_PUSH_ENABLED = False" in am)

# ══════════════ T7 import 不持锁（服务路径不受影响）══════════════
ACQ_EXIT = ("import sys; sys.path.insert(0, %r); import alert_monitor as a; "
            "a.acquire_single_instance_lock(); print('HELD-THEN-EXIT')" % BASE)
p4 = subprocess.run([sys.executable, "-c", ACQ_EXIT], capture_output=True, text=True, env=ENV, timeout=60)
p5 = subprocess.run([sys.executable, "-c", ACQ], capture_output=True, text=True, env=ENV, timeout=60)
check("T7 持锁进程退出后新进程可启动（锁随进程释放）",
      "HELD-THEN-EXIT" in p4.stdout and "GOT" in p5.stdout,
      (p4.stdout + p5.stdout).strip()[-100:])

# ══════════════ 汇总 ══════════════
print("=" * 78)
passed = sum(1 for _, ok_, _e in RESULTS if ok_)
print("结果: %d/%d 通过" % (passed, len(RESULTS)))
print("真实下单调用总数（全程）:", len(ORDER_CALLS),
      "| 被测试桩拦截的微信发送尝试:", len(SENT), "（未触达真实企业微信接口）")
if passed != len(RESULTS):
    print("失败项:", [n for n, ok_, _e in RESULTS if not ok_])
print("=" * 78)
sys.exit(0 if passed == len(RESULTS) else 1)
