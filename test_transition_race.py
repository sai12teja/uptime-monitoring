"""#8: _record_transition runs under a lock and re-reads the monitor's current
counters, so two transitions from a stale snapshot (a scheduler check and a
concurrent push ping) can't open two incidents for one outage.

Run: ./venv/Scripts/python.exe test_transition_race.py
"""
import os

import db
import monitor_engine as me

db.DB_PATH = "test_transition_race.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

assert me._ALERT_LOCK is not None

mid = db.add_monitor("acme", "https://acme.test", "website")
# One failure away from DOWN (default threshold 3).
db.update_monitor_state(mid, "up", 2, 0, 1000.0, None)
stale = db.get_monitor(mid)  # snapshot: status 'up', consecutive_fails 2

# First failing transition crosses the threshold and opens the incident.
me._record_transition(stale, 1001.0, False, None, "HTTP 500")
assert len([i for i in db.list_incidents() if i["resolved"] is None]) == 1

# A second call with the SAME stale snapshot must NOT open a second incident --
# the refetch-under-lock sees the now-'down' state. (Without it, the stale
# 'up'/fails=2 snapshot would cross the threshold again and double-open.)
me._record_transition(stale, 1002.0, False, None, "HTTP 500")
open_incs = [i for i in db.list_incidents() if i["resolved"] is None]
assert len(open_incs) == 1, open_incs

os.remove(db.DB_PATH)
print("transition race (refetch-under-lock) checks passed.")
