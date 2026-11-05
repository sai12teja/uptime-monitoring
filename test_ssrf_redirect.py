"""#5: SSRF guard hardening -- a 3xx redirect to an internal host is refused,
carrier-grade NAT (100.64.0.0/10) is blocked, and ssl_check never probes a
blocked host.

Run: ./venv/Scripts/python.exe test_ssrf_redirect.py
"""
import os
import urllib.request
from unittest.mock import patch

import monitor_engine as me
import ssrf_guard

# --- CGNAT / shared address space is blocked (ipaddress.is_private misses it
# before Python 3.13). Public addresses are still allowed. ---
assert ssrf_guard.is_blocked_host("100.64.0.1") is True
assert ssrf_guard.is_blocked_host("100.127.255.254") is True
assert ssrf_guard.is_blocked_host("8.8.8.8") is False

# --- redirect re-check: urllib follows 3xx automatically, so a public URL that
# redirects to an internal host must be refused, not followed. ---
handler = me._GuardedRedirectHandler()
req = urllib.request.Request("http://public.example/")

for target in ("http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/"):
    try:
        handler.redirect_request(req, None, 302, "Found", {}, target)
        assert False, f"redirect to {target} must be refused"
    except me._SSRFBlockedError:
        pass

# A redirect to a public host is allowed (delegates to urllib's handler).
new = handler.redirect_request(req, None, 302, "Found", {}, "http://example.org/next")
assert new is not None and new.get_full_url() == "http://example.org/next", new

print("SSRF redirect + CGNAT checks passed.")

# --- ssl_check must skip a monitor whose host is internal (no handshake, no
# incident). get_cert_days_remaining is wired to blow up if it's ever reached. ---
import db
import email_alerts
import ssl_check

db.DB_PATH = "test_ssrf_redirect.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()
mid = db.add_monitor("internal", "https://10.0.0.5", "website")


def _boom(*a, **k):
    raise AssertionError("get_cert_days_remaining must not be called for a blocked host")


with patch("ssl_check.get_cert_days_remaining", side_effect=_boom), \
     patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
    ssl_check.check_ssl_for_monitor(db.get_monitor(mid), 1000.0)

assert [i for i in db.list_incidents() if i["problem_type"] == "ssl"] == []

print("ssl_check SSRF-skip check passed.")
