"""Specification
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager

class Conflict(ValueError):
    pass

class Delivery:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS notifications(
                id TEXT PRIMARY KEY, business_key TEXT UNIQUE NOT NULL,
                body_hash TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
                expires INTEGER NOT NULL, confirmed INTEGER, late INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS attempts(
                id TEXT PRIMARY KEY, notification_id TEXT NOT NULL REFERENCES notifications(id),
                ordinal INTEGER NOT NULL, channel TEXT NOT NULL, state TEXT NOT NULL,
                active INTEGER NOT NULL, request_key TEXT UNIQUE NOT NULL,
                UNIQUE(notification_id,ordinal));
            CREATE UNIQUE INDEX IF NOT EXISTS one_active
                ON attempts(notification_id) WHERE active=1;
            CREATE TABLE IF NOT EXISTS receipts(
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, outcome TEXT NOT NULL);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def accept(self, producer, event, recipient, purpose, body, expires=86400):
        key = json.dumps([producer, event, recipient, purpose])
        h = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.transaction() as db:
            old = db.execute('SELECT * FROM notifications WHERE business_key=?', (key,)).fetchone()
            if old:
                if old['body_hash'] != h:
                    raise Conflict('same business key with different payload')
                return old['id']
            nid = str(uuid.uuid4())
            db.execute('INSERT INTO notifications(id,business_key,body_hash,state,version,expires) VALUES(?,?,?,?,0,?)',
                       (nid, key, h, 'ACCEPTED', expires))
            return nid

    def state(self, nid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM notifications WHERE id=?', (nid,)).fetchone()
            if not row:
                raise KeyError(nid)
            return dict(row)

    def attempts(self, nid):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM attempts WHERE notification_id=? ORDER BY ordinal', (nid,))]

    def advance(self, nid, expected_version, now, route=('push', 'sms', 'email')):
        """Atomic timer/explicit-failure transition. Caller supplies a due timer.

        A stale timer does nothing. Old SENDING/ACCEPTED becomes UNKNOWN, never
        presumed not sent. This compact model stops after the fast route; RFC
        deferred retries, policy service and durable outbox are out of scope.
        """
        with self.transaction() as db:
            n = db.execute('SELECT * FROM notifications WHERE id=?', (nid,)).fetchone()
            if not n:
                raise KeyError(nid)
            if n['state'] == 'CONFIRMED' or n['version'] != expected_version:
                return None
            active = db.execute('SELECT * FROM attempts WHERE notification_id=? AND active=1', (nid,)).fetchone()
            if active:
                closed = 'CANCELLED' if active['state'] == 'RESERVED' else 'UNKNOWN'
                db.execute('UPDATE attempts SET state=?, active=0 WHERE id=?', (closed, active['id']))
            if now >= n['expires']:
                db.execute("UPDATE notifications SET state='EXPIRED_UNCONFIRMED',version=version+1 WHERE id=?", (nid,))
                return None
            count = db.execute('SELECT count(*) FROM attempts WHERE notification_id=?', (nid,)).fetchone()[0]
            if count >= len(route):
                db.execute("UPDATE notifications SET state='WAITING_RETRY',version=version+1 WHERE id=?", (nid,))
                return None
            aid = str(uuid.uuid4())
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',
                       (aid, nid, count+1, route[count], 'RESERVED', 1, aid))
            db.execute("UPDATE notifications SET state='IN_PROGRESS',version=version+1 WHERE id=?", (nid,))
            return aid

    def claim(self, aid, now):
        with self.transaction() as db:
            a = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
            if not a:
                return False
            n = db.execute('SELECT * FROM notifications WHERE id=?', (a['notification_id'],)).fetchone()
            if n['state']=='CONFIRMED' or now >= n['expires'] or not a['active'] or a['state']!='RESERVED':
                return False
            db.execute("UPDATE attempts SET state='SENDING' WHERE id=?", (aid,))
            return True

    def transport_result(self, aid, result):
        if result not in ('ACCEPTED', 'UNKNOWN', 'FAILED'):
            raise ValueError(result)
        with self.transaction() as db:
            # Callback may have completed the attempt while HTTP was in flight.
            db.execute("UPDATE attempts SET state=?,active=CASE WHEN ?='FAILED' THEN 0 ELSE active END WHERE id=? AND active=1 AND state='SENDING'", (result, result, aid))

    def receipt(self, rid, aid, outcome, now, authenticated=True):
        if not authenticated or outcome not in ('confirmed', 'failed'):
            raise ValueError('untrusted or invalid receipt')
        with self.transaction() as db:
            a = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
            if not a:
                raise ValueError('unknown attempt')
            old = db.execute('SELECT * FROM receipts WHERE id=?', (rid,)).fetchone()
            if old:
                if (old['attempt_id'],old['outcome']) != (aid,outcome):
                    raise Conflict('receipt id reused')
                return False
            db.execute('INSERT INTO receipts VALUES(?,?,?)', (rid, aid, outcome))
            nid = a['notification_id']
            if outcome == 'confirmed':
                # Only pending RESERVED steps can actually be cancelled.
                db.execute("UPDATE attempts SET state=CASE WHEN state='RESERVED' THEN 'CANCELLED' ELSE state END, active=0 WHERE notification_id=?", (nid,))
                db.execute("UPDATE attempts SET state='CONFIRMED' WHERE id=?", (aid,))
                db.execute("UPDATE notifications SET state='CONFIRMED',confirmed=?,late=(? >= expires),version=version+1 WHERE id=? AND state!='CONFIRMED'", (now, now, nid))
            elif a['state'] != 'CONFIRMED':
                db.execute("UPDATE attempts SET state='FAILED',active=0 WHERE id=? AND state!='CONFIRMED'", (aid,))
            return True
