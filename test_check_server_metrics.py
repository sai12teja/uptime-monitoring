"""TDD self-check for server_health.check_server_metrics() — the full
threshold -> incident -> email pipeline (PRD §7.2/§9/§14), wired together.

Bypasses read_all()'s real caching by writing directly into the module's
_cache, so each call represents one controlled, independent "reading" —
matching what "2 consecutive reads" actually means (two distinct
measurement cycles), not two calls within the same 60s cache window.

Run: ./venv/Scripts/python.exe test_check_server_metrics.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import server_health as sh

db.DB_PATH = "test_check_server_metrics.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

sent = []


def tick(cpu, ts):
    fake_metrics = {"cpu_pct": cpu, "mem_pct": 10, "disk_pct": 10, "inodes_pct": 10, "services": {}}
    # read_all() compares its cache timestamp against the real
    # time.monotonic() — these tiny fake `ts` values would always look
    # stale to it and trigger a REAL re-read, clobbering the fake value
    # with this machine's actual metrics. Mock read_all() itself instead,
    # and control only the _cache["ts"] that check_server_metrics() reads
    # for its own "already evaluated this reading" staleness gate.
    sh._cache["ts"] = ts
    with patch.object(sh, "read_all", return_value=fake_metrics), \
         patch.object(email_alerts, "send", side_effect=lambda s, b: sent.append(s)):
        sh.check_server_metrics()


# Two consecutive critical CPU reads -> opens a critical incident + email.
tick(96, 1.0)
assert sent == [], sent  # 1st over-threshold read: not yet committed
tick(97, 2.0)
assert sent == ["[CRITICAL] Server — CPU 97% load"], sent

cpu_incident_id = [i for i in db.list_incidents() if "CPU" in i["problem"]][0]["id"]
email_events = [e for e in db.list_incident_events(cpu_incident_id) if e["event_type"] == "email_sent"]
assert len(email_events) == 1 and email_events[0]["detail"] == sent[0]

# Same reading again (cache hasn't "advanced" — same ts) must NOT re-fire.
tick(97, 2.0)
assert sent == ["[CRITICAL] Server — CPU 97% load"], sent  # unchanged

# Recovery: two consecutive back-to-ok reads -> resolves + recovery email.
sent.clear()
tick(10, 3.0)
assert sent == []
tick(10, 4.0)
assert len(sent) == 1 and sent[0].startswith("[RECOVERED] Server (CPU) — "), sent

email_events = [e for e in db.list_incident_events(cpu_incident_id) if e["event_type"] == "email_sent"]
assert len(email_events) == 2, email_events

os.remove(db.DB_PATH)
print("All check_server_metrics checks passed.")
