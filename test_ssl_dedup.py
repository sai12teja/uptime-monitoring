"""#3: check_all_ssl does one cert handshake per unique (host, port), not one
per monitor -- two monitors on the same host share a single read.

Run: ./venv/Scripts/python.exe test_ssl_dedup.py
"""
import os
from collections import Counter
from unittest.mock import patch

import db
import email_alerts
import ssl_check

db.DB_PATH = "test_ssl_dedup.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

a1 = db.add_monitor("a1", "https://shared.example/one", "website")
a2 = db.add_monitor("a2", "https://shared.example/two", "website")  # same host+port as a1
b1 = db.add_monitor("b1", "https://other.example", "website")

calls = Counter()


def fake_cert(hostname, port=443, timeout=10):
    calls[(hostname, port)] += 1
    return 100, None  # healthy


with patch("ssl_check.get_cert_days_remaining", side_effect=fake_cert), \
     patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
    ssl_check.check_all_ssl([db.get_monitor(m) for m in (a1, a2, b1)])

assert calls[("shared.example", 443)] == 1, dict(calls)  # ONE handshake for the shared host
assert calls[("other.example", 443)] == 1, dict(calls)
assert sum(calls.values()) == 2, dict(calls)             # 3 monitors, 2 handshakes

# ...but all three monitors were still evaluated (state written for each).
for mid in (a1, a2, b1):
    assert db.get_metric_state(f"ssl:{mid}") is not None, mid

os.remove(db.DB_PATH)
print("ssl per-host dedup checks passed.")
