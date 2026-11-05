"""DNS-record-change detection (a hijack / misconfiguration signal).

If a monitored domain's A/AAAA records suddenly change, the site may have been
hijacked or misconfigured -- the kind of "up but pointing at the wrong server"
problem a plain uptime check can miss. This records a baseline per monitor and
sends a one-shot WARNING when it changes, then adopts the new set (so one alert
per distinct change, never a loop).

OPT-IN (DNS_CHANGE_ALERTS=1), because it's only low-noise for stable-IP hosting
(e.g. your own WHM servers); a CDN-fronted site that rotates IPs would alert on
each rotation. Reuses the bounded ssrf_guard.resolve -- no new dependency, and
a slow/failed lookup never alerts (an unknown is not a change).
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import db
import email_alerts
import ssrf_guard
from domain_check import _is_ip  # same IP-literal guard


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (ValueError, TypeError):
        print(f"[dns_check] invalid {name}={os.environ.get(name)!r}, using {default}")
        return int(default)


CHECK_INTERVAL_SEC = _int_env("DNS_CHANGE_INTERVAL_SEC", 3600)
MAX_WORKERS = 8


def _enabled():
    return os.environ.get("DNS_CHANGE_ALERTS", "").lower() in ("1", "true", "yes")


def _hostname(url):
    parsed = urlparse(url if "//" in (url or "") else f"//{url}")
    return (parsed.hostname or "").strip().lower()


def _signature(hostname):
    """Sorted A/AAAA set as a stable string, or None if it couldn't be resolved
    (NXDOMAIN/timeout) -- an unresolved lookup is never treated as a change."""
    try:
        addrs = ssrf_guard.resolve(hostname)
    except ssrf_guard.DNSTimeout:
        return None
    return ",".join(sorted(addrs)) if addrs else None


def check_monitor_dns(monitor, now, signature=None):
    hostname = _hostname(monitor["url"])
    if not hostname or _is_ip(hostname):
        return
    baseline = db.get_dns_baseline(monitor["id"])
    if baseline is not None and now - baseline["checked_at"] < CHECK_INTERVAL_SEC:
        return  # not due yet
    if signature is None:
        signature = _signature(hostname)
    if signature is None:
        return  # couldn't resolve this cycle -> no baseline write, no alert

    if baseline is None:
        db.set_dns_baseline(monitor["id"], signature, now)  # first observation, silent
        return
    if signature != baseline["records"]:
        detail = f"DNS records changed (was {baseline['records']}, now {signature})"
        subject, body, html = email_alerts.format_alert(monitor["name"], detail, "warning",
                                                        url=monitor["url"], ts=now)
        email_alerts.send(subject, body, html)
    db.set_dns_baseline(monitor["id"], signature, now)  # adopt the new set either way


def check_all_dns(monitors):
    """One pass over due monitors when DNS-change alerts are enabled; resolve
    each unique host concurrently (bounded), then compare serially."""
    if not _enabled():
        return
    now = time.time()
    candidates = []
    for m in monitors:
        host = _hostname(m["url"])
        if not host or _is_ip(host):
            continue
        baseline = db.get_dns_baseline(m["id"])
        if baseline is not None and now - baseline["checked_at"] < CHECK_INTERVAL_SEC:
            continue
        candidates.append((m, host))
    if not candidates:
        return

    hosts = {host for _m, host in candidates}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(hosts))) as pool:
        sigs = dict(zip(hosts, pool.map(_signature, hosts)))

    for m, host in candidates:
        try:
            check_monitor_dns(m, now, signature=sigs.get(host))
        except Exception as e:
            print(f"[dns_check] check failed for monitor {m['id']}: {e}")
