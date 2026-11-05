"""#6 External dead-man's switch: while alive, Rovix pings a configured URL
every HEARTBEAT_INTERVAL_SEC; disabled when unset; gated so it doesn't ping
every 5s tick; failures are swallowed (a missed ping is the whole point).
Mocked -- no real network.

Run: ./venv/Scripts/python.exe test_heartbeat.py
"""
import os
from unittest.mock import patch

import heartbeat


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- disabled when HEARTBEAT_URL is unset: no request at all ---
calls = []
with patch.dict(os.environ, {}, clear=True), \
     patch("heartbeat.urllib.request.urlopen", side_effect=lambda *a, **k: calls.append(1)):
    heartbeat._last_ping = 0.0
    assert heartbeat.ping(now=1000) is False
assert calls == [], "disabled heartbeat must not ping"
print("disabled-no-ping check passed.")


# --- enabled: pings once, then GATED until the interval elapses ---
hit = []
with patch.dict(os.environ, {"HEARTBEAT_URL": "https://hc.test/abc", "HEARTBEAT_INTERVAL_SEC": "60"}, clear=True), \
     patch("heartbeat.urllib.request.urlopen",
           side_effect=lambda req, timeout=None: hit.append(req.full_url) or _Resp(200)):
    heartbeat._last_ping = 0.0
    assert heartbeat.ping(now=1000) is True          # first: pings
    assert heartbeat.ping(now=1030) is False          # 30s later: gated, no ping
    assert heartbeat.ping(now=1100) is True           # 100s later: due again
assert hit == ["https://hc.test/abc", "https://hc.test/abc"], hit
print("enabled + gating checks passed.")


# --- a failed ping is swallowed (never raises), returns False ---
with patch.dict(os.environ, {"HEARTBEAT_URL": "https://hc.test/abc"}, clear=True), \
     patch("heartbeat.urllib.request.urlopen", side_effect=OSError("network down")):
    heartbeat._last_ping = 0.0
    assert heartbeat.ping(now=2000) is False
print("failure-swallowed check passed.")

print("All heartbeat checks passed.")
