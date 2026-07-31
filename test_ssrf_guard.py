"""TDD self-check for the SSRF/internal-target guard added in response to
security review finding H2: monitor checks must not be usable to probe
cloud metadata endpoints or other hosts on the local network, while
still allowing the legitimate/already-tested case of monitoring a
service on the same box the scheduler runs on (loopback).

Run: ./venv/Scripts/python.exe test_ssrf_guard.py
"""
from monitor_engine import _is_blocked_host, do_http_check, do_tcp_check

# --- _is_blocked_host: the actual classification logic ---

assert _is_blocked_host("169.254.169.254") is True, "cloud metadata endpoint must be blocked"
assert _is_blocked_host("10.0.0.5") is True
assert _is_blocked_host("172.16.0.1") is True
assert _is_blocked_host("192.168.1.1") is True
assert _is_blocked_host("fc00::1") is True

# Loopback is deliberately exempt (see docstring) -- self-monitoring is a
# legitimate use case, and this repo's own test suite depends on it.
assert _is_blocked_host("127.0.0.1") is False
assert _is_blocked_host("localhost") is False

# Public targets are unaffected.
assert _is_blocked_host("8.8.8.8") is False
assert _is_blocked_host("1.1.1.1") is False

# Unresolvable -> not blocked; the real check should report the DNS
# failure with its own detail message, not a misleading "blocked".
assert _is_blocked_host("this-host-does-not-exist.invalid") is False

print("All _is_blocked_host classification checks passed.")


# --- do_http_check / do_tcp_check actually short-circuit before connecting ---

ok, response_ms, detail = do_http_check({
    "url": "http://169.254.169.254/latest/meta-data/", "keyword": None,
    "http_method": "GET", "http_body": None, "http_body_encoding": "json",
    "timeout_sec": None,
})
assert ok is False
assert response_ms == 0
assert "Blocked" in detail, detail

ok, response_ms, detail = do_tcp_check({"url": "192.168.1.1", "port": 22, "timeout_sec": None})
assert ok is False
assert response_ms == 0
assert "Blocked" in detail, detail

print("All do_http_check/do_tcp_check SSRF-guard checks passed.")
print("All SSRF guard checks passed.")
