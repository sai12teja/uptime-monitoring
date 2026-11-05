"""#11: a connectivity failure must NOT open a duplicate critical SSL incident
(reachability is the HTTP monitor's job); only a real cert problem does.

Run: ./venv/Scripts/python.exe test_ssl_unreachable.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import ssl_check

db.DB_PATH = "test_ssl_unreachable.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("site", "https://site.example", "website")


def tick(cert_result, ts):
    with patch("ssl_check.get_cert_days_remaining", return_value=cert_result), \
         patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
        ssl_check.check_ssl_for_monitor(db.get_monitor(mid), ts)


# Unreachable (timeout/refused/DNS) -> no SSL incident.
tick((None, "Unreachable: timed out"), 1000.0)
assert [i for i in db.list_incidents() if i["problem_type"] == "ssl"] == [], "connectivity != cert problem"

# A genuine cert verification failure 24h later DOES open a critical incident.
tick((None, "Invalid certificate: certificate has expired"), 1000.0 + ssl_check.CHECK_INTERVAL_SEC)
ssl_incs = [i for i in db.list_incidents() if i["problem_type"] == "ssl"]
assert len(ssl_incs) == 1 and ssl_incs[0]["severity"] == "critical", ssl_incs

os.remove(db.DB_PATH)
print("ssl unreachable-not-incident checks passed.")
