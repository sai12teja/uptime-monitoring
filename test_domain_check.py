"""#4 Domain-registration expiry monitoring. Pure zone/parse logic, the RDAP
walk-up to the registrable domain (mocked -- no network), and the
check_domain_for_monitor pipeline (warning -> critical -> resolve, unknown
lookups never alert, IP hosts skipped, 24h gating).

Run: ./venv/Scripts/python.exe test_domain_check.py
"""
import os
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import db
import domain_check as dc
import email_alerts

db.DB_PATH = "test_domain_check.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()


# --- domain_zone_for: unknown stays unknown; an unreachable lookup is NOT critical ---
assert dc.domain_zone_for(None) is None            # couldn't look it up -> no alert
assert dc.domain_zone_for(100) == "ok"
assert dc.domain_zone_for(dc.WARN_DAYS) == "warning"
assert dc.domain_zone_for(1) == "warning"
assert dc.domain_zone_for(0) == "critical"
assert dc.domain_zone_for(-5) == "critical"
print("domain_zone_for checks passed.")


# --- _expiry_from / _parse_rdap_date: pull the expiration event ---
soon = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
data = {"events": [{"eventAction": "registration", "eventDate": "2000-01-01T00:00:00Z"},
                   {"eventAction": "expiration", "eventDate": soon}]}
assert abs((dc._expiry_from(data) - datetime.now(timezone.utc)).days - 10) <= 1
assert dc._expiry_from({"events": []}) is None
print("RDAP expiry-parse checks passed.")


# --- get_domain_expiry: walks up from a subdomain to the registrable domain ---
def fake_rdap(candidate):
    if candidate == "sub.example.com":
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    if candidate == "example.com":
        return {"events": [{"eventAction": "expiration", "eventDate": soon}]}
    raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)


with patch("domain_check._rdap", side_effect=fake_rdap):
    days, err = dc.get_domain_expiry("sub.example.com")
    assert err is None and 9 <= days <= 11, (days, err)
    days, err = dc.get_domain_expiry("www.example.com")  # www stripped
    assert err is None and 9 <= days <= 11, (days, err)
print("get_domain_expiry walk-up checks passed.")


# --- check_domain_for_monitor pipeline ---
mid = db.add_monitor("acme", "https://acme.example", "website")
sent = []
INT = dc.CHECK_INTERVAL_SEC


def tick(result, ts):
    sent.clear()
    with patch("domain_check.get_domain_expiry", return_value=result), \
         patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        dc.check_domain_for_monitor(db.get_monitor(mid), ts)


def domain_incidents():
    return [i for i in db.list_incidents() if i["problem_type"] == "domain"]


# Healthy (far from expiry) -> nothing.
tick((100, None), 1000)
assert sent == [] and domain_incidents() == []

# Not due again for 24h, even a bad reading is ignored until the interval passes.
tick((-1, None), 1000 + 10)
assert sent == [] and domain_incidents() == []

# 24h later: inside the warning window -> opens a warning incident, one alert.
tick((10, None), 1000 + INT)
assert len(sent) == 1 and sent[0].startswith("[WARNING]") and "expires in 10 days" in sent[0], sent
incs = domain_incidents()
assert len(incs) == 1 and incs[0]["severity"] == "warning", incs

# Expired -> escalates the SAME incident to critical.
tick((-2, None), 1000 + 2 * INT)
assert len(sent) == 1 and sent[0].startswith("[CRITICAL]") and "EXPIRED" in sent[0], sent
assert len(domain_incidents()) == 1 and domain_incidents()[0]["severity"] == "critical"

# Renewed -> resolves.
tick((200, None), 1000 + 3 * INT)
assert len(sent) == 1 and sent[0].startswith("[RECOVERED]") and "(domain)" in sent[0], sent
assert domain_incidents()[0]["resolved"] is not None

# An unknown lookup (unsupported TLD / RDAP down) must NOT alert or open anything.
mid2 = db.add_monitor("weird", "https://weird.tld", "website")
with patch("domain_check.get_domain_expiry", return_value=(None, "RDAP 404")), \
     patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(1)):
    dc.check_domain_for_monitor(db.get_monitor(mid2), 5000)
assert not [i for i in db.list_incidents() if i["target_name"] == "weird"], "unknown lookup must not alert"

# An IP-literal monitor is skipped entirely (nothing to expire).
ip_mid = db.add_monitor("box", "10.0.0.5", "tcp", port=22)
dc.check_domain_for_monitor(db.get_monitor(ip_mid), 6000)
assert db.get_metric_state(f"domain:{ip_mid}") is None, "IP host must be skipped"
print("check_domain_for_monitor pipeline checks passed.")

os.remove(db.DB_PATH)
print("All domain_check checks passed.")
