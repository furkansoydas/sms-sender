"""SQLite database for audit log, SMS history, and send queue."""
import os
import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "sms_sender.db")


def init_db():
    """Create tables if not exist."""
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                username TEXT,
                action TEXT NOT NULL,
                details TEXT,
                ip_address TEXT,
                status TEXT DEFAULT 'success'
            );

            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_username ON audit_log(username);

            CREATE TABLE IF NOT EXISTS sms_batch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_uid TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL,
                message TEXT NOT NULL,
                total_recipients INTEGER DEFAULT 0,
                successful INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                send_type TEXT DEFAULT 'bulk',
                completed_at TEXT,
                error_message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_batch_created ON sms_batch(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_batch_status ON sms_batch(status);

            CREATE TABLE IF NOT EXISTS sms_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                firma TEXT,
                sent_at TEXT,
                status TEXT DEFAULT 'pending',
                response TEXT,
                FOREIGN KEY (batch_id) REFERENCES sms_batch(id)
            );

            CREATE INDEX IF NOT EXISTS idx_sms_batch ON sms_log(batch_id);
            CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_log(phone);

            CREATE TABLE IF NOT EXISTS sms_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_uid TEXT NOT NULL,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL,
                message TEXT NOT NULL,
                phones_json TEXT NOT NULL,
                firmas_json TEXT,
                send_type TEXT DEFAULT 'bulk',
                status TEXT DEFAULT 'queued',
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT,
                error_message TEXT,
                scheduled_for TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_queue_status ON sms_queue(status);
            CREATE INDEX IF NOT EXISTS idx_queue_created ON sms_queue(created_at DESC);
        """)
        db.commit()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Audit Log ──

def log_action(username: str | None, action: str, details: str = "", ip: str = "", status: str = "success"):
    with get_db() as db:
        db.execute(
            "INSERT INTO audit_log (timestamp, username, action, details, ip_address, status) VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), username, action, details, ip, status),
        )
        db.commit()


def get_audit_log(limit: int = 200, username: str = None, action: str = None):
    with get_db() as db:
        q = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if username:
            q += " AND username = ?"
            params.append(username)
        if action:
            q += " AND action LIKE ?"
            params.append(f"%{action}%")
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in db.execute(q, params).fetchall()]


# ── SMS Queue ──

def enqueue_sms(batch_uid: str, username: str, message: str, phones: list, firmas: list = None, send_type: str = "bulk"):
    with get_db() as db:
        db.execute(
            """INSERT INTO sms_queue (batch_uid, created_at, username, message, phones_json, firmas_json, send_type, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
            (batch_uid, _now(), username, message,
             json.dumps(phones), json.dumps(firmas or []), send_type),
        )
        db.commit()


def get_queue(limit: int = 100, status: str = None):
    with get_db() as db:
        q = "SELECT * FROM sms_queue WHERE 1=1"
        params = []
        if status:
            q += " AND status = ?"
            params.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in db.execute(q, params).fetchall()]
        for r in rows:
            try:
                r["phones"] = json.loads(r["phones_json"])
                r["firmas"] = json.loads(r["firmas_json"] or "[]")
                r["phone_count"] = len(r["phones"])
            except Exception:
                r["phones"] = []
                r["firmas"] = []
                r["phone_count"] = 0
        return rows


def get_next_queued():
    """Fetch and lock next queued item by setting status=processing."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM sms_queue WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        db.execute("UPDATE sms_queue SET status = 'processing', attempts = attempts + 1, last_attempt_at = ? WHERE id = ?",
                   (_now(), row["id"]))
        db.commit()
        result = dict(row)
        try:
            result["phones"] = json.loads(result["phones_json"])
            result["firmas"] = json.loads(result["firmas_json"] or "[]")
        except Exception:
            result["phones"] = []
            result["firmas"] = []
        return result


def mark_queue_done(queue_id: int, status: str, error: str = ""):
    with get_db() as db:
        db.execute("UPDATE sms_queue SET status = ?, error_message = ? WHERE id = ?",
                   (status, error, queue_id))
        db.commit()


def retry_queue_item(queue_id: int):
    with get_db() as db:
        db.execute("UPDATE sms_queue SET status = 'queued', error_message = NULL WHERE id = ?", (queue_id,))
        db.commit()


# ── SMS Batch / History ──

def create_batch(batch_uid: str, username: str, message: str, recipients: int, send_type: str = "bulk") -> int:
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO sms_batch (batch_uid, created_at, username, message, total_recipients, send_type, status)
               VALUES (?, ?, ?, ?, ?, ?, 'sending')""",
            (batch_uid, _now(), username, message, recipients, send_type),
        )
        db.commit()
        return cur.lastrowid


def add_sms_log(batch_id: int, phone: str, firma: str, status: str, response: str = ""):
    with get_db() as db:
        db.execute(
            """INSERT INTO sms_log (batch_id, phone, firma, sent_at, status, response)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (batch_id, phone, firma or "", _now(), status, response),
        )
        db.commit()


def finalize_batch(batch_id: int, successful: int, failed: int, status: str = "completed", error: str = ""):
    with get_db() as db:
        db.execute(
            """UPDATE sms_batch SET successful = ?, failed = ?, status = ?, completed_at = ?, error_message = ?
               WHERE id = ?""",
            (successful, failed, status, _now(), error, batch_id),
        )
        db.commit()


def get_batches(limit: int = 100):
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM sms_batch ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]


def get_batch_detail(batch_uid: str):
    with get_db() as db:
        batch = db.execute("SELECT * FROM sms_batch WHERE batch_uid = ?", (batch_uid,)).fetchone()
        if not batch:
            return None, []
        logs = db.execute("SELECT * FROM sms_log WHERE batch_id = ? ORDER BY id ASC", (batch["id"],)).fetchall()
        return dict(batch), [dict(l) for l in logs]


def get_stats():
    with get_db() as db:
        total_batches = db.execute("SELECT COUNT(*) c FROM sms_batch").fetchone()["c"]
        total_sms = db.execute("SELECT COUNT(*) c FROM sms_log").fetchone()["c"]
        successful = db.execute("SELECT COUNT(*) c FROM sms_log WHERE status = 'success'").fetchone()["c"]
        failed = db.execute("SELECT COUNT(*) c FROM sms_log WHERE status = 'failed'").fetchone()["c"]
        queued = db.execute("SELECT COUNT(*) c FROM sms_queue WHERE status = 'queued'").fetchone()["c"]
        return {
            "total_batches": total_batches,
            "total_sms": total_sms,
            "successful": successful,
            "failed": failed,
            "queued": queued,
        }
