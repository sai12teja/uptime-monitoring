"""③ Slow-response alerting: a site that's UP but slow (>= LATENCY_WARN_MS) for
2 consecutive reads opens a WARNING latency incident (separate problem_type, so
it never conflates with up/down), and resolves when it recovers. A down check
never drives latency, and non-http monitor types are ignored.

Run: ./venv/Scripts/python.exe test_latency_alert.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import monitor_engine as me

db.DB_PATH = "test_latency_alert.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("api", "https://api.example", "website")
monitor = db.get_monitor(mid)
sent = []


def lat(response_ms, now):
    sent.clear()
    with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        me._evaluate_latency(db.get_monitor(mid), response_ms, now)


def latency_incidents():
    return [i for i in db.list_incidents() if i["problem_type"] == "latency"]


# One slow read is not enough (2-read confirmation, no single-blip alarms).
lat(me.LATENCY_WARN_MS + 500, 1000)
assert sent == [], sent
assert latency_incidents() == []

# Second consecutive slow read opens a WARNING latency incident, one alert.
lat(me.LATENCY_WARN_MS + 500, 1060)
assert len(sent) == 1 and sent[0].startswith("[WARNING]") and "Slow response" in sent[0], sent
incs = latency_incidents()
assert len(incs) == 1 and incs[0]["severity"] == "warning" and incs[0]["resolved"] is None, incs

# Still slow -> no repeat alert.
lat(me.LATENCY_WARN_MS + 500, 1120)
assert sent == [], sent

# Recovers (2 fast reads) -> resolve, one recovery email.
lat(10, 1180)
lat(10, 1240)
assert len(sent) == 1 and sent[0].startswith("[RECOVERED]") and "(latency)" in sent[0], sent
assert latency_incidents()[0]["resolved"] is not None

# A down check (response_ms irrelevant) and non-http types never drive latency.
before = len(latency_incidents())
me._evaluate_latency(db.get_monitor(mid), None, 1300)  # None -> no-op
tcp = db.get_monitor(db.add_monitor("db", "10.0.0.5", "tcp", port=5432))
me._evaluate_latency(tcp, me.LATENCY_WARN_MS + 999, 1300)  # tcp -> ignored
me._evaluate_latency(tcp, me.LATENCY_WARN_MS + 999, 1360)
assert len(latency_incidents()) == before, "None value / non-http type must not open latency incidents"

os.remove(db.DB_PATH)
print("All latency-alert checks passed.")
