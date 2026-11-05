"""External dead-man's switch for Rovix itself (PRD §6).

Rovix cannot alert you that IT died -- it's the thing that sends the alerts.
So while alive it pings an external heartbeat service (healthchecks.io,
Cronitor, or any URL that expects a periodic ping); if Rovix's scheduler loop
stalls or the process dies, those pings stop and that service's OWN alerting
fires. Set HEARTBEAT_URL to enable; unset -> disabled.

The ping is called at the end of every scheduler tick, so a ping means a whole
tick completed -- liveness of the actual check loop, not just the process.
Gated in-memory to HEARTBEAT_INTERVAL_SEC; after a restart the next tick pings
immediately, which is correct.
"""
import os
import time
import urllib.request

HEARTBEAT_TIMEOUT_SEC = 10
_last_ping = 0.0


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (ValueError, TypeError):
        print(f"[heartbeat] invalid {name}={os.environ.get(name)!r}, using {default}")
        return int(default)


def ping(now=None):
    """Ping the external heartbeat if configured and due. Called every tick;
    a no-op when disabled or not yet due. Never raises -- a missed ping is
    exactly the signal the external watch exists to catch. Returns True only
    when a ping was actually sent and accepted (2xx)."""
    global _last_ping
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        return False
    now = time.time() if now is None else now
    if now - _last_ping < _int_env("HEARTBEAT_INTERVAL_SEC", 60):
        return False
    _last_ping = now
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RovixMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT_SEC) as resp:
            return 200 <= getattr(resp, "status", 0) < 300
    except Exception as e:
        print(f"[heartbeat] ping failed: {e}")
        return False
