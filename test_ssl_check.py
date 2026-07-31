"""TDD self-check for SSL certificate expiry checking (PRD §5.1/§7.1/§8/§9/§15).

Run: ./venv/Scripts/python.exe test_ssl_check.py
"""
import time
from datetime import datetime, timezone

from ssl_check import ssl_zone_for, parse_cert_expiry, WARN_DAYS

assert WARN_DAYS == 14

# ssl_zone_for: date-based (§8 — "N/A, not consecutive-failure based"), a
# single read is authoritative. An invalid/unreachable cert is immediately
# critical (§7.1: "SSL certificate invalid" is its own detection, distinct
# from the days-remaining countdown).
assert ssl_zone_for(days_remaining=100, error=None) == "ok"
assert ssl_zone_for(days_remaining=14, error=None) == "warning"
assert ssl_zone_for(days_remaining=1, error=None) == "warning"
assert ssl_zone_for(days_remaining=0, error=None) == "critical"
assert ssl_zone_for(days_remaining=-3, error=None) == "critical"  # already expired
assert ssl_zone_for(days_remaining=100, error="Invalid certificate: hostname mismatch") == "critical"
assert ssl_zone_for(days_remaining=None, error="Unreachable: connection refused") == "critical"

# parse_cert_expiry: the exact format ssl.SSLSocket.getpeercert() returns
# for 'notAfter' — getting the format string wrong here silently breaks
# every SSL check with no test ever catching it, since the network-facing
# wrapper always calls this on a real value.
dt = parse_cert_expiry("Jun  1 12:00:00 2027 GMT")
assert dt == datetime(2027, 6, 1, 12, 0, 0, tzinfo=timezone.utc), dt

# Single-digit day (double space, as OpenSSL/ssl module actually emits it).
dt2 = parse_cert_expiry("Sep  9 23:59:59 2025 GMT")
assert dt2 == datetime(2025, 9, 9, 23, 59, 59, tzinfo=timezone.utc), dt2

print("All ssl_check pure-logic checks passed.")


# ---------- check_ssl_for_monitor() — the full pipeline ----------
# Isolated throwaway DB, mocked get_cert_days_remaining (no real network in
# the repeatable suite — proven live separately), captured email_alerts.send.
import os
from unittest.mock import patch

import db
import email_alerts
import ssl_check

db.DB_PATH = "test_ssl_check.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("clientsite.com", "https://clientsite.com", "website")
monitor = db.get_monitor(mid)

sent = []


def tick(days_remaining, error, ts):
    sent.clear()
    with patch("ssl_check.get_cert_days_remaining", return_value=(days_remaining, error)), \
         patch.object(email_alerts, "send", side_effect=lambda s, b: sent.append(s)):
        ssl_check.check_ssl_for_monitor(db.get_monitor(mid), ts)


# Healthy cert, far from expiry -> no incident, no email.
tick(100, None, 1000.0)
assert sent == [], sent
assert db.get_metric_state("ssl:1")["status"] == "ok"
assert len([i for i in db.list_incidents() if i["problem_type"] == "ssl"]) == 0

# But it's not due again for 24h — even a critical reading is ignored
# until CHECK_INTERVAL_SEC has passed (§8: 12-24h cadence).
tick(-5, None, 1000.0 + 10)  # only 10s later
assert sent == [], sent
assert db.get_metric_state("ssl:1")["status"] == "ok"

# 24h later: enters the warning window -> opens at warning, one email, no
# 2-consecutive-reads delay (§8: date-based, a single read is authoritative).
tick(10, None, 1000.0 + ssl_check.CHECK_INTERVAL_SEC)
assert sent == ["[WARNING] clientsite.com — SSL certificate expires in 10 days"], sent
ssl_incidents = [i for i in db.list_incidents() if i["problem_type"] == "ssl"]
assert len(ssl_incidents) == 1 and ssl_incidents[0]["severity"] == "warning"
ssl_inc_id = ssl_incidents[0]["id"]

# gap 7: the alert email is logged as an incident_event against this incident.
email_events = [e for e in db.list_incident_events(ssl_inc_id) if e["event_type"] == "email_sent"]
assert len(email_events) == 1 and email_events[0]["detail"] == sent[0]

# 24h later still: expired -> escalates the SAME incident to critical, one email.
tick(-1, None, 1000.0 + 2 * ssl_check.CHECK_INTERVAL_SEC)
assert sent == ["[CRITICAL] clientsite.com — SSL certificate expired 1 days ago"], sent
ssl_incidents = [i for i in db.list_incidents() if i["problem_type"] == "ssl"]
assert len(ssl_incidents) == 1, "escalation must update the existing incident, not open a second one"
assert ssl_incidents[0]["severity"] == "critical" and ssl_incidents[0]["resolved"] is None

# Renewed -> resolves, one recovery email.
tick(90, None, 1000.0 + 3 * ssl_check.CHECK_INTERVAL_SEC)
assert len(sent) == 1 and sent[0].startswith("[RECOVERED] clientsite.com (SSL) — "), sent
ssl_incidents = [i for i in db.list_incidents() if i["problem_type"] == "ssl"]
assert ssl_incidents[0]["resolved"] is not None

# gap 7: escalation and recovery emails are logged too (3 email_sent events
# total for this incident: opened/warning, escalated/critical, recovered).
email_events = [e for e in db.list_incident_events(ssl_inc_id) if e["event_type"] == "email_sent"]
assert len(email_events) == 3, email_events

# Invalid cert (hostname mismatch etc.) is immediately critical, no warning phase.
tick(None, "Invalid certificate: hostname mismatch", 1000.0 + 4 * ssl_check.CHECK_INTERVAL_SEC)
assert sent == ["[CRITICAL] clientsite.com — Invalid certificate: hostname mismatch"], sent

# http:// monitors have nothing to check — no crash, no state written.
db.add_monitor("internal-tool.local", "http://internal-tool.local", "website")
ssl_check.check_ssl_for_monitor(db.get_monitor(2), time.time())
assert db.get_metric_state("ssl:2") is None

os.remove(db.DB_PATH)
print("All check_ssl_for_monitor checks passed.")
