#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-A / P0-B / P0-C 修复验证（新增测试，运行于 staging；零真实下单、零微信、零外网）

覆盖：
  A. 单实例锁 fail-closed（fcntl 缺失 / 锁文件打不开 / 加锁异常 / 多进程互斥 / 非截断 / PID 写入时机）
  B. Authorization / Bearer / Basic 脱敏（写入真实 JSONL 后检查文件内容）
  C. 日志目录 700 / 文件 600（首次创建、已存在、轮转后）+ scan_id 端到端传播

运行：python3 tests/p0_fixes_tests.py      （cwd = staging 根目录）
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

TMP_HOME = tempfile.mkdtemp(prefix="p0fix_home_")
os.environ["HOME"] = TMP_HOME
os.environ["CRYPTO_CLIENT"] = "mom"

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("PASS  " if cond else "FAIL  ") + name + (("  | " + extra) if extra else ""))


# ── 假 ccxt（任何真实下单都会计数并抛错）──
ORDER_CALLS = []


class FakeExchange:
    markets = {
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

    def fetch_ticker(self, s):
        return {"last": 100.0}

    def amount_to_precision(self, s, a):
        return a

    def create_market_order(self, **kw):
        ORDER_CALLS.append(kw)
        raise AssertionError("REAL ORDER ATTEMPTED")


fake_ccxt = types.ModuleType("ccxt")
fake_ccxt.gate = lambda *a, **k: FakeExchange()
sys.modules["ccxt"] = fake_ccxt

import wechat_push

_ORIG_SEND = wechat_push.send_simple_message          # 记下真实实现，用于边界断言
SENT = []
wechat_push.send_simple_message = lambda *a, **k: SENT.append(a)

import requests
requests.post = lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("blocked"))

print("=" * 78)
print("P0-A/B/C 修复测试 | staging =", BASE)
print("临时 HOME =", TMP_HOME)
print("=" * 78)

# ══════════════════ A. 单实例锁 ══════════════════
import alert_monitor

ENV = dict(os.environ)
ACQ = ("import sys; sys.path.insert(0, %r); import alert_monitor as a; "
       "print('GOT' if a.acquire_single_instance_lock() else 'BLOCKED:' + a.LOCK_ERROR)" % BASE)

# A1 正常取得
os.environ["HOME"] = TMP_HOME
p = subprocess.run([sys.executable, "-c", ACQ], capture_output=True, text=True, env=ENV, timeout=60)
check("A1 正常情况可取得锁", p.stdout.startswith("GOT"), p.stdout.strip()[:80])

# A2 真实多进程互斥：5 个同时抢，恰好 1 个成功
HOLD = ("import sys, time; sys.path.insert(0, %r); import alert_monitor as a; "
        "ok = a.acquire_single_instance_lock(); print('GOT' if ok else 'BLOCKED', flush=True); "
        "time.sleep(2.5)" % BASE)
procs = [subprocess.Popen([sys.executable, "-c", HOLD], stdout=subprocess.PIPE, text=True, env=ENV)
         for _ in range(5)]
outs = [pr.communicate(timeout=30)[0].strip() for pr in procs]
check("A2 多进程同时抢锁：恰好 1 个成功", outs.count("GOT") == 1, "outs=%s" % outs)

# A3 锁文件无法打开（指向一个目录）→ 必须 fail-closed
d = tempfile.mkdtemp(prefix="p0lockdir_")
alert_monitor.LOCK_FILE = d          # 用目录冒充锁文件 → open('a+') 必失败
r = alert_monitor.acquire_single_instance_lock()
check("A3 锁文件打不开 → 拒绝（fail-closed）", r is False, "LOCK_ERROR=" + alert_monitor.LOCK_ERROR[:70])

# A4 fcntl 不可用 → 必须 fail-closed
alert_monitor.LOCK_FILE = os.path.join(TMP_HOME, "lock_a4.lock")
saved = sys.modules.get("fcntl", "MISSING")
sys.modules["fcntl"] = None          # import fcntl → ImportError
try:
    r = alert_monitor.acquire_single_instance_lock()
finally:
    if saved == "MISSING":
        sys.modules.pop("fcntl", None)
    else:
        sys.modules["fcntl"] = saved
check("A4 fcntl 缺失 → 拒绝（fail-closed）", r is False, "LOCK_ERROR=" + alert_monitor.LOCK_ERROR[:70])

# A5 flock 抛异常 → 必须 fail-closed
class BoomFcntl:
    LOCK_EX, LOCK_NB = 1, 2

    class flock:
        def __init__(self, *a, **k):
            raise OSError("simulated flock failure")


saved = sys.modules.get("fcntl", "MISSING")
sys.modules["fcntl"] = BoomFcntl
try:
    r = alert_monitor.acquire_single_instance_lock()
finally:
    if saved == "MISSING":
        sys.modules.pop("fcntl", None)
    else:
        sys.modules["fcntl"] = saved
check("A5 加锁异常 → 拒绝（fail-closed）", r is False, "LOCK_ERROR=" + alert_monitor.LOCK_ERROR[:70])

# A6 抢锁失败不得清空既有 PID（非截断打开）
import time as _t
lock6 = os.path.join(TMP_HOME, "lock_a6.lock")
open(lock6, "w").write("424242")
ACQ6 = ("import sys, os; sys.path.insert(0, %r); import alert_monitor as a; "
        "a.LOCK_FILE = %r; print('GOT' if a.acquire_single_instance_lock() else 'BLOCKED')"
        % (BASE, lock6))
holder = subprocess.Popen([sys.executable, "-c", ACQ6 + "; import time; time.sleep(2.5)"],
                          stdout=subprocess.PIPE, text=True, env=ENV)
_t.sleep(0.8)
p2 = subprocess.run([sys.executable, "-c", ACQ6], capture_output=True, text=True, env=ENV, timeout=30)
content = open(lock6).read().strip()
# 持锁进程取得锁后会写入自己的 PID；被拒的进程绝不能把内容清空（旧实现用 "w" 会截断成空）
check("A6 抢锁失败不清空持锁进程的 PID",
      p2.stdout.strip() == "BLOCKED" and content.isdigit() and content == str(holder.pid),
      "out=%s file=%r holder_pid=%d" % (p2.stdout.strip(), content[:20], holder.pid))
try:
    holder.kill()
except Exception:
    pass

# A7 只有拿到锁后才写入 PID
lock7 = os.path.join(TMP_HOME, "lock_a7.lock")
ACQ7 = ("import sys, os; sys.path.insert(0, %r); import alert_monitor as a; "
        "a.LOCK_FILE = %r; print('GOT' if a.acquire_single_instance_lock() else 'BLOCKED')"
        % (BASE, lock7))
p3 = subprocess.run([sys.executable, "-c", ACQ7], capture_output=True, text=True, env=ENV, timeout=30)
written = open(lock7).read().strip()
check("A7 取得锁后 PID 被写入且为数字", p3.stdout.strip() == "GOT" and written.isdigit(), "pid=%r" % written)

# ══════════════════ B. 脱敏（写文件后检查）══════════════════
from clients import decision_log as dlog

dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_redact")
SECRETS = {
    "auth_bearer": "FAKE_AUTH_0001",
    "auth_lower": "FAKE_LOWER_0002",
    "auth_basic": "FAKE_BASIC_0003",
    "standalone": "FAKE_STAND_0004",
    "short": "FAKE5",
    "nested": "FAKE_NEST_0006",
    "exc": "FAKE_EXC_0007",
    "mixed": "FAKE_MIX_0008",
}
dlog.event("t_auth", note="Authorization: Bearer " + SECRETS["auth_bearer"])
dlog.event("t_auth_lower", note="authorization: bearer " + SECRETS["auth_lower"])
dlog.event("t_basic", note="Authorization:Basic " + SECRETS["auth_basic"])
dlog.event("t_standalone", note="headerless " + "Bearer " + SECRETS["standalone"] + " end")
dlog.event("t_short", note="bearer " + SECRETS["short"])
dlog.event("t_nested", payload={"headers": {"Authorization": "Bearer " + SECRETS["nested"]}, "ok": 1})
dlog.event("t_exc", note="requests.HTTPError: 401 Unauthorized Authorization: Bearer " + SECRETS["exc"])
dlog.event("t_mixed", note="key=sk-FAKEabcdefghijkl and Bearer " + SECRETS["mixed"])

blob = ""
for f in os.listdir(dlog.BASE_DIR):
    blob += open(os.path.join(dlog.BASE_DIR, f), encoding="utf-8").read()

leaked = [k for k, v in SECRETS.items() if v in blob]
check("B1 八类 Authorization/Bearer 变体均未泄漏", not leaked, "leaked=" + str(leaked))
check("B2 前缀被保留、值被替换", ("Authorization: ***REDACTED***" in blob) or ("Authorization:" in blob and "Bearer ***REDACTED***" in blob))
check("B3 小写 authorization 也命中", "authorization:" in blob)
check("B4 嵌套 JSON 未泄漏且结构仍在", ('"headers"' in blob) and ('"ok": 1' in blob))
check("B5 sk- 形式仍被脱敏", "sk-FAKEabcdefghijkl" not in blob and "sk-***REDACTED***" in blob)
check("B6 每条事件仍是合法 JSON", all(json.loads(l) for l in blob.strip().splitlines()))

# ══════════════════ C1. 权限 ══════════════════
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_perm")
dlog.event("perm", n=1)
dmode = stat.S_IMODE(os.stat(dlog.BASE_DIR).st_mode)
fpath = os.path.join(dlog.BASE_DIR, sorted(os.listdir(dlog.BASE_DIR))[0])
fmode = stat.S_IMODE(os.stat(fpath).st_mode)
check("C1 目录权限 700", dmode == 0o700, "dir=%o" % dmode)
check("C2 文件权限 600", fmode == 0o600, "file=%o" % fmode)

os.chmod(fpath, 0o644)              # 模拟"历史遗留的宽权限文件"
os.chmod(dlog.BASE_DIR, 0o755)
dlog.event("perm", n=2)
check("C3 已存在文件被纠正回 600", stat.S_IMODE(os.stat(fpath).st_mode) == 0o600,
      "%o" % stat.S_IMODE(os.stat(fpath).st_mode))
check("C4 已存在目录被纠正回 700", stat.S_IMODE(os.stat(dlog.BASE_DIR).st_mode) == 0o700,
      "%o" % stat.S_IMODE(os.stat(dlog.BASE_DIR).st_mode))

dlog.MAX_BYTES = 60                 # 强制轮转
for i in range(4):
    dlog.event("perm_rot", i=i, pad="y" * 40)
rot = [f for f in os.listdir(dlog.BASE_DIR) if f.endswith(".jsonl.1")]
check("C5 轮转文件权限仍为 600",
      bool(rot) and stat.S_IMODE(os.stat(os.path.join(dlog.BASE_DIR, rot[0])).st_mode) == 0o600,
      "rot=%s mode=%o" % (rot, stat.S_IMODE(os.stat(os.path.join(dlog.BASE_DIR, rot[0])).st_mode) if rot else 0))
dlog.MAX_BYTES = 20 * 1024 * 1024

# ══════════════════ C2. scan_id 端到端 ══════════════════
import crypto_monitor
import clients.json_prompt as jp

FIX = {
    "name": "BTC", "symbol": "btcusdt", "price": 100000.0, "open": 99000.0,
    "change_24h": 1.5, "high_24h": 101000.0, "low_24h": 98000.0,
    "volume_coin": 1000.0, "volume_usdt": 1e8, "rsi": 76.0, "rsi_signal": "超买",
    "ema20": 99500.0, "ema50": 99000.0, "sma200": 97000.0,
    "macd": 10.0, "macd_signal": 8.0, "macd_histogram": 2.0,
    "bb_upper": 101500.0, "bb_mid": 99500.0, "bb_lower": 97500.0,
    "vol_ratio": 1.2, "price_vs_ema20": "above",
}
crypto_monitor.analyze_symbol = lambda n, s: (dict(FIX), None)
crypto_monitor.build_prompt = lambda d: ""
crypto_monitor.ask_ai = lambda *a, **k: ""
jp.build_json_prompt = lambda *a, **k: ""
jp.ask_ai_json = lambda *a, **k: {"action": "HOLD", "reason": "p0c-test-hold"}
alert_monitor._is_yunwu_ready = lambda *a, **k: True
alert_monitor._fetch_coin_news = lambda *a, **k: ""

dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_scanid")
_, sid1 = alert_monitor.check_alerts()
_, sid2 = alert_monitor.check_alerts()
check("C6 扫描 ID 每次不同且非空", bool(sid1) and bool(sid2) and sid1 != sid2, "%s vs %s" % (sid1, sid2))

alert_monitor.push_alerts(["\u26a0\ufe0f BTC RSI\u8d85\u4e70\n\u4ef7: $100,000.00  RSI: 76.0\n24h: +1.50%"],
                          scan_id=sid2)
_rows = []
for _f in os.listdir(dlog.BASE_DIR):
    _rows += [json.loads(_x) for _x in open(os.path.join(dlog.BASE_DIR, _f), encoding="utf-8").read().splitlines() if _x.strip()]
_sh = [r for r in _rows if r["event"] == "shallow_decision"]
check("C7 浅层决策与所属扫描同 ID", bool(_sh) and _sh[-1].get("scan_id") == sid2,
      "shallow=%s expect=%s" % (_sh[-1].get("scan_id") if _sh else None, sid2))

import seven_roles_committee as src7
src7.fetch_real_crypto_data = lambda *a, **k: ("价格:$100 | 24H涨跌:+1.0%", 100.0)
src7.fetch_real_crypto_news = lambda *a, **k: ""
src7.ask_agent = lambda role, p, model=None, max_retries=2, coin_name=None: src7.RoleResult(True, (
    '{"action":"HOLD","percentage":0,"reason":"stub-hold"}' if role == "trader" else "stub text"))
src7._get_holdings = lambda *a, **k: {}
src7.get_real_balance = lambda *a, **k: {"USDT": {"free": 50.0}}
src7.send_simple_message = lambda *a, **k: SENT.append(a)


def _boom(*a, **k):
    ORDER_CALLS.append({"boom": True})
    raise AssertionError("execute_order 不应被调用（HOLD）")


src7.execute_order = _boom
sid_e2e = "s-e2e-verify-0001"
src7.run_committee("BTC", "btcusdt", "p0c-e2e", scan_id=sid_e2e)

lines = []
for f in os.listdir(dlog.BASE_DIR):
    lines += [json.loads(x) for x in open(os.path.join(dlog.BASE_DIR, f), encoding="utf-8").read().splitlines() if x.strip()]
by_sid = [r for r in lines if r.get("scan_id") == sid_e2e]
kinds = sorted({r["event"] for r in by_sid})
check("C8 委员会事件共享同一 scan_id", {"committee_start", "committee_decision"} <= set(kinds), str(kinds))
check("C9 HOLD 不触发下单调用", len(ORDER_CALLS) == 0, "calls=%d" % len(ORDER_CALLS))
scans = [r for r in lines if r["event"] == "scan"]
check("C10 不同扫描的 scan_id 不混淆",
      len({r["scan_id"] for r in scans}) >= 2 and sid_e2e not in {r["scan_id"] for r in scans},
      "scans=%d" % len(scans))

# ══════════════════ A8/A9 权限无法纠正 → 写入必须失败 ══════════════════
_real_chmod = os.chmod

# A8：目录权限纠正失败
d8 = tempfile.mkdtemp(prefix="p0perm_dir_")
os.chmod(d8, 0o755)
dlog.BASE_DIR = d8


def _chmod_dir_boom(path, mode, *a, **k):
    if os.path.abspath(path) == os.path.abspath(d8):
        return                      # 静默不纠正，模拟"无法修改权限"
    return _real_chmod(path, mode, *a, **k)


os.chmod = _chmod_dir_boom
try:
    r8 = dlog.event("perm_fail_dir", x=1)
finally:
    os.chmod = _real_chmod
check("A8 目录权限纠正不了 → event 返回 False", r8 is False)
check("A8b 且未向该目录写入任何文件", os.listdir(d8) == [], "files=%s" % os.listdir(d8))

# A9：文件权限纠正失败
d9 = tempfile.mkdtemp(prefix="p0perm_file_")
os.chmod(d9, 0o700)
dlog.BASE_DIR = d9
p9 = dlog._current_path()
open(p9, "w").write("PRE-EXISTING\n")
os.chmod(p9, 0o644)


def _chmod_file_boom(path, mode, *a, **k):
    if os.path.abspath(path) == os.path.abspath(p9):
        return
    return _real_chmod(path, mode, *a, **k)


_real_fchmod = os.fchmod
os.fchmod = lambda fd, mode: None
os.chmod = _chmod_file_boom
try:
    r9 = dlog.event("perm_fail_file", x=1)
finally:
    os.chmod = _real_chmod
    os.fchmod = _real_fchmod
content9 = open(p9, encoding="utf-8").read()
check("A9 文件权限纠正不了 → event 返回 False", r9 is False)
check("A9b 且未向该文件追加任何内容", content9 == "PRE-EXISTING\n", repr(content9[:40]))
check("A9c 该文件权限仍为原始的 644（未被伪装成合规）",
      stat.S_IMODE(os.stat(p9).st_mode) == 0o644, "%o" % stat.S_IMODE(os.stat(p9).st_mode))

# ══════════════════ C11/C12 并发与全局移除 ══════════════════
import threading

dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_concurrent")
alert_monitor.load_json = lambda *a, **k: {}
alert_monitor.save_json = lambda *a, **k: None
CC = ["s-cc-%02d" % i for i in range(10)]


def _worker(idx):
    try:
        alert_monitor.check_alerts(scan_id=CC[idx])
    except Exception as e:
        print("   (线程", idx, "异常:", type(e).__name__, e, ")")


ts = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
[t.start() for t in ts]
[t.join() for t in ts]

rows = []
for f in os.listdir(dlog.BASE_DIR):
    rows += [json.loads(x) for x in open(os.path.join(dlog.BASE_DIR, f), encoding="utf-8").read().splitlines() if x.strip()]
scans = [r for r in rows if r["event"] == "scan"]
ids_seen = sorted(r["scan_id"] for r in scans)
check("C11 10 个并发扫描：每个 ID 恰好 1 条 scan 事件", ids_seen == sorted(CC), "got=%d ids" % len(ids_seen))
alien = [r.get("scan_id") for r in rows if r.get("scan_id") and r["scan_id"] not in set(CC)]
check("C11b 无事件携带他人/未知 scan_id", not alien, "alien=%s" % alien[:3])
check("C12 模块级 _CURRENT_SCAN_ID 已移除（改为显式传参）",
      not hasattr(alert_monitor, "_CURRENT_SCAN_ID"))

# ══════════════════ D. order_result 用 mock 验证（不触达交易所）══════════════════
EXEC_CALLS = []


def _fake_execute_order(symbol, action, amount_usdt=10, coin_name="", percentage=0, request_id=None):
    EXEC_CALLS.append({"symbol": symbol, "action": action, "percentage": percentage,
                       "amount_usdt": amount_usdt})
    return {"success": True, "detail": "mock-filled", "fill_price": 100.0, "quantity": 1.0,
            "pnl_pct": 0, "pnl_usdt": 0}


src7.execute_order = _fake_execute_order
src7.ask_agent = lambda role, p, model=None, max_retries=2, coin_name=None: src7.RoleResult(True, (
    '{"action":"BUY","percentage":20,"reason":"stub-buy"}' if role == "trader" else "stub text"))
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_order")
sid_o = "s-order-verify-0001"
before = len(ORDER_CALLS)
src7.run_committee("BTC", "btcusdt", "p0d-order", scan_id=sid_o)

rows = []
for f in os.listdir(dlog.BASE_DIR):
    rows += [json.loads(x) for x in open(os.path.join(dlog.BASE_DIR, f), encoding="utf-8").read().splitlines() if x.strip()]
evs = [r for r in rows if r.get("scan_id") == sid_o]
kinds = sorted({r["event"] for r in evs})
ord_ev = [r for r in evs if r["event"] == "order_result"]
check("D1 committee_start / committee_decision / order_result 同一 scan_id",
      {"committee_start", "committee_decision", "order_result"} <= set(kinds), str(kinds))
check("D2 order_result 记录了成功/成交价/数量",
      bool(ord_ev) and ord_ev[0].get("success") is True and ord_ev[0].get("fill_price") == 100.0
      and ord_ev[0].get("quantity") == 1.0, str(ord_ev[0])[:140] if ord_ev else "none")
check("D3 mock execute_order 恰好被调用 1 次", len(EXEC_CALLS) == 1, "calls=%d" % len(EXEC_CALLS))
check("D3b 真实下单接口调用 0 次", len(ORDER_CALLS) == before, "real=%d" % (len(ORDER_CALLS) - before))

# ══════════════════ B7-B11 嵌套 JSON 递进脱敏 ══════════════════
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_nested")
NEST = {
    "HTTPAUTH_SHORT": "ab",                 # 2 字符短值：正则抓不到，只能靠键名识别
    "PROXYTOK_DEEP": "short1",
    "OBJVAL_TOKEN": "REAL_TOKEN_IN_OBJECT",
    "LISTTOK": "Bearer LISTTOKEN_9911",
}
dlog.event("n_auth_short", payload={"headers": {"AUTHORIZATION": NEST["HTTPAUTH_SHORT"]}})
dlog.event("n_proxy_deep", payload={"a": {"b": {"Proxy-Authorization": NEST["PROXYTOK_DEEP"]}}})
dlog.event("n_obj_value", payload={"headers": {"authorization": {"scheme": "Bearer",
                                                                 "token": NEST["OBJVAL_TOKEN"]}}})
dlog.event("n_list", payload={"hops": [{"Authorization": NEST["LISTTOK"]}, {"keep": "yes"}]})
dlog.event("n_mixed", note="Authorization: Bearer " + NEST["LISTTOK"], ordinary_field="keep-me")

blob2 = ""
for f in os.listdir(dlog.BASE_DIR):
    blob2 += open(os.path.join(dlog.BASE_DIR, f), encoding="utf-8").read()
leak2 = [k for k, v in NEST.items() if v in blob2]
check("B7 嵌套/短值/对象/列表 四类均未泄漏", not leak2, "leaked=" + str(leak2))
check("B8 键名命中即整值丢弃（对象值也被替换）", "REAL_TOKEN_IN_OBJECT" not in blob2 and "***REDACTED***" in blob2)
check("B9 非敏感同级字段保留", "keep-me" in blob2 and '"keep": "yes"' in blob2)
_rows2 = [json.loads(x) for x in blob2.strip().splitlines() if x.strip()]
check("B10 输出仍是合法 JSON 且可解析", len(_rows2) == 5, "rows=%d" % len(_rows2))
check("B11 大小写混合键（AUTHORIZATION）也被识别",
      any("AUTHORIZATION" in json.dumps(r, ensure_ascii=False) for r in _rows2)
      and all(NEST["HTTPAUTH_SHORT"] not in json.dumps(r, ensure_ascii=False) for r in _rows2))

# ══════════════════ C13-C18 轮转权限失败 / 回滚 / 回滚失败 ══════════════════
_real_replace, _real_chmod2 = os.replace, os.chmod


def _rot_setup(tag):
    d = os.path.join(TMP_HOME, "decisions_rot_" + tag)
    dlog.BASE_DIR = d
    dlog.MAX_BYTES = 60
    dlog.event("rot_a", pad="x" * 40)
    return d, dlog._current_path()


def _rot_state(path):
    active = os.path.exists(path)
    unsafe = os.path.exists(path + ".unsafe")
    src = path if active else (path + ".unsafe" if unsafe else None)
    mode = stat.S_IMODE(os.stat(src).st_mode) if src else None
    body = open(src, encoding="utf-8").read() if src else ""
    return active, mode, unsafe, body


# C13-C17：chmod 坏掉（无法纠正），回滚本身成功
d13, p13 = _rot_setup("chmodfail")


def _bad_replace(src, dst):
    _real_replace(src, dst)
    _real_chmod2(dst, 0o644)


def _noop_chmod(path, mode, *a, **k):
    return


os.replace, os.chmod = _bad_replace, _noop_chmod
try:
    r13 = dlog.event("rot_b", pad="x" * 40)
    err13 = dlog.LAST_ERROR
finally:
    os.replace, os.chmod = _real_replace, _real_chmod2

check("C13 轮转权限纠正失败 → event 返回 False", r13 is False, "LAST_ERROR=" + str(err13)[:60])
check("C14 不遗留不合规 .1", not os.path.exists(p13 + ".1"), "files=%s" % sorted(os.listdir(d13)))
active13, mode13, unsafe13, body13 = _rot_state(p13)
check("C15 活动文件不会静默停留在 644（600 或已隔离）",
      (active13 and mode13 == 0o600) or (not active13),
      "active=%s mode=%s unsafe=%s" % (active13, oct(mode13) if mode13 else None, unsafe13))
check("C16 数据未丢失（活动文件或隔离文件中仍含 rot_a）", "rot_a" in body13, repr(body13[:30]))

# C18：回滚自身失败 → 语义必须明确
d18, p18 = _rot_setup("rollbackfail")


def _replace_forward_only(src, dst):
    if os.path.abspath(dst) == os.path.abspath(p18):
        raise OSError("simulated rollback failure")
    _real_replace(src, dst)
    _real_chmod2(dst, 0o644)


os.replace, os.chmod = _replace_forward_only, _noop_chmod
try:
    r18 = dlog.event("rot_b", pad="x" * 40)
    err18 = dlog.LAST_ERROR
finally:
    os.replace, os.chmod = _real_replace, _real_chmod2

check("C18 回滚失败 → event 返回 False", r18 is False, "LAST_ERROR=" + str(err18)[:70])
check("C18b 失败原因明确标注'回滚失败'", "回滚失败" in str(err18), str(err18)[:70])
check("C18c 数据保留在 .1（不丢数据）", os.path.exists(p18 + ".1"))
active18, mode18, _u18, _b18 = _rot_state(p18)
check("C18d 活动路径不存在（未留下 644 活动文件）", active18 is False,
      "active=%s mode=%s" % (active18, oct(mode18) if mode18 else None))

# C19：chmod 失败 + 隔离 rename 也失败 → 无法保证活动文件权限，必须显式报错
d19, p19 = _rot_setup("quarfail")


def _replace_no_quarantine(src, dst):
    if os.path.abspath(dst) == os.path.abspath(p19 + ".unsafe"):
        raise OSError("simulated quarantine failure")
    _real_replace(src, dst)
    _real_chmod2(dst, 0o644)


os.replace, os.chmod = _replace_no_quarantine, _noop_chmod
try:
    r19 = dlog.event("rot_b", pad="x" * 40)
    err19 = dlog.LAST_ERROR
finally:
    os.replace, os.chmod = _real_replace, _real_chmod2

check("C19 chmod 与隔离同时失败 → event 返回 False", r19 is False, "last=" + str(err19)[:60])
check("C19b 错误显式报告'隔离失败'（不静默）", "隔离失败" in str(err19), str(err19)[:90])
_active19 = os.path.exists(p19)
_mode19 = stat.S_IMODE(os.stat(p19).st_mode) if _active19 else None
check("C19c 此场景下活动文件确实为 644，但已被显式上报（不再声称'绝不为 644'）",
      _active19 and _mode19 == 0o644, "active=%s mode=%s" % (_active19, oct(_mode19) if _mode19 else None))
dlog.MAX_BYTES = 20 * 1024 * 1024

# ══════════════════ D4-D6 下单异常 / 返回值语义 ══════════════════
def _run_buy(sid):
    dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_order_" + sid)
    src7.ask_agent = lambda role, p, model=None, max_retries=2, coin_name=None: src7.RoleResult(True, (
        '{"action":"BUY","percentage":20,"reason":"stub-buy"}' if role == "trader" else "stub text"))
    before = len(ORDER_CALLS)
    try:
        src7.run_committee("BTC", "btcusdt", "p0d", scan_id=sid)
        raised = None
    except Exception as e:
        raised = e
    rows = []
    for f in os.listdir(dlog.BASE_DIR):
        rows += [json.loads(x) for x in open(os.path.join(dlog.BASE_DIR, f), encoding="utf-8").read().splitlines() if x.strip()]
    evs = [r for r in rows if r.get("scan_id") == sid and r["event"] == "order_result"]
    return raised, evs, len(ORDER_CALLS) - before


# D4：execute_order 抛异常 → 记录 exception / 结果未知 / 不谎报成交
def _raise_exec(*a, **k):
    raise RuntimeError("mock-exchange-down")


src7.execute_order = _raise_exec
SENT.clear()
raised4, evs4, real4 = _run_buy("s-order-exc-0001")
check("D4 下单异常被记录为 exception(status)", bool(evs4) and evs4[0].get("status") == "exception",
      str(evs4[0])[:120] if evs4 else "no event")
check("D4b 明确标注 success=False / exchange_confirmed=False",
      bool(evs4) and evs4[0].get("success") is False and evs4[0].get("exchange_confirmed") is False)
check("D4c 记录异常文本且标注结果未知",
      bool(evs4) and "mock-exchange-down" in str(evs4[0].get("error", ""))
      and "未知" in str(evs4[0].get("detail", "")))
check("D4d 未真实下单", real4 == 0, "real=%d" % real4)
check("D4e 异常时未推送\"已成交\"文案",
      not any("已成交" in str(x) for x in SENT), "sent=%d" % len(SENT))

# D5：返回 success=True → 只记 returned_success，且 exchange_confirmed 仍为 False
src7.execute_order = _fake_execute_order
raised5, evs5, real5 = _run_buy("s-order-ok-0001")
check("D5 返回成功记为 returned_success", bool(evs5) and evs5[0].get("status") == "returned_success",
      str(evs5[0])[:120] if evs5 else "no event")
check("D5b 不把函数返回值当作交易所已确认成交（exchange_confirmed=False）",
      bool(evs5) and evs5[0].get("exchange_confirmed") is False)

# D6：返回 success=False → 记 returned_failure
src7.execute_order = lambda *a, **k: {"success": False, "detail": "mock-rejected"}
raised6, evs6, real6 = _run_buy("s-order-fail-0001")
check("D6 返回失败记为 returned_failure", bool(evs6) and evs6[0].get("status") == "returned_failure",
      str(evs6[0])[:120] if evs6 else "no event")

# ══════════════════ B12-B17 敏感键名变体 + 短凭据 ══════════════════
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_keyvar")
SHORT = {"K1": "x", "K2": "1", "K3": "ab"}
dlog.event("k_camel", payload={"headers": {"refreshToken": SHORT["K1"], "apiKey": SHORT["K2"],
                                           "clientSecret": SHORT["K3"]}})
dlog.event("k_sep", payload={"X_API_KEY": SHORT["K1"], "X-API-KEY": SHORT["K2"],
                             "x_auth_token": SHORT["K3"]})
dlog.event("k_space", payload={" Authorization ": SHORT["K1"], "  proxy-authorization": SHORT["K2"]})
dlog.event("k_list", payload={"hops": [{"AUTHORIZATION": SHORT["K3"]}, {"Authorization": SHORT["K2"]}]})
dlog.event("k_deep", payload={"a": {"b": {"c": {"d": {"IdToken": SHORT["K1"]}}}}})
dlog.event("k_keep", payload={"Authorization": SHORT["K1"], "note": "keep-this", "n": 7})

blob3 = ""
for f in os.listdir(dlog.BASE_DIR):
    blob3 += open(os.path.join(dlog.BASE_DIR, f), encoding="utf-8").read()
rows3 = [json.loads(x) for x in blob3.strip().splitlines() if x.strip()]
flat = json.dumps(rows3, ensure_ascii=False)
check("B12 camelCase 键（refreshToken/apiKey/clientSecret）全部命中",
      all(k in blob3 for k in ("refreshToken", "apiKey", "clientSecret"))
      and flat.count("***REDACTED***") >= 3)
check("B13 分隔符变体（X_API_KEY / X-API-KEY / x_auth_token）全部命中",
      flat.count("***REDACTED***") >= 6)
check("B14 键名带前后空格也能识别", " Authorization " in blob3 and '"  proxy-authorization"' in blob3)
check("B15 极短凭据（x / 1 / ab）未出现在文件里",
      all(('"%s"' % v) not in flat for v in SHORT.values()))
check("B16 深层嵌套（5 层）仍被脱敏", '"IdToken"' in blob3 and flat.count("***REDACTED***") >= 9)
check("B17 非敏感字段保留且输出仍是合法 JSON", "keep-this" in blob3 and len(rows3) == 6)

# B18-B22：api_secret / apiSecret / yunwu_api_key / bytes / 自定义对象
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_keys2")
K2 = {"S1": "FAKE_APISECRET_01", "S2": "FAKE_CAMEL_02", "S3": "FAKE_YUNWU_03",
      "S4": "FAKE_DEEPSEEK_04", "B1": "FAKE_BYTES_06", "B2": "FAKE_BYTES_07", "O1": "FAKE_OBJ_05"}


class _CredObj:
    def __str__(self):
        return "Authorization: Bearer " + K2["O1"]


dlog.event("x_api_secret", payload={"api_secret": K2["S1"]})
dlog.event("x_api_camel", payload={"apiSecret": K2["S2"]})
dlog.event("x_yunwu", payload={"yunwu_api_key": K2["S3"], "deepseek_api_key": K2["S4"],
                               "note": "keep-normal-field"})
dlog.event("x_bytes", payload={"blob": ("Bearer " + K2["B1"]).encode(),
                               "raw_secret": K2["B2"].encode()})
dlog.event("x_obj", payload={"thing": _CredObj(), "nested": {"apiSecret": _CredObj()}})
dlog.event("x_nonstrkey", payload={1: "v", None: "n"})

blob4 = b""
for f in os.listdir(dlog.BASE_DIR):
    blob4 += open(os.path.join(dlog.BASE_DIR, f), "rb").read()
txt4 = blob4.decode("utf-8")
rows4 = [json.loads(x) for x in txt4.strip().splitlines() if x.strip()]
leak4 = [k for k, v in K2.items() if v in txt4]
check("B18 api_secret / apiSecret 键全部命中", "FAKE_APISECRET_01" not in txt4 and "FAKE_CAMEL_02" not in txt4)
check("B19 yunwu_api_key / deepseek_api_key 键全部命中", "FAKE_YUNWU_03" not in txt4 and "FAKE_DEEPSEEK_04" not in txt4)
check("B20 bytes 值先 decode 再脱敏（Bearer 串与敏感键都清掉）",
      "FAKE_BYTES_06" not in txt4 and "FAKE_BYTES_07" not in txt4)
check("B21 自定义对象的 __str__ 内容也被脱敏", "FAKE_OBJ_05" not in txt4)
check("B22 六类样本 0 泄漏 + 输出仍是合法 JSON + 非敏感字段保留",
      not leak4 and len(rows4) == 6 and "keep-normal-field" in txt4, "leak=%s rows=%d" % (leak4, len(rows4)))
check("B22b 非字符串键被转成字符串（JSON 合法）", '"1"' in txt4 and '"None"' in txt4)

# ══════════════════ E1-E4 微信推送边界 ══════════════════
check("E1 send_simple_message 在测试进程内已被内存桩替换",
      wechat_push.send_simple_message is not _ORIG_SEND)
_before_e = len(SENT)
wechat_push.send_simple_message("boundary-probe")
check("E2 桩调用只进内存、不触网", len(SENT) == _before_e + 1)
# E3：绕过桩直接调真实实现 → 内部 try/except 吞掉，返回 False（不可能外发）
_direct = _ORIG_SEND("boundary-probe-direct")
check("E3 直接调用真实实现返回 False（requests.post 已注入为必抛，无法外发）",
      _direct is False, "ret=%r" % (_direct,))
msgs = [str(x[0]) if x else "" for x in SENT]
check("E4 委员会文案可枚举地落在桩上（成功/失败两种都出现过）",
      any("模拟成交！" in m for m in msgs) and any("未能模拟成交" in m for m in msgs),
      "msgs=%d" % len(msgs))

# ══════════════════ K1-K6 字典键 / 事件名 脱敏绕过 ══════════════════
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_keys3")
TOK = {"EV": "FAKE_EVENTTOK_11", "K1": "FAKEKEYAA1122334455", "K2": "FAKEKEYBB1122334455",
       "LONG": "L" * 40, "DEEP": "FAKE_DEEPKEY_12"}
dlog.event("Bearer " + TOK["EV"], note="x")
dlog.event("k_sk_key", payload={"sk-" + TOK["K1"]: "v1", "sk-" + TOK["K2"]: "v2"})
dlog.event("k_long_key", payload={TOK["LONG"]: "v3"})
dlog.event("k_deep_key", payload={"a": {TOK["DEEP"]: "v4"}})

rawK = b"".join(open(os.path.join(dlog.BASE_DIR, f), "rb").read() for f in os.listdir(dlog.BASE_DIR))
txtK = rawK.decode("utf-8")
rowsK = [json.loads(x) for x in txtK.strip().splitlines() if x.strip()]
check("K1 事件名里的 Bearer 令牌被抹", TOK["EV"] not in txtK,
      [r["event"] for r in rowsK][:2].__str__()[:60])
check("K2 字典键里的 sk- 令牌被抹", TOK["K1"] not in txtK and TOK["K2"] not in txtK)
check("K3 长串键名（>=32 位）被抹", TOK["LONG"] not in txtK)
check("K4 脱敏后键名冲突不丢数据（两个不同令牌键都保留）",
      '"v1"' in txtK and '"v2"' in txtK and txtK.count("sk-***REDACTED***") >= 2)
check("K5 嵌套字典的键同样被抹", TOK["DEEP"] not in txtK)
check("K6 原始 JSONL 泄漏扫描：全部假令牌 0 命中且每行合法 JSON",
      not any(v in txtK for v in TOK.values()) and len(rowsK) == 4)

# K7：反向保护 —— 正常字段名不能被误抹（避免过度脱敏破坏 schema）
dlog.event("k_fieldnames", payload={"authorization": "Bearer FTOK_01", "refreshToken": "RTOK_02",
                                    "percentage": 20, "scan_id": "s-1", "exchange_confirmed": False})
rawK2 = b"".join(open(os.path.join(dlog.BASE_DIR, f), "rb").read() for f in os.listdir(dlog.BASE_DIR))
txtK2 = rawK2.decode("utf-8")
rowK2 = [json.loads(x) for x in txtK2.strip().splitlines() if x.strip()][-1]["payload"]
check("K7 正常字段名保留、其值被脱敏（不过度脱敏）",
      set(rowK2.keys()) == {"authorization", "refreshToken", "percentage", "scan_id", "exchange_confirmed"}
      and rowK2["authorization"] == "***REDACTED***" and rowK2["refreshToken"] == "***REDACTED***"
      and rowK2["percentage"] == 20,
      str(sorted(rowK2.keys()))[:90])

# ══════════════════ L1-L3 LAST_ERROR 状态语义 ══════════════════
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_err")
okL1 = dlog.event("err_probe", n=1)
check("L1 成功时 LAST_ERROR 为空字符串（表示最近一次成功）",
      okL1 is True and dlog.LAST_ERROR == "", repr(dlog.LAST_ERROR))
dlog.BASE_DIR = "/proc/p0_no_write"
okL2 = dlog.event("err_probe", n=2)
check("L2 失败时 LAST_ERROR 记录原因", okL2 is False and dlog.LAST_ERROR.strip() != "",
      str(dlog.LAST_ERROR)[:70])
dlog.BASE_DIR = os.path.join(TMP_HOME, "decisions_err")
okL3 = dlog.event("err_probe", n=3)
check("L3 下一次成功会清空 LAST_ERROR（不是累积状态）",
      okL3 is True and dlog.LAST_ERROR == "", repr(dlog.LAST_ERROR))

print("=" * 78)
passed = sum(1 for _, o, _e in RESULTS if o)
print("修复测试结果: %d/%d 通过" % (passed, len(RESULTS)))
print("真实下单调用:", len(ORDER_CALLS), "| 被拦截的微信发送尝试:", len(SENT))
if passed != len(RESULTS):
    print("失败项:", [n for n, o, _e in RESULTS if not o])
print("=" * 78)
sys.exit(0 if passed == len(RESULTS) else 1)
