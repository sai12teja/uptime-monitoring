"""#3: check_ssl_for_monitor must check the certificate on the URL's own port,
not always 443. An https monitor on a non-standard port (e.g. :8443) was
checking the wrong port's cert.

Run: ./venv/Scripts/python.exe test_ssl_port.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import ssl_check

db.DB_PATH = "test_ssl_port.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

captured = {}


def fake_cert(hostname, port=443, timeout=ssl_check.CHECK_TIMEOUT_SEC):
    captured["hostname"], captured["port"] = hostname, port
    return 100, None  # healthy cert, no incident


def check(url, ts):
    mid = db.add_monitor("m", url, "website")
    with patch("ssl_check.get_cert_days_remaining", side_effect=fake_cert), \
         patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
        ssl_check.check_ssl_for_monitor(db.get_monitor(mid), ts)


# Explicit non-standard port must be used (was hard-coded 443 before the fix).
check("https://example.com:8443", 1000.0)
assert captured == {"hostname": "example.com", "port": 8443}, captured

# Plain https with no port still defaults to 443.
check("https://example.org", 2000.0)
assert captured == {"hostname": "example.org", "port": 443}, captured

print("test_ssl_port passed.")
