"""SQLite storage for monitors, checks, and incidents (PRD §12, trimmed to
the website/CRM scope — the server_metrics table lands with the
server-health task).

A fresh sqlite3 connection is opened per call instead of sharing one across
threads — simplest way to stay correct across the scheduler's background
thread and Dash's request thread without a shared lock.
"""
import contextlib
import secrets
import sqlite3
import time

DB_PATH = "rovix.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    type TEXT NOT NULL,
    keyword TEXT,
    interval_sec INTEGER NOT NULL DEFAULT 60,
    consecutive_fails INTEGER NOT NULL DEFAULT 0,
    consecutive_oks INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'awaiting',
    last_checked_at REAL,
    last_response_ms INTEGER,
    created_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    port INTEGER,
    push_token TEXT,
    retries INTEGER,
    timeout_sec INTEGER,
    http_method TEXT DEFAULT 'GET',
    http_body TEXT,
    http_body_encoding TEXT DEFAULT 'json',
    group_key TEXT,
    notify INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id),
    ts REAL NOT NULL,
    ok INTEGER NOT NULL,
    response_ms INTEGER,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER REFERENCES monitors(id),
    target_name TEXT NOT NULL,
    problem TEXT NOT NULL,
    problem_type TEXT NOT NULL DEFAULT 'http',
    severity TEXT NOT NULL DEFAULT 'critical',
    started REAL NOT NULL,
    resolved REAL,
    acknowledged_by TEXT
);
CREATE TABLE IF NOT EXISTS server_metric_state (
    metric TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'ok',
    pending_zone TEXT,
    pending_count INTEGER NOT NULL DEFAULT 0,
    incident_id INTEGER REFERENCES incidents(id),
    last_read_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    ts REAL NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    email_1 TEXT,
    email_2 TEXT,
    email_3 TEXT,
    notify_enabled INTEGER NOT NULL DEFAULT 1
);
"""


def format_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


@contextlib.contextmanager
def _conn():
    # `with sqlite3.connect(...)` commits/rolls back but does NOT close, so
    # this wraps it to also release the file handle. Relying on refcounting
    # to close was correctness-by-accident, and on Windows a leaked handle
    # makes the file undeletable. Call sites are unchanged.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _add_column_if_missing(conn, table, column, coltype):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS is a no-op once `monitors` already
        # exists, so new columns for a monitor type added after go-live
        # (PRD-additional-monitor-types) need an explicit, idempotent
        # migration rather than relying on the CREATE statement above.
        _add_column_if_missing(conn, "monitors", "port", "INTEGER")
        _add_column_if_missing(conn, "monitors", "push_token", "TEXT")
        _add_column_if_missing(conn, "monitors", "retries", "INTEGER")
        _add_column_if_missing(conn, "monitors", "timeout_sec", "INTEGER")
        _add_column_if_missing(conn, "monitors", "http_method", "TEXT DEFAULT 'GET'")
        _add_column_if_missing(conn, "monitors", "http_body", "TEXT")
        _add_column_if_missing(conn, "monitors", "http_body_encoding", "TEXT DEFAULT 'json'")
        _add_column_if_missing(conn, "monitors", "group_key", "TEXT")
        _add_column_if_missing(conn, "monitors", "notify", "INTEGER NOT NULL DEFAULT 1")


def list_monitors():
    with _conn() as conn:
        return conn.execute("SELECT * FROM monitors WHERE active = 1 ORDER BY id").fetchall()


def get_monitor(monitor_id):
    # Unfiltered by `active` on purpose — history/detail lookups (§13.6,
    # incident timelines) must still resolve a soft-deleted monitor's name.
    with _conn() as conn:
        return conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()


def get_due_monitors(now):
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM monitors WHERE active = 1 AND (last_checked_at IS NULL "
            "OR (? - last_checked_at) >= interval_sec)",
            (now,),
        ).fetchall()


def update_monitor(monitor_id, name, url, keyword, interval_sec, port=None,
                    retries=None, timeout_sec=None, http_method="GET",
                    http_body=None, http_body_encoding="json", notify=True):
    with _conn() as conn:
        conn.execute(
            "UPDATE monitors SET name = ?, url = ?, keyword = ?, interval_sec = ?, port = ?, "
            "retries = ?, timeout_sec = ?, http_method = ?, http_body = ?, http_body_encoding = ?, "
            "notify = ? WHERE id = ?",
            (name, url, keyword, interval_sec, port,
             retries, timeout_sec, http_method, http_body, http_body_encoding, int(notify), monitor_id),
        )


def deactivate_monitor(monitor_id):
    with _conn() as conn:
        conn.execute("UPDATE monitors SET active = 0 WHERE id = ?", (monitor_id,))


def add_monitor(name, url, target_type, keyword=None, interval_sec=60, port=None,
                 retries=None, timeout_sec=None, http_method="GET",
                 http_body=None, http_body_encoding="json", group_key=None, notify=True):
    # Push monitors are pinged via a secret-token URL rather than actively
    # checked -- the token is always server-generated, never client-supplied.
    push_token = secrets.token_hex(16) if target_type == "push" else None
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO monitors (name, url, type, keyword, interval_sec, created_at, port, push_token, "
            "retries, timeout_sec, http_method, http_body, http_body_encoding, group_key, notify) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, url, target_type, keyword, interval_sec, time.time(), port, push_token,
             retries, timeout_sec, http_method, http_body, http_body_encoding, group_key, int(notify)),
        )
        return cur.lastrowid


def get_monitor_by_push_token(push_token):
    with _conn() as conn:
        return conn.execute("SELECT * FROM monitors WHERE push_token = ?", (push_token,)).fetchone()


def record_check(monitor_id, ts, ok, response_ms, detail):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO checks (monitor_id, ts, ok, response_ms, detail) VALUES (?, ?, ?, ?, ?)",
            (monitor_id, ts, int(ok), response_ms, detail),
        )


def list_checks(monitor_id, limit=10):
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM checks WHERE monitor_id = ? ORDER BY ts DESC LIMIT ?",
            (monitor_id, limit),
        ).fetchall()


def update_monitor_state(monitor_id, status, fails, oks, checked_at, response_ms):
    with _conn() as conn:
        conn.execute(
            "UPDATE monitors SET status = ?, consecutive_fails = ?, consecutive_oks = ?, "
            "last_checked_at = ?, last_response_ms = ? WHERE id = ?",
            (status, fails, oks, checked_at, response_ms, monitor_id),
        )


def _log_event(conn, incident_id, ts, event_type, detail=None):
    conn.execute(
        "INSERT INTO incident_events (incident_id, ts, event_type, detail) VALUES (?, ?, ?, ?)",
        (incident_id, ts, event_type, detail),
    )


def open_incident(monitor_id, target_name, problem, severity="critical", problem_type="http"):
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (monitor_id, target_name, problem, severity, problem_type, started) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (monitor_id, target_name, problem, severity, problem_type, time.time()),
        )
        return cur.lastrowid


def resolve_incident(monitor_id, problem_type="http"):
    """Resolves the open incident of this specific problem_type for this
    monitor, if any — scoped by type (PRD §15: "deduplicated by (target,
    problem type)") so resolving an HTTP-down incident can never also
    resolve an unrelated, still-open SSL incident on the same monitor.

    Returns downtime in seconds (for the §14 recovery email), or None if
    there was no matching open incident.
    """
    now = time.time()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, started FROM incidents WHERE monitor_id = ? AND problem_type = ? AND resolved IS NULL",
            (monitor_id, problem_type),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE incidents SET resolved = ? WHERE monitor_id = ? AND problem_type = ? AND resolved IS NULL",
            (now, monitor_id, problem_type),
        )
        _log_event(conn, row["id"], now, "resolved")
        return now - row["started"]


def list_incidents(status=None, monitor_id=None, since=None, until=None):
    clauses, params = [], []
    if status == "open":
        clauses.append("resolved IS NULL")
    elif status == "resolved":
        clauses.append("resolved IS NOT NULL")
    if monitor_id is not None:
        clauses.append("monitor_id = ?")
        params.append(monitor_id)
    if since is not None:
        clauses.append("started >= ?")
        params.append(since)
    if until is not None:
        clauses.append("started <= ?")
        params.append(until)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn() as conn:
        return conn.execute(f"SELECT * FROM incidents{where} ORDER BY started DESC", params).fetchall()


def get_incident(incident_id):
    with _conn() as conn:
        return conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()


def get_metric_state(metric):
    with _conn() as conn:
        return conn.execute("SELECT * FROM server_metric_state WHERE metric = ?", (metric,)).fetchone()


def set_metric_state(metric, status, pending_zone, pending_count, incident_id, last_read_ts):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO server_metric_state (metric, status, pending_zone, pending_count, incident_id, last_read_ts) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(metric) DO UPDATE SET status=excluded.status, pending_zone=excluded.pending_zone, "
            "pending_count=excluded.pending_count, incident_id=excluded.incident_id, last_read_ts=excluded.last_read_ts",
            (metric, status, pending_zone, pending_count, incident_id, last_read_ts),
        )


def open_server_incident(target_name, problem, severity):
    """Server-metric incidents have no monitor row (CPU load isn't a URL you
    check) — monitor_id is NULL, target_name identifies what broke."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (monitor_id, target_name, problem, severity, started) "
            "VALUES (NULL, ?, ?, ?, ?)",
            (target_name, problem, severity, time.time()),
        )
        return cur.lastrowid


def update_incident_severity(incident_id, severity):
    with _conn() as conn:
        conn.execute("UPDATE incidents SET severity = ? WHERE id = ?", (severity, incident_id))


def resolve_server_incident(incident_id):
    now = time.time()
    with _conn() as conn:
        row = conn.execute("SELECT started FROM incidents WHERE id = ? AND resolved IS NULL",
                            (incident_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE incidents SET resolved = ? WHERE id = ?", (now, incident_id))
        _log_event(conn, incident_id, now, "resolved")
        return now - row["started"]


def acknowledge_incident(incident_id, who):
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE incidents SET acknowledged_by = ? WHERE id = ? AND resolved IS NULL",
            (who, incident_id),
        )
        if cur.rowcount:
            _log_event(conn, incident_id, time.time(), "acknowledged", who)


def add_user(username, password_hash):
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, time.time()),
        )
        return cur.lastrowid


def get_user_by_username(username):
    with _conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users():
    with _conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY id").fetchall()


def record_incident_event(incident_id, ts, event_type, detail=None):
    with _conn() as conn:
        _log_event(conn, incident_id, ts, event_type, detail)


def list_incident_events(incident_id):
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY ts", (incident_id,)
        ).fetchall()


def delete_old_checks(cutoff_ts):
    """PRD §16 retention -- incidents/incident_events are untouched (kept
    indefinitely, per the §16 auditability requirement); only the
    high-volume checks table gets pruned."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff_ts,))
        return cur.rowcount


def get_settings():
    # No row yet (Settings UI never saved) -> None, not an auto-inserted
    # default -- a plain SELECT must never write, or every read (including
    # from tests pointed at a throwaway db) would mutate rovix.db.
    with _conn() as conn:
        return conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()


def update_settings(email_1, email_2, email_3, notify_enabled):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO settings (id, email_1, email_2, email_3, notify_enabled) VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET email_1 = excluded.email_1, email_2 = excluded.email_2, "
            "email_3 = excluded.email_3, notify_enabled = excluded.notify_enabled",
            (email_1, email_2, email_3, int(notify_enabled)),
        )


def get_open_incident(monitor_id, problem_type="http"):
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM incidents WHERE monitor_id = ? AND problem_type = ? AND resolved IS NULL",
            (monitor_id, problem_type),
        ).fetchone()


init_db()
