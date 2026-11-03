#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-11-03 补丁回归：灰尘阈值 / 失败原因区分 / 幂等 client_oid / 两个小 bug。
全部离线：不联网、不下单、不发消息。"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import alert_monitor as am  # noqa: E402
import seven_roles_committee as committee  # noqa: E402
from clients import committee_coordination as cc  # noqa: E402
from clients import gateio_trade as gt  # noqa: E402
from clients import position_review as pr  # noqa: E402
from clients.order_journal import OrderJournal  # noqa: E402

FAILS = []
TOTAL = [0]


def check(name, cond, extra=""):
    TOTAL[0] += 1
    print(("  PASS " if cond else "  FAIL ") + name + (("  | " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(name)


print("=" * 70)
print("1. 灰尘仓位：全链路视同空仓")
print("=" * 70)
FAKE_BAL = {"SOL": {"total": "0.007899", "free": "0.007899", "used": "0"},
            "USDT": {"total": "71.7", "free": "71.7", "used": "0"}}
with patch.object(gt, "DRY_RUN", False), patch.object(gt, "get_real_balance", return_value=FAKE_BAL):
    ctx = pr.position_context("SOL", 92.39)
check("灰尘被识别", ctx.get("is_dust") is True, ctx.get("is_dust"))
check("灰尘不打历史成本查询", ctx["cost_status"] == "dust", ctx["cost_status"])
txt = pr.position_text("SOL", ctx)
check("文案写明按空仓处理", "空仓" in txt and "灰尘" in txt)
check("文案允许 BUY", "可正常 BUY" in txt)
check("文案严禁 SELL", "严禁 SELL" in txt)
print("     ->", txt[:120])
try:
    pr.validate_account(ctx, "SELL")
    check("灰尘 SELL 被拦截", False, "没有抛异常")
except ValueError as e:
    check("灰尘 SELL 被拦截", str(e) == "dust_position_cannot_sell", str(e))
pr.validate_account(ctx, "BUY")   # 不应抛
check("灰尘 BUY 放行", True)

print()
print("=" * 70)
print("2. 非灰尘仓位不受影响")
print("=" * 70)
BIG = {"SOL": {"total": "0.1", "free": "0.1", "used": "0"}, "USDT": {"total": "50", "free": "50", "used": "0"}}
with patch.object(gt, "DRY_RUN", False), patch.object(gt, "get_real_balance", return_value=BIG), \
        patch.object(gt, "_fetch_current_price", return_value=100.0):
    ctx2 = pr.position_context("SOL", 92.39)
check("0.1 SOL 不算灰尘", ctx2.get("is_dust") is False)
check("有持仓文案", "有持仓" in pr.position_text("SOL", ctx2))

print()
print("=" * 70)
print("3. review_cycle 跳过灰尘")
print("=" * 70)
calls, events = [], []
with patch.object(gt, "_fetch_current_price", return_value=92.39):
    pr._DUST_LOGGED.clear()
    pr.review_cycle([("SOL", "solusdt")], {"SOL": {"quantity": "0.007899"}}, None, 1, 
                    lambda *a, **k: calls.append(a), lambda e, **k: events.append(e))
check("灰尘不触发复评", calls == [], calls)
check("只记一条 dust 事件", events == ["position_review_dust_skipped"], events)
with patch.object(gt, "_fetch_current_price", return_value=92.39):
    pr._DUST_LOGGED.clear()
    calls2, events2 = [], []
    pr.review_cycle([("SOL", "solusdt")], {"SOL": {"quantity": "0.1"}}, None, 1,
                    lambda *a, **k: calls2.append(a), lambda e, **k: events2.append(e))
check("0.1 SOL 正常复评", len(calls2) == 1, calls2)

print()
print("=" * 70)
print("4. 幂等键 client_oid")
print("=" * 70)
oid = gt.client_oid_for("committee:position-review-SOL-123:SOL:SELL")
check("以 t- 开头", oid.startswith("t-"), oid)
check("长度 26 ≤ 28", len(oid) == 26, len(oid))
check("仅字母数字横杠", all(c.isalnum() or c in "-_" for c in oid), oid)
check("确定性", oid == gt.client_oid_for("committee:position-review-SOL-123:SOL:SELL"))
check("空 request_id 返回 None", gt.client_oid_for(None) is None)


class FakeExchange:
    def __init__(self):
        self.calls = []

    def load_markets(self):
        pass

    @property
    def markets(self):
        return {"SOL/USDT": {"limits": {"cost": {"min": 3.0}, "amount": {"min": 0.001}},
                             "precision": {"amount": 0.001}}}

    def fetch_ticker(self, s):
        return {"last": 92.39}

    def fetch_balance(self):
        return {"free": {"USDT": 71.7, "SOL": 0.1}, "USDT": {"free": 71.7, "total": 71.7},
                "SOL": {"free": 0.1, "total": 0.1}}

    def amount_to_precision(self, s, a):
        return "%.3f" % a

    def create_market_order(self, **kw):
        self.calls.append(kw)
        return {"id": "123456789"}

    def fetch_order(self, oid, s):
        return {"status": "closed", "filled": 0.05, "cost": 4.6, "average": 92.39}


FE = FakeExchange()


class FakeCcxt:
    @staticmethod
    def gate(cfg):
        return FE


sys.modules.setdefault("ccxt", FakeCcxt)
with patch.object(gt, "_load_env", return_value={"GATEIO_API_KEY": "k", "GATEIO_API_SECRET": "s"}), \
        patch.dict(sys.modules, {"ccxt": FakeCcxt}):
    res = gt._live_trade("SOL/USDT", "SOL", "SELL", 5.0, percentage=50, current_qty=0.1,
                         client_oid="t-abc123")
hit = FE.calls[-1] if FE.calls else {}
check("下单带 text", hit.get("params", {}).get("text") == "t-abc123", hit.get("params"))
check("回传 client_oid", res.get("client_oid") == "t-abc123", res.get("client_oid"))

print()
print("=" * 70)
print("5. OrderJournal 存 client_oid 并按 text 反查")
print("=" * 70)
TMP = tempfile.mkdtemp(prefix="oj_")
JP = os.path.join(TMP, "orders.sqlite3")
j = OrderJournal(JP)
j.prepare("k1", "committee-service", "SOL/USDT", "sell", {"percentage": 60}, client_oid="t-abc123")
row = j.get("k1")
check("client_oid 已落库", row.get("client_oid") == "t-abc123", row.get("client_oid"))
check("初始 state=prepared", row["state"] == "prepared")


class ReaderFound:
    def order_by_text(self, text, symbol):
        return {"id": "1159937661445", "text": text}

    def order(self, oid, symbol):
        return {"id": oid, "currency_pair": "SOL_USDT", "side": "sell", "account": "spot",
                "status": "closed", "finish_as": "filled", "filled_amount": "0.03",
                "filled_total": "2.7717", "left": "0", "amount": "0.03",
                "avg_deal_price": "92.39", "fee": "0.0027", "fee_currency": "USDT"}

    def trades(self, oid, symbol):
        return [{"id": "99182791", "order_id": oid, "currency_pair": "SOL_USDT", "side": "sell",
                 "amount": "0.03", "price": "92.39", "fee": "0.0027", "fee_currency": "USDT"}]


j.reconcile("k1", ReaderFound())
row = j.get("k1")
check("按 text 反查到订单并 attach", row["order_id"] == "1159937661445", row["order_id"])
check("按 text 反查后完成核实 -> filled", row["state"] == "filled", row["state"])

j.prepare("k2", "committee-service", "BTC/USDT", "buy", {"percentage": 20}, client_oid="t-notfound01")
c = sqlite3.connect(JP)
c.execute("UPDATE intents SET updated=? WHERE key=?", (__import__("time").time() - 120, "k2"))
c.commit()
c.close()


class ReaderNotFound:
    def order_by_text(self, text, symbol):
        return None

    def order(self, oid, symbol):
        raise AssertionError("should not be called")

    def trades(self, oid, symbol):
        raise AssertionError("should not be called")


j.reconcile("k2", ReaderNotFound())
row = j.get("k2")
check("60 秒后查无此单 -> 终态", row["state"] == "cancelled", row["state"])
check("终态原因记录", row["error"] == "not_found_by_client_oid", row["error"])
check("未结数归零", j.unresolved("committee-service", "BTC/USDT") == 0)

try:
    j.prepare("k3", "committee-service", "ETH/USDT", "buy", {"percentage": 20}, client_oid="bad key!")
    check("非法 client_oid 被拒", False)
except ValueError as e:
    check("非法 client_oid 被拒", str(e) == "invalid client_oid", str(e))

print()
print("=" * 70)
print("6. 失败原因区分（run_committee outcome）")
print("=" * 70)
out = {}
with patch.dict(os.environ, {"POSITION_REVIEW_ENABLED": "0"}), \
        patch.object(committee, "_run_committee", side_effect=committee.CommitteeUnavailable("必需角色失败: risk_manager")):
    ok = committee.run_committee("BTC", "btcusdt", outcome=out)
check("AI 角色失败 -> ai_failure", ok is False and out.get("reason") == "ai_failure", out)
out2 = {}
with patch.dict(os.environ, {"POSITION_REVIEW_ENABLED": "0"}), \
        patch.object(committee, "_run_committee", side_effect=committee.CommitteeUnavailable("账户快照不可用")):
    ok = committee.run_committee("BTC", "btcusdt", outcome=out2)
check("账户快照失败 -> committee_blocked（不算 AI）", ok is False and out2.get("reason") == "committee_blocked", out2)
out3 = {}
with patch.dict(os.environ, {"POSITION_REVIEW_ENABLED": "0"}), \
        patch.object(committee, "_run_committee", return_value=True):
    ok = committee.run_committee("BTC", "btcusdt", outcome=out3)
check("成功 -> ok", ok is True and out3.get("reason") == "ok", out3)

print()
print("=" * 70)
print("7. 安全锁提醒去重")
print("=" * 70)
sent = []
with patch.object(am, "LOCK_NOTICE_FILE", os.path.join(TMP, "lock.json")), \
        patch("wechat_push.send_simple_message", side_effect=lambda m: sent.append(m) or True):
    am._notify_lock_once("存在未决订单")
    am._notify_lock_once("存在未决订单")
    check("同一把锁只提醒一次", len(sent) == 1, len(sent))
    check("文案写明安全锁", "安全锁" in sent[0])
    am._lock_notice_reset()
    am._notify_lock_once("存在未决订单")
    check("解锁后再锁可再提醒", len(sent) == 2, len(sent))

print()
print("=" * 70)
print("8. 两个小 bug")
print("=" * 70)
with patch.object(gt, "DRY_RUN", False), \
        patch.object(gt, "_generate_live_summary", return_value="LIVE-SUMMARY"), \
        patch.object(gt, "_bjt_now_str", return_value="now"):
    s = gt.generate_ledger_summary()
check("实盘每日盘点走 _generate_live_summary", s == "LIVE-SUMMARY", s)


class StopOnce:
    def __init__(self):
        self.n = 0

    def is_set(self):
        return self.n >= 1

    def wait(self, t):
        self.n += 1


evs = []
with patch.object(gt, "run_protection_cycle",
                  return_value={"blocked_new_risk": True, "exits": [], "reason": "live_execution_disabled"}), \
        patch.object(am, "_ev", side_effect=lambda e, **k: evs.append(e)):
    w = am.start_protection_worker(StopOnce())
    w.join(timeout=5)
check("实盘不再写 paper_protection", "paper_protection" not in evs, evs)

evs2 = []
with patch.object(gt, "run_protection_cycle",
                  return_value={"blocked_new_risk": True, "exits": [], "reason": "risk_latch"}), \
        patch.object(am, "_ev", side_effect=lambda e, **k: evs2.append(e)):
    w = am.start_protection_worker(StopOnce())
    w.join(timeout=5)
check("纸面/风险事件仍会记录", "paper_protection" in evs2, evs2)

print()
print("=" * 70)
print("结果: %d/%d 项通过, %d 项失败" % (TOTAL[0]-len(FAILS), TOTAL[0], len(FAILS)))
if FAILS:
    print("失败项:", FAILS)
print("=" * 70)
sys.exit(1 if FAILS else 0)
