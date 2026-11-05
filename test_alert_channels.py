"""Multi-channel alerting: send() fans out to email + Slack/Discord/generic
webhooks, each channel isolated, and returns True if ANY channel delivered
(so batch retry only fires when the alert reached nobody). The global notify
toggle mutes every channel. Mocked -- no real network/SMTP.

Run: ./venv/Scripts/python.exe test_alert_channels.py
"""
import json
import os
from unittest.mock import MagicMock, patch

import db
import email_alerts

db.DB_PATH = "test_alert_channels.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- webhook-only (no SMTP): the alert still gets delivered ---
posted = []


def fake_urlopen(req, timeout=None):
    posted.append((req.full_url, json.loads(req.data.decode())))
    return _Resp(200)


with patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "https://hooks.slack.test/abc"}, clear=True), \
     patch("email_alerts.urllib.request.urlopen", side_effect=fake_urlopen):
    ok = email_alerts.send("[DOWN] site — HTTP 500", "body text")
assert ok is True, "a webhook delivery must count as sent even with no SMTP"
assert posted and posted[0][0] == "https://hooks.slack.test/abc"
assert posted[0][1]["text"] == "*[DOWN] site — HTTP 500*\nbody text", posted[0][1]
print("webhook-only delivery checks passed.")

# --- generic webhook gets structured fields ---
posted.clear()
with patch.dict(os.environ, {"ALERT_WEBHOOK_URL": "https://recv.test/hook"}, clear=True), \
     patch("email_alerts.urllib.request.urlopen", side_effect=fake_urlopen):
    ok = email_alerts.send("[WARNING] x", "slow")
assert ok is True
assert posted[0][1] == {"subject": "[WARNING] x", "body": "slow"}, posted[0][1]
print("generic webhook payload checks passed.")

# --- email + webhook together; a failing webhook does NOT sink email ---
with patch.dict(os.environ, {"SMTP_HOST": "smtp.test", "SMTP_PORT": "25",
                             "SLACK_WEBHOOK_URL": "https://hooks.slack.test/abc"}, clear=True), \
     patch("smtplib.SMTP") as mock_smtp, \
     patch("email_alerts.urllib.request.urlopen", side_effect=OSError("webhook down")):
    mock_smtp.return_value.__enter__.return_value = MagicMock()
    ok = email_alerts.send("[DOWN] y", "b")
assert ok is True, "email success stands even when the webhook channel errors"
print("channel isolation checks passed.")

# --- nothing configured -> not delivered (False, so a batch will retry) ---
with patch.dict(os.environ, {}, clear=True):
    assert email_alerts.send("[DOWN] z", "b") is False
print("no-channel -> False checks passed.")

# --- global mute suppresses ALL channels (no webhook POST attempted) ---
db.update_settings("a@x.com", None, None, notify_enabled=False)
posted.clear()
with patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "https://hooks.slack.test/abc"}, clear=True), \
     patch("email_alerts.urllib.request.urlopen", side_effect=fake_urlopen):
    assert email_alerts.send("[DOWN] muted", "b") is False
assert posted == [], "mute must stop webhooks too"
print("global-mute checks passed.")

os.remove(db.DB_PATH)
print("All alert-channel checks passed.")
