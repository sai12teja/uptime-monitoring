"""#4c/#3: batched alerts survive a restart. Queued items are persisted so a
fresh process reloads them -- the up-to-ALERT_BATCH_WINDOW_SEC batch window is
no longer lost on restart.

Run: ./venv/Scripts/python.exe test_pending_alert_durability.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import monitor_engine as me

db.DB_PATH = "test_pending_durability.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("acme", "https://acme.test", "website")
inc = db.open_incident(mid, "acme", "HTTP 500", problem_type="http")

# The live process queues a down alert well inside the batch window (no flush).
me._PENDING = {"opened": [], "resolved": []}
me._BATCH_STARTED_AT = {"opened": None, "resolved": None}
me._queue_alert("opened", {"name": "acme", "detail": "HTTP 500", "url": None,
                           "incident_id": inc, "context": None, "ts": 1000.0}, now=1000.0)

rows = db.list_pending_alerts()
assert len(rows) == 1 and rows[0]["kind"] == "opened", rows  # persisted

# Restart: wipe in-memory state, reload from the DB.
me._PENDING = {"opened": [], "resolved": []}
me._BATCH_STARTED_AT = {"opened": None, "resolved": None}
me._load_pending_alerts()
assert len(me._PENDING["opened"]) == 1, me._PENDING
assert me._PENDING["opened"][0]["detail"] == "HTTP 500", me._PENDING
assert me._BATCH_STARTED_AT["opened"] == 1000.0, me._BATCH_STARTED_AT

# A successful flush clears both memory and the persisted rows.
with patch.object(email_alerts, "send", return_value=True):
    me._flush_batch("opened")
assert me._PENDING["opened"] == [], me._PENDING
assert db.list_pending_alerts() == [], db.list_pending_alerts()

# A FAILED send keeps the persisted row for the next tick / restart.
me._queue_alert("opened", {"name": "beta", "detail": "HTTP 503", "url": None,
                           "incident_id": inc, "context": None, "ts": 2000.0}, now=2000.0)
with patch.object(email_alerts, "send", return_value=False):
    me._flush_batch("opened")
assert len(db.list_pending_alerts()) == 1, "a failed send must not drop the persisted alert"

os.remove(db.DB_PATH)
print("pending-alert durability checks passed.")
