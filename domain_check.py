"""Domain-registration expiry monitoring (a close sibling of ssl_check.py).

A domain silently lapsing takes every site on it down just like an expired
TLS cert -- and it's the "up but actually broken" class Rovix exists for. This
warns before the registration expires.

Lookups use RDAP (the IETF JSON successor to WHOIS) over urllib -- NO new
dependency and no fragile WHOIS text parsing. We query the public rdap.org
bootstrap service, which redirects to the authoritative registry; we never
fetch the monitored host itself, so there's no SSRF surface here. Date-based
like ssl_check: a single reading is authoritative, so it reuses
server_health.evaluate_metric_zone with threshold=1. A domain we can't resolve
(unsupported TLD, no expiry field) returns None days and simply never alerts.
"""
import ipaddress
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import db
import email_alerts
from server_health import evaluate_metric_zone


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (ValueError, TypeError):
        print(f"[domain_check] invalid {name}={os.environ.get(name)!r}, using {default}")
        return int(default)


WARN_DAYS = _int_env("DOMAIN_WARN_DAYS", 30)       # domains need more lead time than certs
CHECK_INTERVAL_SEC = _int_env("DOMAIN_CHECK_INTERVAL_SEC", 24 * 3600)
TIMEOUT_SEC = 10
MAX_WORKERS = 8


def _hostname(url):
    parsed = urlparse(url if "//" in (url or "") else f"//{url}")
    return (parsed.hostname or "").strip().lower()


def _is_ip(host):
    # tcp/dns monitors may hold a bare IP -- an IP has no registration to expire.
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _rdap(domain):
    url = f"https://rdap.org/domain/{urllib.parse.quote(domain)}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/rdap+json", "User-Agent": "RovixMonitor/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return json.loads(resp.read())


def _parse_rdap_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _expiry_from(data):
    for event in data.get("events") or []:
        if event.get("eventAction") == "expiration":
            return _parse_rdap_date(event.get("eventDate"))
    return None


def get_domain_expiry(hostname):
    """(days_remaining: float|None, error: str|None) for a hostname's registered
    domain. Strips `www.` and walks up to the registrable domain -- so
    sub.example.co.uk resolves example.co.uk without a public-suffix list. None
    days means it couldn't be determined (unsupported TLD / no expiry), and the
    caller then does NOT alert (an unknown is not an expiry)."""
    labels = hostname.split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    last_err = None
    for i in range(max(1, len(labels) - 1)):  # example.com, then co.uk-style, ...
        candidate = ".".join(labels[i:])
        if candidate.count(".") < 1:
            break
        try:
            data = _rdap(candidate)
        except urllib.error.HTTPError as e:
            last_err = f"RDAP {e.code}"
            continue  # too-specific (a subdomain); try the next shorter candidate
        except Exception as e:
            return None, f"RDAP error: {e}"
        expiry = _expiry_from(data)
        if expiry is not None:
            return (expiry - datetime.now(timezone.utc)).total_seconds() / 86400, None
        return None, "no expiration date in RDAP"
    return None, last_err or "no RDAP data"


def domain_zone_for(days_remaining):
    """None (unknown) stays None -- never alert on a lookup we couldn't make.
    Unlike SSL, an unreachable/unsupported lookup is NOT critical."""
    if days_remaining is None:
        return None
    if days_remaining <= 0:
        return "critical"
    if days_remaining <= WARN_DAYS:
        return "warning"
    return "ok"


def _detail(days_remaining):
    if days_remaining <= 0:
        return f"Domain registration EXPIRED {abs(int(days_remaining))} days ago"
    return f"Domain registration expires in {int(days_remaining)} days"


def check_domain_for_monitor(monitor, now, expiry_result=None):
    hostname = _hostname(monitor["url"])
    if not hostname or _is_ip(hostname):
        return  # nothing registrable to check

    state_key = f"domain:{monitor['id']}"
    state = db.get_metric_state(state_key)
    if state is not None and now - state["last_read_ts"] < CHECK_INTERVAL_SEC:
        return  # not due yet (domains change slowly -- once a day is plenty)

    if expiry_result is None:
        expiry_result = get_domain_expiry(hostname)
    days_remaining, error = expiry_result

    status = state["status"] if state else "ok"
    pending_zone = state["pending_zone"] if state else None
    pending_count = state["pending_count"] if state else 0
    incident_id = state["incident_id"] if state else None

    zone = domain_zone_for(days_remaining)  # None on a failed lookup -> no transition
    new_status, npz, npc, transition = evaluate_metric_zone(status, pending_zone, pending_count, zone, threshold=1)

    if transition in ("open", "escalate"):
        detail = _detail(days_remaining)
        if transition == "open":
            incident_id = db.open_incident(monitor["id"], monitor["name"], detail,
                                           new_status, problem_type="domain")
        else:
            db.update_incident_severity(incident_id, new_status)
        subject, body, html = email_alerts.format_alert(monitor["name"], detail, new_status,
                                                        url=monitor["url"], ts=now)
        email_alerts.send_and_record(incident_id, subject, body, html)
    elif transition == "deescalate":
        db.update_incident_severity(incident_id, new_status)
    elif transition == "resolve":
        downtime = db.resolve_incident(monitor["id"], problem_type="domain")
        subject, body, html = email_alerts.format_recovered(
            f"{monitor['name']} (domain)", db.format_duration(downtime), url=monitor["url"], ts=now)
        email_alerts.send_and_record(incident_id, subject, body, html)
        incident_id = None

    # Record last_read_ts even on an unknown lookup so we don't re-hit RDAP every
    # tick for an unsupported TLD; the 24h gate above keys off it.
    db.set_metric_state(state_key, new_status, npz, npc, incident_id, now)


def check_all_domains(monitors):
    """One pass across monitors due for a domain check, one RDAP lookup per
    unique hostname, run concurrently so slow registries can't stall the
    scheduler loop (mirrors ssl_check.check_all_ssl)."""
    now = time.time()
    candidates = []
    for m in monitors:
        host = _hostname(m["url"])
        if not host or _is_ip(host):
            continue
        state = db.get_metric_state(f"domain:{m['id']}")
        if state is not None and now - state["last_read_ts"] < CHECK_INTERVAL_SEC:
            continue
        candidates.append((m, host))
    if not candidates:
        return

    hosts = {host for _m, host in candidates}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(hosts))) as pool:
        expiries = dict(zip(hosts, pool.map(get_domain_expiry, hosts)))

    for m, host in candidates:
        try:
            check_domain_for_monitor(m, now, expiry_result=expiries.get(host))
        except Exception as e:
            print(f"[domain_check] check failed for monitor {m['id']}: {e}")
