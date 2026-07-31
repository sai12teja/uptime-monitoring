"""TDD self-check for push/passive monitors, per
docs/superpowers/specs/2026-07-30-additional-monitor-types-design.md.

Run: ./venv/Scripts/python.exe test_push_monitors.py
"""
import os

from monitor_engine import evaluate_status

# --- evaluate_status: default thresholds unchanged (3 fails / 2 successes) ---
assert evaluate_status("up", 2, 0, False) == ("down", 3, 0, True, False)
assert evaluate_status("down", 0, 1, True) == ("up", 0, 2, False, True)

# --- evaluate_status: fail_threshold=1 / ok_threshold=1 (push monitors) ---
assert evaluate_status("up", 0, 0, False, fail_threshold=1, ok_threshold=1) == ("down", 1, 0, True, False)
assert evaluate_status("down", 0, 0, True, fail_threshold=1, ok_threshold=1) == ("up", 0, 1, False, True)
assert evaluate_status("awaiting", 0, 0, False, fail_threshold=1, ok_threshold=1) == ("down", 1, 0, True, False)

print("evaluate_status threshold generalization checks: OK")

# --- db.add_monitor: server-generates a push_token for type=push, never for others ---
import db

db.DB_PATH = "test_push_monitors.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

push_id = db.add_monitor("nightly-backup", "", "push", interval_sec=3600)
row = db.get_monitor(push_id)
assert row["push_token"], "push monitor must get a server-generated token"
assert len(row["push_token"]) >= 32, "token should be a real random secret, not a short/guessable value"

web_id = db.add_monitor("acme", "https://acme.example", "website")
assert db.get_monitor(web_id)["push_token"] is None

# Two push monitors never collide on the same token.
push_id2 = db.add_monitor("weekly-sync", "", "push", interval_sec=604800)
assert db.get_monitor(push_id2)["push_token"] != row["push_token"]

# --- db.get_monitor_by_push_token: the lookup the ping endpoint needs ---
found = db.get_monitor_by_push_token(row["push_token"])
assert found["id"] == push_id
assert db.get_monitor_by_push_token("not-a-real-token") is None

os.remove(db.DB_PATH)
print("db.py push_token generation checks: OK")

# --- _check_one's push branch: missed check-in = down after 1 miss ---
import time as _time

from monitor_engine import _check_one

db.init_db()

# Brand-new push monitor, never pinged: must NOT be treated as a failure on
# the very next tick (there's been no chance to check in yet).
push_mid = db.add_monitor("nightly-backup", "", "push", interval_sec=5)
event = _check_one(db.get_monitor(push_mid), _time.time())
assert event is None
assert db.get_monitor(push_mid)["status"] == "awaiting"

# Simulate a real first ping having arrived a while ago, then let the
# interval lapse without another ping -- ONE missed check-in is enough
# to go down (fail_threshold=1, per the approved design).
now = _time.time()
db.update_monitor_state(push_mid, "up", 0, 1, now - 10, None)  # last pinged 10s ago, interval=5s
event = _check_one(db.get_monitor(push_mid), now)
assert event is not None and event[0] == "opened", event
assert db.get_monitor(push_mid)["status"] == "down"

os.remove(db.DB_PATH)
print("_check_one push-branch (missed check-in) checks: OK")

# --- record_push_ping: a real check-in arriving, first-ever and recovery ---
from monitor_engine import record_push_ping

db.init_db()

push_mid2 = db.add_monitor("weekly-sync", "", "push", interval_sec=5)
monitor = db.get_monitor(push_mid2)

# First-ever ping: awaiting -> up, no incident (nothing was ever down).
event = record_push_ping(monitor, _time.time())
assert event is None
assert db.get_monitor(push_mid2)["status"] == "up"

# Simulate it having gone down (a real missed check-in), then a fresh ping
# arrives -- ONE successful ping recovers it immediately (ok_threshold=1).
db.update_monitor_state(push_mid2, "down", 1, 0, _time.time() - 20, None)
db.open_incident(push_mid2, "weekly-sync", "No check-in received within 5s", severity="critical")
event = record_push_ping(db.get_monitor(push_mid2), _time.time())
assert event is not None and event[0] == "resolved", event
assert db.get_monitor(push_mid2)["status"] == "up"

os.remove(db.DB_PATH)
print("record_push_ping checks: OK")

# --- /push/<token> route: real Flask test client, same pattern as test_api.py ---
from flask import Flask

import push as push_module

db.init_db()
push_mid3 = db.add_monitor("db-backup", "", "push", interval_sec=60)
token = db.get_monitor(push_mid3)["push_token"]

server = Flask(__name__)
push_module.register_push(server)
client = server.test_client()

# GET works (plain `curl <url>` from a cron job) ...
r = client.get(f"/push/{token}")
assert r.status_code == 200, r.status_code
assert db.get_monitor(push_mid3)["status"] == "up"

# ... and so does POST.
r = client.post(f"/push/{token}")
assert r.status_code == 200

# Unknown token -> 404, not a 500 or a silent no-op.
r = client.get("/push/not-a-real-token")
assert r.status_code == 404

os.remove(db.DB_PATH)
print("/push/<token> endpoint checks: OK")
