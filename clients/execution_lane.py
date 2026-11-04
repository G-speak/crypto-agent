"""Process/thread-shared spot executor lock and pre-submit crash latch.

Only this service's calls cooperate. Manual/other bots trading concurrently are
outside this boundary. Pending rows require explicit read-only reconciliation;
never delete this database merely because no fill was found.
"""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import sqlite3
import os
import stat
import threading
import time
import uuid
from clients.order_journal import database

_LOCK = threading.Lock()


class Lane:
    def __init__(self, path):
        self.path = str(path)
        self.key = None
        with database(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS submissions (key TEXT PRIMARY KEY, symbol TEXT NOT NULL, state TEXT NOT NULL, order_id TEXT, updated REAL NOT NULL)')

    def _follow_reconciled_committee(self):
        """Follow only terminal private-order evidence already verified by OrderJournal.

        This never treats missing trades or an operator's no-order annotation as
        evidence of terminality. No HTTP or order calls occur here.
        """
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        if not path.exists(): return
        with database(self.path) as db:
            pending=db.execute("SELECT key,order_id FROM submissions WHERE state='pending'").fetchall()
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as source:
            for item in pending:
                matches=source.execute("SELECT state,order_id,evidence FROM intents WHERE client_oid=?",(item['key'],)).fetchall()
                if len(matches)!=1: continue
                state,oid,raw=matches[0]
                if state not in ('filled','cancelled') or not oid or (item['order_id'] and item['order_id']!=str(oid)):
                    continue
                try:
                    evidence=json.loads(raw)
                    if evidence.get('state')!=state or not isinstance(evidence.get('trades'),list): continue
                    if state=='filled' and (evidence.get('exchange_confirmed') is not True or not evidence['trades']): continue
                    from clients.order_journal import number
                    number(evidence['quantity']);number(evidence['cost']);number(evidence['average'])
                except (TypeError, ValueError, KeyError):
                    continue
                with database(self.path) as db:
                    db.execute("UPDATE submissions SET state='terminal',order_id=?,updated=? WHERE key=? AND state='pending'",(str(oid),time.time(),item['key']))

    def blocked(self):
        self._follow_reconciled_committee()
        with database(self.path) as db:
            return bool(db.execute("SELECT 1 FROM submissions WHERE state='pending' LIMIT 1").fetchone())

    def existing_committee_pending(self, client_oid):
        import sqlite3
        path=Path(os.path.expanduser('~/.hermes/state/committee_orders.sqlite3'))
        if not path.exists(): return False
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
            rows=db.execute("SELECT client_oid,state FROM intents WHERE state NOT IN ('filled','cancelled','rejected')").fetchall()
        # Exempt only the currently prepared outer intent; never an unrelated latch.
        return any(not client_oid or row[0]!=client_oid or row[1]!='prepared' for row in rows)

    def prepare(self, symbol, key):
        self.key = key or 'local-'+uuid.uuid4().hex
        with database(self.path) as db:
            if db.execute('SELECT 1 FROM submissions WHERE key=?', (self.key,)).fetchone():
                return False
            db.execute('INSERT INTO submissions VALUES (?,?,?,NULL,?)', (self.key, symbol, 'pending', time.time()))
        return True

    def attach(self, order_id):
        with database(self.path) as db:
            db.execute('UPDATE submissions SET order_id=?,updated=? WHERE key=?', (str(order_id) if order_id else None,time.time(),self.key))

    def terminal(self):
        with database(self.path) as db:
            db.execute("UPDATE submissions SET state='terminal',updated=? WHERE key=?", (time.time(),self.key))


@contextmanager
def execution_lane(path=None):
    path = Path(path or os.path.expanduser('~/.hermes/state/executor_allocation.sqlite3'))
    if not _LOCK.acquire(blocking=False):
        yield None
        return
    fd = None
    try:
        lane = Lane(path)
        fd = os.open(str(path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600:
            raise PermissionError('untrusted_executor_lock')
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield lane
    finally:
        if fd is not None: os.close(fd)
        _LOCK.release()
