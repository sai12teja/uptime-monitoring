"""SSL certificate expiry checking (PRD §5.1/§7.1/§9/§15).

Date-based, not consecutive-failure based (§8) — a single read is
authoritative, so this reuses monitor_engine's zone state machine with
threshold=1 (commits immediately) instead of the 2-consecutive-reads
hysteresis server_health.py uses for noisy live metrics.
"""
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import db
import email_alerts
from server_health import evaluate_metric_zone

WARN_DAYS = 14                    # §15 example lead time, widest of "14/7/1"
CHECK_INTERVAL_SEC = 24 * 3600    # §8: "once every 12-24 hours"
CHECK_TIMEOUT_SEC = 10


def ssl_zone_for(days_remaining, error):
    """§7.1: an invalid/unreachable cert is immediately critical, distinct
    from the days-remaining countdown for a still-valid cert."""
    if error:
        return "critical"
    if days_remaining <= 0:
        return "critical"
    if days_remaining <= WARN_DAYS:
        return "warning"
    return "ok"


def parse_cert_expiry(not_after):
    """Parses ssl.SSLSocket.getpeercert()['notAfter'], e.g.
    'Jun  1 12:00:00 2027 GMT' (double space for single-digit days)."""
    return datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def get_cert_days_remaining(hostname, port=443, timeout=CHECK_TIMEOUT_SEC):
    """Returns (days_remaining: float|None, error: str|None). A verification
    failure (expired/invalid/hostname-mismatched cert) surfaces as `error`,
    not an exception — the default SSL context refuses to even complete the
    handshake for an already-expired cert, so that path must be handled
    here, not left to crash the caller."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
        expiry = parse_cert_expiry(cert["notAfter"])
        days = (expiry - datetime.now(timezone.utc)).total_seconds() / 86400
        return days, None
    except ssl.SSLCertVerificationError as e:
        return None, f"Invalid certificate: {e.verify_message}"
    except (socket.gaierror, socket.timeout, OSError) as e:
        return None, f"Unreachable: {e}"


def _detail_for(days_remaining, error):
    if error:
        return error
    if days_remaining <= 0:
        return f"SSL certificate expired {abs(int(days_remaining))} days ago"
    return f"SSL certificate expires in {int(days_remaining)} days"


def check_ssl_for_monitor(monitor, now):
    if not monitor["url"].startswith("https://"):
        return  # nothing to check

    state_key = f"ssl:{monitor['id']}"
    state = db.get_metric_state(state_key)
    if state is not None and now - state["last_read_ts"] < CHECK_INTERVAL_SEC:
        return  # not due yet (§8: every 12-24h, far less often than the HTTP check)

    hostname = urlparse(monitor["url"]).hostname
    days_remaining, error = get_cert_days_remaining(hostname)
    zone = ssl_zone_for(days_remaining, error)
    detail = _detail_for(days_remaining, error)

    status = state["status"] if state else "ok"
    incident_id = state["incident_id"] if state else None
    new_status, _, _, transition = evaluate_metric_zone(status, None, 0, zone, threshold=1)

    if transition in ("open", "escalate"):
        if transition == "open":
            incident_id = db.open_incident(monitor["id"], monitor["name"], detail,
                                            new_status, problem_type="ssl")
        else:
            db.update_incident_severity(incident_id, new_status)
        subject, body = email_alerts.format_alert(monitor["name"], detail, new_status,
                                                    url=monitor["url"], ts=now)
        email_alerts.send(subject, body)
        # Fresh time.time(), not `now` (this function's batch-tick timestamp,
        # captured before db.open_incident's own internal time.time() call) --
        # reusing `now` could predate the incident's `started`, sorting this
        # event BEFORE "opened" in the merged timeline.
        db.record_incident_event(incident_id, time.time(), "email_sent", subject)
    elif transition == "deescalate":
        db.update_incident_severity(incident_id, new_status)
    elif transition == "resolve":
        downtime_sec = db.resolve_incident(monitor["id"], problem_type="ssl")
        subject, body = email_alerts.format_recovered(f"{monitor['name']} (SSL)", db.format_duration(downtime_sec),
                                                       url=monitor["url"], ts=now)
        email_alerts.send(subject, body)
        db.record_incident_event(incident_id, time.time(), "email_sent", subject)
        incident_id = None

    db.set_metric_state(state_key, new_status, None, 0, incident_id, now)


def check_all_ssl(monitors):
    now = time.time()
    for monitor in monitors:
        try:
            check_ssl_for_monitor(monitor, now)
        except Exception as e:
            print(f"[ssl_check] check failed for monitor {monitor['id']}: {e}")
