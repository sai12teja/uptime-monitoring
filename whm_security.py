"""Surface a WHM server's OWN security tooling into Rovix's one alert channel:
ImunifyAV/360 malware detections, ModSecurity web-attack blocks, and cPHulk
brute-force events. These are the authoritative "is this server being hacked"
signals -- WHM/Imunify already vetted them, so this just relays them as
incidents/emails alongside everything else, instead of someone logging into
WHM to find out.

Same shape as whm_health: reuses whm_health's WHM API client + _int_env, and
the shared incident/email machinery. Each source is DORMANT until its WHM/
plugin function name is configured (the exact functions/response shapes are a
confirmation from the WHM-admin dev, PRD §8-style -- not guessed). The alert
logic below is complete and tested; only the per-function response parse may
need a one-line tweak once a real shape is confirmed.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import db
import email_alerts
import whm_health

# WHM / plugin functions -- unset => that source is skipped (not guessed).
WHM_IMUNIFY_FUNCTION = os.environ.get("WHM_IMUNIFY_FUNCTION") or None
WHM_MODSEC_FUNCTION = os.environ.get("WHM_MODSEC_FUNCTION") or None
WHM_CPHULK_FUNCTION = os.environ.get("WHM_CPHULK_FUNCTION") or None

# Event-count thresholds at/above which we alert. Malware is 1 (ANY detection
# is critical); the noisy event streams get a count threshold, configurable.
WHM_MODSEC_THRESHOLD = whm_health._int_env("WHM_MODSEC_THRESHOLD", 50)
WHM_CPHULK_THRESHOLD = whm_health._int_env("WHM_CPHULK_THRESHOLD", 20)
CHECK_INTERVAL_SEC = whm_health._int_env("WHM_SECURITY_INTERVAL_SEC", 300)
MAX_WORKERS = 8

# (function, threshold, label, noun) per source.
_SOURCES = {
    "imunify": (WHM_IMUNIFY_FUNCTION, 1, "Malware (ImunifyAV/360)", "detection(s)"),
    "modsec": (WHM_MODSEC_FUNCTION, WHM_MODSEC_THRESHOLD, "ModSecurity", "blocked request(s)"),
    "cphulk": (WHM_CPHULK_FUNCTION, WHM_CPHULK_THRESHOLD, "cPHulk brute-force", "failed login(s)"),
}


def _count_from(payload):
    """Best-effort event count from a WHM/plugin response. Reads the common
    shapes -- a count-ish field or the length of the main list -- so it works
    across functions and is trivially corrected once a real shape is confirmed."""
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        for k in ("count", "num", "total", "num_detected", "detections", "events", "items"):
            v = data.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, list):
                return len(v)
        for v in data.values():  # fall back to the first list we find
            if isinstance(v, list):
                return len(v)
    if isinstance(data, list):
        return len(data)
    return 0


def _fetch_count(host, token, source):
    fn = _SOURCES[source][0]
    if not fn:
        return None  # source dormant until its function is configured
    try:
        return _count_from(whm_health._api(host, token, fn))
    except Exception as e:
        print(f"[whm_security] {host} {source} unavailable: {e}")
        return None


def _evaluate(host, source, count, now):
    """Open a critical incident + alert when events reach the source's
    threshold; resolve when they clear. Deduped per (server, source) via
    metric_state, so it alerts once per episode, not every poll."""
    if count is None:
        return
    _fn, threshold, label, noun = _SOURCES[source]
    state_key = f"whmsec:{host}:{source}"
    state = db.get_metric_state(state_key)
    active = state is not None and state["status"] == "active"
    incident_id = state["incident_id"] if state else None
    breached = count >= threshold

    if breached and not active:
        detail = f"{label}: {count} {noun}"
        incident_id = db.open_server_incident(host, detail, "critical")
        subject, body, html = email_alerts.format_alert(host, detail, "critical", ts=now)
        email_alerts.send_and_record(incident_id, subject, body, html)
        db.set_metric_state(state_key, "active", None, 0, incident_id, now)
    elif not breached and active:
        downtime = db.resolve_server_incident(incident_id)
        subject, body, html = email_alerts.format_recovered(
            f"{host} ({label})", db.format_duration(downtime), ts=now)
        email_alerts.send_and_record(incident_id, subject, body, html)
        db.set_metric_state(state_key, "ok", None, 0, None, now)
    else:
        db.set_metric_state(state_key, "active" if active else "ok", None, 0, incident_id, now)


def ingest_push(host, counts, now=None):
    """Push path (no WHM token needed): a trusted agent ON the WHM server reads
    its own security counts locally -- e.g. `imunify360-agent malware malicious
    list --json` -- and POSTs them here. Runs the SAME threshold/incident/alert/
    dedup/resolve logic as the pull path (_evaluate), so Rovix never needs any
    WHM access. Only the three known sources are accepted; counts must be
    non-negative ints. Returns (accepted_sources, error); validates all before
    evaluating any, so a bad field rejects the whole report rather than alerting
    on half of it. A source the caller omits is simply not touched this cycle."""
    if not host or not isinstance(host, str):
        return None, "host is required"
    if not isinstance(counts, dict) or not counts:
        return None, "counts must be a non-empty object"
    for source, value in counts.items():
        if source not in _SOURCES:
            return None, f"unknown source '{source}' (expected {sorted(_SOURCES)})"
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None, f"{source} count must be a non-negative integer"
    now = now or time.time()
    accepted = []
    for source, value in counts.items():
        _evaluate(host, source, value, now)
        accepted.append(source)
    return accepted, None


def _due(host, now):
    gate = db.get_metric_state(f"whmsec:{host}:_poll")
    return gate is None or now - gate["last_read_ts"] >= CHECK_INTERVAL_SEC


def check_all():
    """One security pass across every configured WHM server, poll-gated and
    with all (server, source) fetches run concurrently (bounded) so WHM latency
    can't stall the scheduler. No-op when no sources are configured."""
    if not any(fn for fn, *_ in _SOURCES.values()):
        return  # every source dormant -> nothing to do
    now = time.time()
    due = [(h, t) for (h, t) in whm_health.servers() if _due(h, now)]
    if not due:
        return

    tasks = [(h, t, s) for (h, t) in due for s in _SOURCES]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(tasks))) as pool:
        readings = list(pool.map(lambda t: (t, _fetch_count(*t)), tasks))

    for (host, _token, source), count in readings:
        try:
            _evaluate(host, source, count, now)
        except Exception as e:
            print(f"[whm_security] {host} {source} evaluation failed: {e}")

    for host, _token in due:
        try:
            db.set_metric_state(f"whmsec:{host}:_poll", "ok", None, 0, None, now)
        except Exception as e:
            print(f"[whm_security] {host} gate update failed: {e}")
