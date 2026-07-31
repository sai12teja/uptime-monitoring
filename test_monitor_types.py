"""TDD self-check for additional monitor types (DNS, TCP port) per
docs/superpowers/specs/2026-07-30-additional-monitor-types-design.md.

Run: ./venv/Scripts/python.exe test_monitor_types.py
"""
import os
import sqlite3
import time

import db

db.DB_PATH = "test_monitor_types.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)

# --- migration: init_db() must add port/push_token to a pre-existing db that
# predates this gap (simulates the real rovix.db, which already has data and
# was never recreated from scratch for this change) ---
OLD_SCHEMA = """
CREATE TABLE monitors (
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
    active INTEGER NOT NULL DEFAULT 1
);
"""
conn = sqlite3.connect(db.DB_PATH)
conn.executescript(OLD_SCHEMA)
conn.execute("INSERT INTO monitors (name, url, type, created_at) VALUES ('pre-existing', 'https://x', 'website', 0)")
conn.commit()
conn.close()

db.init_db()  # must not drop/recreate the table, must not lose the existing row, must add the new columns

check_conn = sqlite3.connect(db.DB_PATH)
cols = {row[1] for row in check_conn.execute("PRAGMA table_info(monitors)").fetchall()}
check_conn.close()
assert "port" in cols, cols
assert "push_token" in cols, cols
assert [m["name"] for m in db.list_monitors()] == ["pre-existing"], "migration must preserve existing rows"

db.init_db()  # calling it again (e.g. next process start) must not error
db.init_db()

os.remove(db.DB_PATH)
print("db.py monitors-table migration (port/push_token) checks: OK")

# --- db.add_monitor / update_monitor: port threads through for tcp monitors ---
db.init_db()

tcp_id = db.add_monitor("db-server", "10.0.0.5", "tcp", port=5432)
row = db.get_monitor(tcp_id)
assert row["port"] == 5432, row["port"]
assert row["type"] == "tcp"

# port defaults to None for non-tcp types (unchanged call signature still works)
web_id = db.add_monitor("acme", "https://acme.example", "website")
assert db.get_monitor(web_id)["port"] is None

db.update_monitor(tcp_id, "db-server-2", "10.0.0.6", None, 60, port=5433)
assert db.get_monitor(tcp_id)["port"] == 5433

os.remove(db.DB_PATH)
print("db.py port threading checks: OK")

# --- do_tcp_check: real sockets, not mocked (matches do_http_check's own
# test style -- this project tests real I/O where it's cheap to do so) ---
import socket

from monitor_engine import do_tcp_check

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(("127.0.0.1", 0))
server.listen(1)
open_port = server.getsockname()[1]

ok, response_ms, detail = do_tcp_check({"url": "127.0.0.1", "port": open_port, "timeout_sec": None})
assert ok is True, detail
assert isinstance(response_ms, int)

server.close()

# Nothing listens on this port now that the socket above is closed.
ok, response_ms, detail = do_tcp_check({"url": "127.0.0.1", "port": open_port, "timeout_sec": None})
assert ok is False, detail
assert "refused" in detail.lower() or "unreachable" in detail.lower(), detail

print("do_tcp_check checks: OK")

# --- do_dns_check: real resolution, not mocked -- "localhost" always
# resolves to 127.0.0.1 with no network dependency, matching this project's
# preference for real I/O over mocks where it's cheap and reliable to do so ---
from monitor_engine import do_dns_check

ok, response_ms, detail = do_dns_check({"url": "localhost", "keyword": None})
assert ok is True, detail
assert isinstance(response_ms, int)

# Optional "expected value" match (reuses the keyword column, per the design doc).
ok, response_ms, detail = do_dns_check({"url": "localhost", "keyword": "127.0.0.1"})
assert ok is True, detail

ok, response_ms, detail = do_dns_check({"url": "localhost", "keyword": "9.9.9.9"})
assert ok is False, detail

ok, response_ms, detail = do_dns_check({"url": "this-domain-does-not-exist.rovix-test.invalid", "keyword": None})
assert ok is False, detail

print("do_dns_check checks: OK")

# --- _check_one dispatches by monitor type -- tcp/dns monitors flow through
# the SAME state machine/incident/email pipeline as http, just via a
# different check function ---
from monitor_engine import _check_one

db.init_db()

server2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server2.bind(("127.0.0.1", 0))
server2.listen(1)
tcp_port = server2.getsockname()[1]

tcp_mid = db.add_monitor("local-tcp", "127.0.0.1", "tcp", port=tcp_port)
event = _check_one(db.get_monitor(tcp_mid), time.time())
assert event is None  # single success, no state transition to report
assert db.get_monitor(tcp_mid)["status"] == "up"
server2.close()

dns_mid = db.add_monitor("local-dns", "localhost", "dns")
event = _check_one(db.get_monitor(dns_mid), time.time())
assert event is None
assert db.get_monitor(dns_mid)["status"] == "up"

# A tcp monitor pointed at a closed port goes down after 3 fails, exactly
# like an http monitor -- same shared state machine, different check fn.
dead_mid = db.add_monitor("dead-tcp", "127.0.0.1", "tcp", port=tcp_port)  # tcp_port's listener is closed now
event = None
for _ in range(3):
    event = _check_one(db.get_monitor(dead_mid), time.time())
assert event is not None and event[0] == "opened", event
assert db.get_monitor(dead_mid)["status"] == "down"

os.remove(db.DB_PATH)
print("_check_one type-dispatch checks: OK")
