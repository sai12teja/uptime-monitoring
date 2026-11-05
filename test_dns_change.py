"""#7 DNS A/AAAA-record change detection: opt-in; first observation is silent;
a changed record set sends ONE warning then rebaselines (no loop); an
unchanged set / unresolved lookup / IP host never alert; gated by interval.
Mocked -- no real network.

Run: ./venv/Scripts/python.exe test_dns_change.py
"""
import os
from unittest.mock import patch

import db
import dns_check
import email_alerts

db.DB_PATH = "test_dns_change.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("acme", "https://acme.example", "website")
sent = []


def check(sig, now):
    sent.clear()
    with patch("dns_check._signature", return_value=sig), \
         patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        dns_check.check_monitor_dns(db.get_monitor(mid), now)


INT = dns_check.CHECK_INTERVAL_SEC

# First observation: records the baseline silently.
check("1.1.1.1", 1000)
assert sent == [], sent
assert db.get_dns_baseline(mid)["records"] == "1.1.1.1"

# Same records next cycle -> no alert.
check("1.1.1.1", 1000 + INT)
assert sent == [], sent

# Changed records -> ONE warning that names old and new, then rebaseline.
check("9.9.9.9", 1000 + 2 * INT)
assert len(sent) == 1 and sent[0].startswith("[WARNING]") and "DNS records changed" in sent[0], sent
assert db.get_dns_baseline(mid)["records"] == "9.9.9.9"

# Same (new) records again -> quiet (no repeat).
check("9.9.9.9", 1000 + 3 * INT)
assert sent == [], sent

# An unresolved lookup (None) must NOT alert and must NOT overwrite the baseline.
check(None, 1000 + 4 * INT)
assert sent == [] and db.get_dns_baseline(mid)["records"] == "9.9.9.9", "unknown must not change baseline"

# Gating: measured from the last baseline write. Write one at T, then a change
# 10s later (< interval) must be ignored.
check("9.9.9.9", 1000 + 5 * INT)                 # same sig -> writes checked_at
assert sent == []
check("2.2.2.2", 1000 + 5 * INT + 10)            # 10s later -> gated, no alert
assert sent == [] and db.get_dns_baseline(mid)["records"] == "9.9.9.9", "must be interval-gated"
print("check_monitor_dns checks passed.")

# IP-literal host is skipped entirely.
ip_mid = db.add_monitor("box", "10.0.0.5", "tcp", port=22)
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
    dns_check.check_monitor_dns(db.get_monitor(ip_mid), 5000)
assert db.get_dns_baseline(ip_mid) is None, "IP host must be skipped"

# check_all_dns is a no-op unless DNS_CHANGE_ALERTS is enabled.
with patch.dict(os.environ, {}, clear=False), \
     patch("dns_check._signature", side_effect=AssertionError("must not resolve when disabled")):
    os.environ.pop("DNS_CHANGE_ALERTS", None)
    dns_check.check_all_dns([db.get_monitor(mid)])  # disabled -> returns immediately
print("opt-in + IP-skip checks passed.")

os.remove(db.DB_PATH)
print("All dns-change checks passed.")
