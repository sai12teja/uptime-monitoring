"""Assert-based self-check for the aria-live diffing logic (Decision 6.2).

Run: ./venv/Scripts/python.exe test_dashboard.py
"""

from app import diff_status_messages, diff_outage_message, announce_changes

TARGETS_A = [
    {"id": 1, "name": "acmecorp.com", "status": "up"},
    {"id": 2, "name": "clientsite.com", "status": "down"},
]
TARGETS_B = [
    {"id": 1, "name": "acmecorp.com", "status": "down"},
    {"id": 2, "name": "clientsite.com", "status": "down"},
]
TARGETS_C = [
    {"id": 1, "name": "acmecorp.com", "status": "down"},
    {"id": 2, "name": "clientsite.com", "status": "up"},
]

# No prior baseline (first tick after page load) — never announce.
assert diff_status_messages({}, TARGETS_A) == []

# Baseline present, nothing changed — silent (never announce a no-op poll).
prev = {t["id"]: t["status"] for t in TARGETS_A}
assert diff_status_messages(prev, TARGETS_A) == []

# One target flipped up -> down.
assert diff_status_messages(prev, TARGETS_B) == ["acmecorp.com is now down"]

# Two targets flip in the same tick -> two messages.
assert diff_status_messages(prev, TARGETS_C) == [
    "acmecorp.com is now down",
    "clientsite.com is now up",
]

# Outage transitions: no prior state -> silent; no change -> silent;
# actual flip in either direction -> a message.
assert diff_outage_message(None, False) is None
assert diff_outage_message(False, False) is None
assert diff_outage_message(False, True) == "Server unreachable — all checks failing"
assert diff_outage_message(True, False) == "Server recovered — all checks passing"

# Integration check: the actual wired Dash callback (not just the pure
# helpers above) reads live data (db.py) and reacts correctly to a
# transition.
import os
import time

import db

# Redirect every db call at a throwaway file BEFORE the first query, so this
# test never writes into the dashboard's real rovix.db. db._conn() reads
# db.DB_PATH at call time, so rebinding it here is enough — importing app
# above only ran CREATE TABLE IF NOT EXISTS against the real DB, no rows.
db.DB_PATH = "test_rovix.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)  # a previous failed run may have left one behind
db.init_db()

import data
from dash import no_update

test_id = db.add_monitor("test-integration-check.invalid", "https://test-integration-check.invalid", "website")
db.update_monitor_state(test_id, "up", 0, 1, time.time(), 100)

current = {t["id"]: t["status"] for t in data.get_targets()}
assert current[test_id] == "up"

# Baseline identical to current data -> nothing changed -> no_update.
same_state = {"targets": dict(current), "outage": False}
announcement, _ = announce_changes(1, same_state)
assert announcement is no_update, announcement

# DB state flips to down; baseline still says "up" -> the callback must
# catch that transition by reading live data, not the stale baseline.
db.update_monitor_state(test_id, "down", 3, 0, time.time(), None)
announcement, new_state = announce_changes(1, same_state)
assert announcement == "test-integration-check.invalid is now down", announcement
assert new_state["targets"][test_id] == "down"

os.remove(db.DB_PATH)

print("All diff_status_messages / diff_outage_message checks passed.")
