"""TDD self-check for gap 7: a real incident_events table (PRD §12) feeding
the incident timeline instead of synthesizing it from checks+incidents.

Run: ./venv/Scripts/python.exe test_incident_events.py
"""
import os
import time

import db

db.DB_PATH = "test_incident_events.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data

mid = db.add_monitor("acme", "https://acme.example", "website")

# --- db.record_incident_event / list_incident_events ---
inc_id = db.open_incident(mid, "acme", "HTTP 500", severity="critical")
db.record_incident_event(inc_id, 100.0, "check_failure", "HTTP 500")
db.record_incident_event(inc_id, 110.0, "email_sent", "[DOWN] acme — HTTP 500")
events = db.list_incident_events(inc_id)
assert [e["event_type"] for e in events] == ["check_failure", "email_sent"]
assert events[0]["detail"] == "HTTP 500"

# --- get_open_incident: the currently-open incident for a monitor+problem_type ---
open_row = db.get_open_incident(mid, problem_type="http")
assert open_row["id"] == inc_id
assert db.get_open_incident(mid, problem_type="ssl") is None

# --- resolve_incident logs its own "resolved" event ---
downtime = db.resolve_incident(mid, problem_type="http")
assert downtime is not None
resolved_events = [e for e in db.list_incident_events(inc_id) if e["event_type"] == "resolved"]
assert len(resolved_events) == 1
assert db.get_open_incident(mid, problem_type="http") is None

# --- resolve_server_incident logs its own "resolved" event ---
server_inc_id = db.open_server_incident("Server", "Disk 95% full", "critical")
db.resolve_server_incident(server_inc_id)
assert [e["event_type"] for e in db.list_incident_events(server_inc_id)] == ["resolved"]

# --- acknowledge_incident logs "acknowledged", but only when it actually applies ---
inc_id2 = db.open_incident(mid, "acme", "HTTP 500", severity="critical")
db.acknowledge_incident(inc_id2, "sam")
ack_events = [e for e in db.list_incident_events(inc_id2) if e["event_type"] == "acknowledged"]
assert len(ack_events) == 1 and ack_events[0]["detail"] == "sam"

db.resolve_incident(mid, problem_type="http")
db.acknowledge_incident(inc_id2, "someone-else")  # already resolved -> no-op, no phantom event
ack_events = [e for e in db.list_incident_events(inc_id2) if e["event_type"] == "acknowledged"]
assert len(ack_events) == 1, "acknowledging an already-resolved incident must not log a second event"

print("db.py incident_events checks: OK")

# --- data.get_incident_detail: timeline = incidents.started ("opened") + incident_events ---
mid2 = db.add_monitor("beta", "https://beta.example", "website")
inc_id3 = db.open_incident(mid2, "beta", "Unreachable", severity="critical")
db.record_incident_event(inc_id3, db.get_incident(inc_id3)["started"], "check_failure", "Unreachable")
db.acknowledge_incident(inc_id3, "sam")
db.resolve_incident(mid2, problem_type="http")

detail = data.get_incident_detail(inc_id3)
kinds = [e["kind"] for e in detail["timeline"]]
assert kinds == ["opened", "check_failure", "acknowledged", "resolved"], kinds
assert detail["timeline"][0]["detail"] == "Unreachable"

# An incident with no incident_events rows at all (pre-existing-data case,
# e.g. one opened before this table existed) still shows its "opened" entry.
mid3 = db.add_monitor("gamma", "https://gamma.example", "website")
inc_id4 = db.open_incident(mid3, "gamma", "Timeout", severity="critical")
bare_detail = data.get_incident_detail(inc_id4)
assert [e["kind"] for e in bare_detail["timeline"]] == ["opened"]

os.remove(db.DB_PATH)
print("data.get_incident_detail incident_events-sourced timeline checks: OK")
