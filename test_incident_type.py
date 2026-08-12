"""TDD self-check: an incident's problem_type must reflect the check that
actually failed.

Everything was being recorded as "http" because _record_transition never
passed a type, so a DNS or TCP failure filed an HTTP incident. That matters
beyond cosmetics: resolve_incident() is scoped BY problem_type, so a
mismatched type can leave an incident open forever or resolve the wrong one.

Run: ./venv/Scripts/python.exe test_incident_type.py
"""
import os

import db

db.DB_PATH = "test_incident_type.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import monitor_engine


def problem_type_for(monitor_type):
    """The type recorded when a monitor of `monitor_type` goes down."""
    mid = db.add_monitor(f"t-{monitor_type}", "example.com", monitor_type, port=443)
    monitor = db.get_monitor(mid)
    # Three consecutive failures crosses the default fail threshold and opens
    # the incident.
    result = None
    for _ in range(3):
        monitor = db.get_monitor(mid)
        result = monitor_engine._record_transition(
            monitor, __import__("time").time(), False, None, "simulated failure")
    assert result and result[0] == "opened", result
    row = db.get_incident(result[3])
    return row["problem_type"]


assert problem_type_for("dns") == "dns", problem_type_for("dns")
assert problem_type_for("tcp") == "tcp"
assert problem_type_for("website") == "http", "website checks are HTTP requests"
assert problem_type_for("crm") == "http", "crm is an HTTP check too"

# --- an opened incident must be resolvable: resolve_incident() is scoped by
# problem_type, so a mismatch between open and resolve leaves it open ---
mid = db.add_monitor("resolve-me", "example.com", "dns")
import time as _time
for _ in range(3):
    monitor_engine._record_transition(db.get_monitor(mid), _time.time(), False, None, "fail")
assert db.get_open_incident(mid, problem_type="dns") is not None, "dns incident must be open"

for _ in range(2):
    res = monitor_engine._record_transition(db.get_monitor(mid), _time.time(), True, None, "ok")
assert db.get_open_incident(mid, problem_type="dns") is None, "dns incident must resolve"

os.remove(db.DB_PATH)
print("All incident problem_type checks passed.")
