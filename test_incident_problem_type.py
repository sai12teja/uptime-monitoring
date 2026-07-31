"""TDD self-check: incidents are scoped by (monitor_id, problem_type), not
just monitor_id — PRD §15's dedup rule ("target, problem type"). Without
this, a monitor with two concurrent incident types (e.g. HTTP down AND SSL
expiring) would have one resolve() call silently resolve BOTH.

Run: ./venv/Scripts/python.exe test_incident_problem_type.py
"""
import os
import time

import db

db.DB_PATH = "test_incident_problem_type.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("dualproblem.invalid", "https://dualproblem.invalid", "website")

# Two DIFFERENT problem types open on the SAME monitor at once.
http_id = db.open_incident(mid, "dualproblem.invalid", "HTTP 500", "critical", problem_type="http")
ssl_id = db.open_incident(mid, "dualproblem.invalid", "SSL expires in 10 days", "warning", problem_type="ssl")
assert http_id != ssl_id

open_incidents = [i for i in db.list_incidents() if i["resolved"] is None]
assert len(open_incidents) == 2

# Resolving the HTTP problem must NOT touch the still-open SSL incident.
downtime_sec = db.resolve_incident(mid, problem_type="http")
assert downtime_sec is not None

incidents_by_id = {i["id"]: i for i in db.list_incidents()}
assert incidents_by_id[http_id]["resolved"] is not None, "http incident should be resolved"
assert incidents_by_id[ssl_id]["resolved"] is None, "ssl incident must still be open — this is the bug"

# Resolving with no matching open incident of that type -> None, not a crash,
# and doesn't touch the other type's incident either.
assert db.resolve_incident(mid, problem_type="http") is None
incidents_by_id_refresh = {i["id"]: i for i in db.list_incidents()}
assert incidents_by_id_refresh[ssl_id]["resolved"] is None

# Default problem_type stays "http" — existing website/CRM call sites in
# monitor_engine.py don't need to change.
mid2 = db.add_monitor("defaulttype.invalid", "https://defaulttype.invalid", "website")
inc_id = db.open_incident(mid2, "defaulttype.invalid", "HTTP 500")
assert db.resolve_incident(mid2) is not None  # default problem_type="http" matches

os.remove(db.DB_PATH)
print("All incident problem_type checks passed.")
