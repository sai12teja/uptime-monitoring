"""#3 Maintenance windows: during an active window every alert is suppressed
(checks still run, incidents still open), and the incident timeline records
"suppressed" -- not a misleading "email_failed". Plus the db window logic and
the REST API (create via minutes or ends, list, delete, validation). Mocked --
no real network/SMTP.

Run: ./venv/Scripts/python.exe test_maintenance.py
"""
import json
import os
from unittest.mock import patch

import db
import email_alerts

db.DB_PATH = "test_maintenance.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()


# --- active_maintenance_window: inclusive start, exclusive end ---
db.add_maintenance_window(1000, 2000, "deploy")
assert db.active_maintenance_window(1500) is not None
assert db.active_maintenance_window(1000) is not None   # starts inclusive
assert db.active_maintenance_window(2000) is None       # ends exclusive
assert db.active_maintenance_window(999) is None
assert db.active_maintenance_window(5000) is None
print("active_maintenance_window checks passed.")


# --- send() suppresses ALL channels while in a window ---
posted = []
with patch("email_alerts.db.active_maintenance_window", return_value={"id": 1}), \
     patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x", "SMTP_HOST": "smtp.test"}, clear=True), \
     patch("email_alerts.urllib.request.urlopen", side_effect=lambda *a, **k: posted.append(1)), \
     patch("smtplib.SMTP") as mock_smtp:
    assert email_alerts.send("[DOWN] site", "b") is False
assert posted == [], "no webhook POST during maintenance"
mock_smtp.assert_not_called()
print("send() suppression checks passed.")


# --- send_and_record records 'suppressed', not 'email_failed' ---
mid = db.add_monitor("acme", "https://acme.test", "website")
inc = db.open_incident(mid, "acme", "HTTP 500", problem_type="http")
with patch("email_alerts.db.active_maintenance_window", return_value={"id": 1}):
    email_alerts.send_and_record(inc, "[DOWN] acme", "b")
kinds = [e["event_type"] for e in db.list_incident_events(inc)]
assert kinds == ["suppressed"], kinds

# Outside maintenance, a muted/failed send is still 'email_failed' (unchanged).
with patch("email_alerts.db.active_maintenance_window", return_value=None), \
     patch.dict(os.environ, {}, clear=True):
    email_alerts.send_and_record(inc, "[DOWN] acme", "b")
kinds = [e["event_type"] for e in db.list_incident_events(inc)]
assert kinds == ["suppressed", "email_failed"], kinds
print("suppressed-vs-failed labelling checks passed.")


# --- REST API: create (minutes + ends), list, delete, validation ---
from flask import Flask

import api

server = Flask(__name__)
api.register_api(server)
os.environ["API_KEY"] = "k"
c = server.test_client()
c.environ_base["HTTP_X_API_KEY"] = "k"


def j(r):
    return json.loads(r.data)


r = c.post("/maintenance", json={"minutes": 120, "reason": "server migration"})
assert r.status_code == 201, (r.status_code, j(r))
w = j(r)
assert w["ends"] - w["starts"] == 120 * 60 and w["reason"] == "server migration", w
wid = w["id"]

r = c.post("/maintenance", json={"starts": 1000, "ends": 500})  # ends before starts
assert r.status_code == 400 and "after starts" in j(r)["error"], j(r)

r = c.post("/maintenance", json={"reason": "no window given"})  # neither minutes nor ends
assert r.status_code == 400, j(r)

assert any(x["id"] == wid for x in j(c.get("/maintenance")))

assert c.delete(f"/maintenance/{wid}").status_code == 204
assert c.delete(f"/maintenance/{wid}").status_code == 404  # already gone
print("maintenance API checks passed.")

os.remove(db.DB_PATH)
print("All maintenance checks passed.")
