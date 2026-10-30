"""One shared committee lane, persistent cooldown and uncertain-order latch.

This module never creates or cancels an exchange order. The original executor
remains the only submission path; its intent is persisted before it is called.
"""
from contextlib import contextmanager
import json
import fcntl
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from clients.order_journal import OrderJournal, database
from clients.position_review import ReviewSchedule

_LOCK = threading.Lock()
ACCOUNT = 'committee-service'
NO_SUBMISSION = {'rejected_input', 'holdings_unavailable', 'skipped_no_holdings',
                 'no_usdt', 'below_min_cost', 'zero_amount'}


def readonly_reader():
    from clients.gateio_trade import _load_env
    from clients.gate_readonly import GateReadOnly
    env = _load_env()
    return GateReadOnly(env.get('GATEIO_API_KEY', ''), env.get('GATEIO_API_SECRET', ''))


class ExecutionGuard:
    def __init__(self, path):
        self.journal = OrderJournal(path)
        self.key = None

    def blocked(self):
        with database(self.journal.path) as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM intents WHERE state NOT IN ('filled','cancelled','rejected')")]
        if rows:
            reader = None
            for row in rows:
                if row['order_id']:
                    if reader is None:
                        reader = readonly_reader()
                    self.journal.reconcile(row['key'], reader)
        with database(self.journal.path) as db:
            return db.execute("SELECT COUNT(*) FROM intents WHERE state NOT IN ('filled','cancelled','rejected')").fetchone()[0] > 0

    def prepare(self, symbol, action, percentage, request_id):
        # Never replay an intent, including a previously filled/cancelled one.
        self.key = request_id
        with database(self.journal.path) as db:
            if db.execute('SELECT 1 FROM intents WHERE key=?', (request_id,)).fetchone():
                raise ValueError('duplicate_intent')
        self.journal.prepare(request_id, ACCOUNT, symbol, action.lower(), {'percentage': percentage})

    def observe(self, result):
        result = dict(result or {})
        if result.get('status') in NO_SUBMISSION and not result.get('order_id'):
            with database(self.journal.path) as db:
                db.execute("UPDATE intents SET state='rejected' WHERE key=? AND order_id IS NULL", (self.key,))
            return result
        from clients.decision_log import sanitize
        original = sanitize({"status": result.get("status"),
                             "detail": str(result.get("detail") or "")[:2000]})
        result['execution_diagnostic'] = original
        oid = result.get('order_id')
        try:
            if oid is not None:
                self.journal.attach(self.key, str(oid))
            row = self.journal.reconcile(self.key, readonly_reader()) if oid else self.journal.unknown(self.key)
        except Exception:
            row = self.journal.unknown(self.key)
        evidence = row.get('evidence') or {}
        if row['state'] in ('filled', 'cancelled'):
            result.update(exchange_confirmed=bool(evidence.get('exchange_confirmed')),
                          quantity=float(evidence.get('quantity', 0)),
                          fill_price=float(evidence.get('average', 0)),
                          success=bool(evidence.get('exchange_confirmed')), status=row['state'],
                          detail='已用交易所私有订单及成交明细核实终态：' + row['state'])
        else:
            result.update(exchange_confirmed=False, status='unconfirmed',
                          detail='订单结果尚未完成只读核实；已阻止后续委员会订单，禁止重发。'
                                 + ' 原始执行信息：' + original['detail'])
            with database(self.journal.path) as db:
                db.execute("UPDATE intents SET error=? WHERE key=?",
                           (json.dumps(original, ensure_ascii=False), self.key))
        return result


@contextmanager
def committee_lane(path):
    if not _LOCK.acquire(blocking=False):
        yield None
        return
    fd = None
    try:
        # OrderJournal validates ownership, regular files and 700/600 modes.
        guard = ExecutionGuard(path)
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('untrusted_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield guard
    finally:
        if fd is not None:
            os.close(fd)
        _LOCK.release()


def coordinated_run(coin, symbol, reason, scan_id, review_only, trigger, runner, event):
    now = time.time()
    state = Path(os.path.expanduser('~/.hermes/state'))
    with committee_lane(state / 'committee_orders.sqlite3') as guard:
        if guard is None:
            event('committee_skipped', coin=coin, reason='committee_busy', trigger=trigger)
            return False
        if guard.blocked():
            event('committee_skipped', coin=coin, reason='unresolved_order', trigger=trigger)
            return False
        # Any committee attempt postpones the next periodic review by four hours.
        # A fresh radar may re-evaluate after five minutes, avoiding an immediate
        # reverse trade without suppressing radar exits for a full four hours.
        schedule = ReviewSchedule(state / 'position_reviews.sqlite3',
                                  interval=14400 if trigger == 'position_review' else 300)
        if not schedule.claim(coin, now):
            event('committee_skipped', coin=coin, reason='shared_cooldown', trigger=trigger)
            return False
        scan_id = scan_id or ('review-' + uuid.uuid4().hex[:16])
        event('committee_dispatch', coin=coin, scan_id=scan_id, trigger=trigger,
              review_only=review_only)
        from clients import gateio_trade as trade
        # Paper executor already has transactional idempotency. Live intent
        # tracking is attached only when actual execution is requested.
        execution_guard = guard if not trade.DRY_RUN and not review_only else None
        try:
            return runner(coin, symbol, reason, scan_id, review_only=review_only,
                          execution_guard=execution_guard)
        finally:
            # A slow model call must not consume the post-decision cooldown.
            schedule.completed(coin, time.time())
