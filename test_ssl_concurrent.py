"""#3 (partial): check_all_ssl must evaluate every due https monitor -- now run
concurrently so a batch can't block the scheduler loop for N*10s -- and skip
non-https monitors entirely.

Run: ./venv/Scripts/python.exe test_ssl_concurrent.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import ssl_check

db.DB_PATH = "test_ssl_concurrent.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

https_ids = [db.add_monitor(f"site{i}", f"https://site{i}.example", "website") for i in range(5)]
http_id = db.add_monitor("plain", "http://plain.example", "website")

# Every cert reads as invalid -> each https monitor should open a critical SSL
# incident; the http monitor must be untouched.
with patch("ssl_check.get_cert_days_remaining", return_value=(None, "Invalid certificate: expired")), \
     patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
    ssl_check.check_all_ssl([db.get_monitor(m) for m in https_ids + [http_id]])

ssl_incidents = [i for i in db.list_incidents() if i["problem_type"] == "ssl"]
assert sorted(i["monitor_id"] for i in ssl_incidents) == sorted(https_ids), ssl_incidents
assert all(i["severity"] == "critical" for i in ssl_incidents), ssl_incidents
assert db.get_metric_state(f"ssl:{http_id}") is None, "non-https monitor must be skipped"

os.remove(db.DB_PATH)
print("ssl concurrent check_all_ssl checks passed.")
