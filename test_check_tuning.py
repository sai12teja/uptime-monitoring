"""TDD self-check for Stage 1 of check-tuning (retries, timeout, HTTP method/
body/encoding) per docs/superpowers/specs/2026-07-30-check-tuning-and-notifications-design.md.

Run: ./venv/Scripts/python.exe test_check_tuning.py
"""
import os
import sqlite3
import time as _time

import db

db.DB_PATH = "test_check_tuning.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

# --- migration adds the new columns without dropping existing data ---
check_conn = sqlite3.connect(db.DB_PATH)
cols = {row[1] for row in check_conn.execute("PRAGMA table_info(monitors)").fetchall()}
check_conn.close()
for col in ("retries", "timeout_sec", "http_method", "http_body", "http_body_encoding"):
    assert col in cols, f"{col} missing: {cols}"

# --- db.add_monitor / update_monitor thread the new fields through ---
mid = db.add_monitor("acme", "https://acme.example", "website",
                      retries=5, timeout_sec=30, http_method="POST",
                      http_body='{"k": "v"}', http_body_encoding="json")
row = db.get_monitor(mid)
assert row["retries"] == 5
assert row["timeout_sec"] == 30
assert row["http_method"] == "POST"
assert row["http_body"] == '{"k": "v"}'
assert row["http_body_encoding"] == "json"

# Defaults: unset fields default to NULL/None (retries/timeout) or Kuma-matching defaults (method/encoding).
mid2 = db.add_monitor("beta", "https://beta.example", "website")
row2 = db.get_monitor(mid2)
assert row2["retries"] is None
assert row2["timeout_sec"] is None
assert row2["http_method"] == "GET"
assert row2["http_body_encoding"] == "json"

db.update_monitor(mid2, "beta2", "https://beta2.example", None, 60,
                   retries=1, timeout_sec=15, http_method="PUT",
                   http_body="x=1", http_body_encoding="form")
row2 = db.get_monitor(mid2)
assert row2["retries"] == 1
assert row2["timeout_sec"] == 15
assert row2["http_method"] == "PUT"
assert row2["http_body"] == "x=1"
assert row2["http_body_encoding"] == "form"

os.remove(db.DB_PATH)
print("Stage 1 schema + db.py threading checks: OK")

# --- do_http_check: custom method/body/encoding, real local HTTP server ---
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from monitor_engine import do_http_check

received = {}


class _RecordingHandler(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        received["method"] = self.command
        received["body"] = body
        received["content_type"] = self.headers.get("Content-Type")
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self._record()

    def do_POST(self):
        self._record()

    def do_PUT(self):
        self._record()

    def log_message(self, *args):
        pass  # quiet test output


server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{port}/"

# Default: plain GET, no body (today's existing behavior, unchanged).
received.clear()
ok, _, _ = do_http_check({"url": base_url, "keyword": None, "http_method": "GET",
                           "http_body": None, "http_body_encoding": "json", "timeout_sec": None})
assert ok is True
assert received["method"] == "GET"
assert received["body"] == b""

# POST + JSON encoding.
received.clear()
ok, _, _ = do_http_check({"url": base_url, "keyword": None, "http_method": "POST",
                           "http_body": '{"key": "value"}', "http_body_encoding": "json", "timeout_sec": None})
assert ok is True
assert received["method"] == "POST"
assert json.loads(received["body"]) == {"key": "value"}
assert received["content_type"] == "application/json"

# PUT + form encoding (body stored as newline-separated key=value pairs).
received.clear()
ok, _, _ = do_http_check({"url": base_url, "keyword": None, "http_method": "PUT",
                           "http_body": "a=1\nb=2", "http_body_encoding": "form", "timeout_sec": None})
assert ok is True
assert received["method"] == "PUT"
assert received["body"] == b"a=1&b=2"
assert received["content_type"] == "application/x-www-form-urlencoded"

# POST + plain text encoding.
received.clear()
ok, _, _ = do_http_check({"url": base_url, "keyword": None, "http_method": "POST",
                           "http_body": "hello world", "http_body_encoding": "text", "timeout_sec": None})
assert ok is True
assert received["body"] == b"hello world"
assert received["content_type"] == "text/plain"

server.shutdown()
print("do_http_check custom method/body/encoding checks: OK")

# --- per-monitor retries -> fail_threshold (recovery stays at the fixed
# 2-consecutive-successes debounce, per the approved design) ---
from unittest.mock import patch

from monitor_engine import _check_one

db.init_db()

# retries=0 means Kuma's "down immediately on first fail" -> fail_threshold=1.
mid3 = db.add_monitor("strict", "https://strict.example", "website", retries=0)
with patch("monitor_engine.do_http_check", return_value=(False, 50, "HTTP 500")):
    event = _check_one(db.get_monitor(mid3), _time.time())
assert event is not None and event[0] == "opened", event
assert db.get_monitor(mid3)["status"] == "down"

# retries unset (None) -> today's global default, fail_threshold=3 (3rd fail opens it).
mid4 = db.add_monitor("default-retries", "https://default.example", "website")
with patch("monitor_engine.do_http_check", return_value=(False, 50, "HTTP 500")):
    event = None
    for _ in range(2):
        event = _check_one(db.get_monitor(mid4), _time.time())
assert event is None, "2 fails must not open yet at the default threshold"
with patch("monitor_engine.do_http_check", return_value=(False, 50, "HTTP 500")):
    event = _check_one(db.get_monitor(mid4), _time.time())
assert event is not None and event[0] == "opened", event

os.remove(db.DB_PATH)
print("Per-monitor retries -> fail_threshold checks: OK")

# --- per-monitor timeout_sec overrides the global CHECK_TIMEOUT_SEC ---
import monitor_engine as me


class _SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        import time as _t
        _t.sleep(0.3)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


server3 = HTTPServer(("127.0.0.1", 0), _SlowHandler)
port3 = server3.server_address[1]
threading.Thread(target=server3.serve_forever, daemon=True).start()

monitor = {"url": f"http://127.0.0.1:{port3}/", "keyword": None, "http_method": "GET",
           "http_body": None, "http_body_encoding": "json", "timeout_sec": 0.05}
ok, _, detail = me.do_http_check(monitor)
assert ok is False and "Timeout" in detail, detail

server3.shutdown()
print("Per-monitor timeout_sec checks: OK")

# --- REST API: accepts the new Stage 1 fields ---
import json as _json

from flask import Flask

import api

db.DB_PATH = "test_check_tuning_api.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

os.environ["API_KEY"] = "test-key-123"
flask_server = Flask(__name__)
api.register_api(flask_server)
client = flask_server.test_client()
client.environ_base["HTTP_X_API_KEY"] = "test-key-123"


def _get_json(resp):
    return _json.loads(resp.data)


r = client.post("/monitors", json={
    "name": "acme", "url": "https://acme.example", "type": "website",
    "retries": 4, "timeout_sec": 20, "http_method": "POST",
    "http_body": '{"a": 1}', "http_body_encoding": "json",
})
assert r.status_code == 201, r.status_code
created = _get_json(r)
assert created["retries"] == 4
assert created["timeout_sec"] == 20
assert created["http_method"] == "POST"
assert created["http_body"] == '{"a": 1}'
assert created["http_body_encoding"] == "json"

mid5 = created["id"]
r = client.put(f"/monitors/{mid5}", json={"retries": 0, "timeout_sec": 5})
assert r.status_code == 200
updated = _get_json(r)
assert updated["retries"] == 0
assert updated["timeout_sec"] == 5

os.remove(db.DB_PATH)
print("REST API Stage 1 field checks: OK")
