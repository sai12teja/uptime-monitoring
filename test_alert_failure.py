"""#4: a failed/muted send() must not leave the incident timeline claiming an
email was sent, and a failed batch must stay queued for retry instead of
vanishing (the monitors are already 'down', so a dropped alert never re-raises).

Run: ./venv/Scripts/python.exe test_alert_failure.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import monitor_engine as me

db.DB_PATH = "test_alert_failure.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("acme", "https://acme.test", "website")
inc_id = db.open_incident(mid, "acme", "HTTP 500", problem_type="http")


def kinds():
    return [e["event_type"] for e in db.list_incident_events(inc_id)]


# --- (a) send_and_record logs the TRUTHFUL outcome ---
# send() returns False when SMTP is unset / notifications muted / it raised.
with patch.object(email_alerts, "send", return_value=False):
    assert email_alerts.send_and_record(inc_id, "[DOWN] acme", "body") is False
assert kinds() == ["email_failed"], kinds()  # NOT 'email_sent'

# A real send (True) logs email_sent.
with patch.object(email_alerts, "send", return_value=True):
    email_alerts.send_and_record(inc_id, "[DOWN] acme", "body")
assert kinds() == ["email_failed", "email_sent"], kinds()

# A test double returning None counts as sent (back-compat with existing suites
# whose send() mocks return None).
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
    email_alerts.send_and_record(inc_id, "[DOWN] acme", "body")
assert kinds()[-1] == "email_sent", kinds()

# --- (b) a failed batch send stays queued for the next tick ---
me._PENDING["opened"] = [{"name": "acme", "detail": "HTTP 500", "url": None,
                          "incident_id": inc_id, "context": None, "ts": 1000.0}]
me._BATCH_STARTED_AT["opened"] = 1000.0
with patch.object(email_alerts, "send", return_value=False):
    me._flush_batch("opened")
assert len(me._PENDING["opened"]) == 1, "a failed batch must be retained, not dropped"

# Once send succeeds the batch flushes and the queue clears.
with patch.object(email_alerts, "send", return_value=True):
    me._flush_batch("opened")
assert me._PENDING["opened"] == [], me._PENDING["opened"]

print("test_alert_failure passed.")
