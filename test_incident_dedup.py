"""#12: open_incident is idempotent per (monitor, problem_type) -- the DB layer
now enforces "one open incident per (target, problem type)" -- and the hot
query paths are indexed.

Run: ./venv/Scripts/python.exe test_incident_dedup.py
"""
import os

import db

db.DB_PATH = "test_incident_dedup.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("acme", "https://acme.test", "website")

i1 = db.open_incident(mid, "acme", "HTTP 500", problem_type="http")
i2 = db.open_incident(mid, "acme", "HTTP 500 again", problem_type="http")  # one already open
assert i1 == i2, (i1, i2)  # reused, not duplicated
assert len([i for i in db.list_incidents() if i["resolved"] is None]) == 1

# A different problem_type is still its own incident.
i3 = db.open_incident(mid, "acme", "SSL expiring", problem_type="ssl")
assert i3 != i1

# After resolving, a fresh http incident can open again.
db.resolve_incident(mid, problem_type="http")
i4 = db.open_incident(mid, "acme", "HTTP 500 later", problem_type="http")
assert i4 not in (i1, i3), i4

# Hot-path indexes exist.
with db._conn() as c:
    idx = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
assert "idx_checks_monitor_ts" in idx, idx
assert "idx_incidents_monitor_type" in idx, idx

os.remove(db.DB_PATH)
print("incident dedup + index checks passed.")
