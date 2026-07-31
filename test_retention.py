"""TDD self-check for gap 8: checks-table retention (PRD §16 "configurable
history window for checks (e.g. 90 days); incidents retained longer").

Run: ./venv/Scripts/python.exe test_retention.py
"""
import os
import time

import db

db.DB_PATH = "test_retention.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import monitor_engine

mid = db.add_monitor("acme", "https://acme.example", "website")

now = time.time()
DAY = 86400

db.record_check(mid, now - 100 * DAY, True, 50, "old")   # older than 90d default
db.record_check(mid, now - 91 * DAY, True, 50, "old2")   # older than 90d default
db.record_check(mid, now - 89 * DAY, True, 50, "recent")  # within window
db.record_check(mid, now - 1 * DAY, True, 50, "recent2")  # within window

# --- db.delete_old_checks: deletes rows strictly older than the cutoff ---
cutoff = now - 90 * DAY
deleted = db.delete_old_checks(cutoff)
assert deleted == 2, deleted
remaining = [c["detail"] for c in db.list_checks(mid, limit=100)]
assert sorted(remaining) == ["recent", "recent2"], remaining

# --- incidents are never touched by retention (PRD: "retained longer" / auditability) ---
inc_id = db.open_incident(mid, "acme", "old problem", severity="critical")
db.resolve_incident(mid)
db.delete_old_checks(now + DAY)  # even a cutoff in the future
assert db.get_incident(inc_id) is not None
assert db.list_incident_events(inc_id) != []

os.remove(db.DB_PATH)
print("db.delete_old_checks checks: OK")

# --- monitor_engine.purge_old_checks: configurable window + once-per-day gate ---
db.DB_PATH = "test_retention_engine.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid2 = db.add_monitor("beta", "https://beta.example", "website")
db.record_check(mid2, now - 100 * DAY, True, 50, "old")
db.record_check(mid2, now - 1 * DAY, True, 50, "recent")

monitor_engine.CHECKS_RETENTION_DAYS = 90  # explicit, don't depend on env/import-time default
monitor_engine.purge_old_checks()
remaining = [c["detail"] for c in db.list_checks(mid2, limit=100)]
assert remaining == ["recent"], remaining

# Running again immediately (same day) must not re-scan/re-purge -- add a
# fresh old-looking row and confirm the gate skips it until the interval passes.
db.record_check(mid2, now - 100 * DAY, True, 50, "old-again")
monitor_engine.purge_old_checks()
remaining = [c["detail"] for c in db.list_checks(mid2, limit=100)]
assert "old-again" in remaining, "gate should have skipped this run (< 1 purge interval since last)"

# Force the gate open (simulate a day having passed) -> purge runs again.
state = db.get_metric_state("checks_retention_purge")
db.set_metric_state("checks_retention_purge", "ok", None, 0, None,
                     state["last_read_ts"] - monitor_engine.PURGE_INTERVAL_SEC - 1)
monitor_engine.purge_old_checks()
remaining = [c["detail"] for c in db.list_checks(mid2, limit=100)]
assert remaining == ["recent"], remaining

os.remove(db.DB_PATH)
print("monitor_engine.purge_old_checks checks: OK")
