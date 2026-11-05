"""#3 (Option A): name resolution is bounded. A slow/hung getaddrinfo yields a
fast 'DNS timeout' failure instead of stalling the scheduler tick for the full
OS-resolver timeout.

Run: ./venv/Scripts/python.exe test_dns_timeout.py
"""
import time
from unittest.mock import patch

import monitor_engine as me
import ssrf_guard


# --- resolve() returns at the cap, not after the full (slow) lookup ---
def _slow(*a, **k):
    time.sleep(0.5)
    return [(2, 1, 6, "", ("1.2.3.4", 0))]  # valid, in case the abandoned thread is read


with patch("socket.getaddrinfo", side_effect=_slow):
    t0 = time.monotonic()
    try:
        ssrf_guard.resolve("slow.example", timeout=0.2)
        assert False, "resolve must time out"
    except ssrf_guard.DNSTimeout:
        pass
    assert time.monotonic() - t0 < 0.45, "resolve must return at the cap, not after the full lookup"

print("resolve() cap check passed.")


# --- each check turns a DNS timeout into a fast, recorded failure ---
http_mon = {"url": "http://slow.example/", "keyword": None, "http_method": "GET",
            "http_body": None, "http_body_encoding": "json", "timeout_sec": None}
tcp_mon = {"url": "slow.example", "port": 22, "timeout_sec": None}
dns_mon = {"url": "slow.example", "keyword": None}

with patch.object(ssrf_guard, "resolve", side_effect=ssrf_guard.DNSTimeout("slow.example")):
    for name, (ok, ms, detail) in {
        "http": me.do_http_check(http_mon),
        "tcp": me.do_tcp_check(tcp_mon),
        "dns": me.do_dns_check(dns_mon),
    }.items():
        assert ok is False, (name, detail)
        assert "DNS timeout" in detail, (name, detail)

print("DNS timeout checks passed.")
