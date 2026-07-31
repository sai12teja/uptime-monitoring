"""Assert-based self-check for gap 5's db.py additions: monitor edit/soft-delete,
single-incident lookup, and incident filtering (PRD §11).

Run: ./venv/Scripts/python.exe test_monitor_crud.py
"""
import os
import time

import db

db.DB_PATH = "test_rovix_crud.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

# --- update_monitor: edits name/url/keyword/interval_sec in place ---
mid = db.add_monitor("acme", "https://acme.example", "website")
db.update_monitor(mid, "acme corp", "https://acme.example/new", "Welcome", 120)
row = db.get_monitor(mid)
assert row["name"] == "acme corp"
assert row["url"] == "https://acme.example/new"
assert row["keyword"] == "Welcome"
assert row["interval_sec"] == 120

# --- deactivate_monitor: soft-delete, disappears from list_monitors but
# get_monitor (used for history/detail lookups) still finds it ---
assert any(m["id"] == mid for m in db.list_monitors())
db.deactivate_monitor(mid)
assert not any(m["id"] == mid for m in db.list_monitors())
assert db.get_monitor(mid) is not None
assert db.get_monitor(mid)["active"] == 0

# New monitors default to active.
mid2 = db.add_monitor("beta", "https://beta.example", "website")
assert db.get_monitor(mid2)["active"] == 1

# --- get_incident: single-row lookup for GET /incidents/{id} ---
inc_id = db.open_incident(mid2, "beta", "Unreachable", severity="critical")
inc = db.get_incident(inc_id)
assert inc["id"] == inc_id
assert inc["target_name"] == "beta"
assert db.get_incident(999999) is None

# --- list_incidents filters: status, monitor_id, date range ---
db.resolve_incident(mid2)
mid3 = db.add_monitor("gamma", "https://gamma.example", "website")
open_id = db.open_incident(mid3, "gamma", "Unreachable", severity="critical")

all_incidents = db.list_incidents()
assert {i["id"] for i in all_incidents} == {inc_id, open_id}

open_only = db.list_incidents(status="open")
assert {i["id"] for i in open_only} == {open_id}

resolved_only = db.list_incidents(status="resolved")
assert {i["id"] for i in resolved_only} == {inc_id}

by_target = db.list_incidents(monitor_id=mid3)
assert {i["id"] for i in by_target} == {open_id}

future_only = db.list_incidents(since=time.time() + 3600)
assert future_only == []

past_only = db.list_incidents(until=time.time() - 3600)
assert past_only == []

os.remove(db.DB_PATH)
print("db.py monitor CRUD + incident lookup/filter checks: OK")
