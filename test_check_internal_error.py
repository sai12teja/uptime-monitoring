"""#2: a check that raises inside _check_one must still record a failed result
and advance last_checked_at. Otherwise the monitor never goes down, never
alerts, and get_due_monitors re-runs it every TICK_SEC (5s) forever, hammering
the target.

Run: ./venv/Scripts/python.exe test_check_internal_error.py
"""
import os

import db
import monitor_engine as me

db.DB_PATH = "test_check_internal_error.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

# An unknown monitor type makes _check_one's check_fn dict lookup raise
# KeyError -- the exact crash that used to be swallowed with no record.
mid = db.add_monitor("broken", "https://example.com", "bogus-type")
monitor = db.get_monitor(mid)
assert monitor["last_checked_at"] is None

me._check_one(monitor, 1000.0)

after = db.get_monitor(mid)
assert after["last_checked_at"] == 1000.0, after["last_checked_at"]  # advanced, won't re-run every tick

checks = db.list_checks(mid)
assert len(checks) == 1, checks
assert checks[0]["ok"] == 0, checks[0]["ok"]
assert "Internal error" in checks[0]["detail"], checks[0]["detail"]

print("test_check_internal_error passed.")
