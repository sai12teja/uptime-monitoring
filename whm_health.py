"""WHM/cPanel remote server resource health (PRD: Server Resource Health
Monitoring & Alerting).

server_health.py watches the box Rovix itself runs on; this watches the REMOTE
cPanel servers the client sites live on, via the WHM JSON API -- because a
remote server running low on disk can break the app while HTTP still returns
200, which the uptime check never flags (the incident this PRD exists for).

Deliberately thin: it only turns WHM readings into percentages/counts and feeds
them through server_health.evaluate_metric_zone -- the SAME warning/critical,
2-read-hysteresis state machine -- then opens/resolves incidents and sends
emails through the SAME code (email_alerts) as every other alert. Only the data
source is new. No new dependency: urllib, like the rest of the codebase.

Credentials: a scoped, read-only WHM API token per server, from the
environment, never committed (PRD §7). Metrics whose WHM function is still
unconfirmed (memory, mail queue -- PRD §8) stay dormant until configured,
rather than guessing an endpoint.
"""
import json
import os
import ssl as _ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import db
import email_alerts
from server_health import evaluate_metric_zone, zone_for


def _int_env(name, default):
    """Parse an int env var, falling back to the default on anything invalid --
    a typo in a threshold must never crash the app at import time."""
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (ValueError, TypeError):
        print(f"[whm_health] invalid {name}={os.environ.get(name)!r}, using {default}")
        return int(default)


WHM_PORT = _int_env("WHM_PORT", 2087)
WHM_USER = os.environ.get("WHM_USER", "root")
WHM_TIMEOUT_SEC = _int_env("WHM_TIMEOUT_SEC", 15)
# One WHM poll per server per interval -- don't hit the API every 5s tick.
CHECK_INTERVAL_SEC = _int_env("WHM_CHECK_INTERVAL_SEC", 60)
MAX_WHM_WORKERS = 8  # concurrency cap so WHM latency can't stall the scheduler loop
# load average -> percent needs a core count; the WHM API doesn't cleanly
# expose it, so it's configured. Unset -> CPU is skipped (PRD §8: don't guess).
WHM_CPU_CORES = _int_env("WHM_CPU_CORES", 0) or None
# WHM functions for the two PRD §8 open items -- unset -> that metric is skipped.
WHM_MEM_FUNCTION = os.environ.get("WHM_MEM_FUNCTION") or None
WHM_MAILQ_FUNCTION = os.environ.get("WHM_MAILQ_FUNCTION") or None

# (warning, critical) per metric -- PRD §6, configurable not inline.
THRESHOLDS = {
    "disk": (_int_env("WHM_DISK_WARN", 80), _int_env("WHM_DISK_CRIT", 90)),
    "cpu": (_int_env("WHM_CPU_WARN", 80), _int_env("WHM_CPU_CRIT", 90)),
    "mem": (_int_env("WHM_MEM_WARN", 80), _int_env("WHM_MEM_CRIT", 90)),
    # PRD §8 #2: raw count, no universal baseline -- placeholder until the PM
    # confirms against the measured typical queue size.
    "mailq": (_int_env("WHM_MAILQ_WARN", 500), _int_env("WHM_MAILQ_CRIT", 1000)),
}
_LABEL = {"disk": "Disk", "cpu": "CPU", "mem": "Memory", "mailq": "Mail queue"}
_PHRASING = {"disk": "full", "cpu": "load", "mem": "used", "mailq": "messages queued"}
_UNIT = {"disk": "%", "cpu": "%", "mem": "%", "mailq": ""}


def servers():
    """Configured WHM servers from WHM_SERVERS='host1:token1,host2:token2'.
    The token lives only in the environment, never in source (PRD §7).
    De-duplicated by host (last token wins) so a repeated host can't fight
    itself over the same per-metric state rows."""
    seen = {}
    for entry in os.environ.get("WHM_SERVERS", "").split(","):
        host, _, token = entry.strip().partition(":")
        if host.strip() and token.strip():
            seen[host.strip()] = token.strip()
    return list(seen.items())


def _api(host, token, function, params=None):
    """One WHM API 1 call -> parsed JSON dict. Raises on any transport/HTTP/
    parse error OR a WHM-level failure (bad token/ACL, unknown function come
    back as HTTP 200 with metadata.result == 0), so the reason is logged rather
    than silently swallowed. Callers treat any raise as 'metric unavailable'."""
    query = urllib.parse.urlencode({**(params or {}), "api.version": 1})
    url = f"https://{host}:{WHM_PORT}/json-api/{function}?{query}"
    req = urllib.request.Request(url, headers={"Authorization": f"whm {WHM_USER}:{token}"})
    # cPanel control ports (:2087) commonly use a self-signed cert; the token is
    # the auth and we only READ. Verification stays ON by default; set
    # WHM_INSECURE_TLS=1 to accept a self-signed control-port cert.
    ctx = (_ssl._create_unverified_context()
           if os.environ.get("WHM_INSECURE_TLS", "").lower() in ("1", "true", "yes") else None)
    with urllib.request.urlopen(req, timeout=WHM_TIMEOUT_SEC, context=ctx) as resp:
        payload = json.loads(resp.read())
    meta = payload.get("metadata")
    if isinstance(meta, dict) and str(meta.get("result")) == "0":
        raise RuntimeError(f"WHM {function} failed: {meta.get('reason', 'unknown')}")
    return payload


def _safe(host, metric, fn):
    """Run one metric fetch; any failure -> None (skipped, logged), so one bad
    metric or server never breaks the others or the scheduler tick (§FR8)."""
    try:
        return fn()
    except Exception as e:
        print(f"[whm_health] {host} {metric} unavailable: {e}")
        return None


def _disk_pct(host, token):
    """Highest partition usage % from WHM getdiskusage (the confirmed function,
    PRD §11) -- a single full partition is enough to break writes. Tolerates the
    API returning one partition as a dict or many as a list."""
    partitions = _api(host, token, "getdiskusage").get("data", {}).get("partition", [])
    if isinstance(partitions, dict):
        partitions = [partitions]
    pcts = []
    for p in partitions if isinstance(partitions, list) else []:
        if not isinstance(p, dict):
            continue
        raw = p.get("percentage", p.get("disk_used_percentage"))
        try:
            if raw is not None:
                pcts.append(float(str(raw).rstrip("%")))
        except ValueError:
            continue
    return max(pcts) if pcts else None


def _cpu_pct(host, token):
    """1-min load average as a % of cores. WHM systemloadavg gives the load;
    cores come from config (WHM_CPU_CORES) -- without it we can't make a %, so
    return None rather than guess."""
    if not WHM_CPU_CORES:
        return None
    data = _api(host, token, "systemloadavg").get("data", {})
    one = data.get("one")
    return round(float(one) / WHM_CPU_CORES * 100, 1) if one is not None else None


def _mem_pct(host, token):
    """Memory % -- no clean standard WHM function (PRD §8, flag to PM). Stays
    dormant until WHM_MEM_FUNCTION names a confirmed one returning used/total."""
    if not WHM_MEM_FUNCTION:
        return None
    data = _api(host, token, WHM_MEM_FUNCTION).get("data", {})
    total, used = data.get("total"), data.get("used")
    if total in (None, 0) or used is None:
        return None
    return round(float(used) / float(total) * 100, 1)


def _mailq_count(host, token):
    """Mail queue size -- WHM function unconfirmed (PRD §8 #1). Dormant until
    WHM_MAILQ_FUNCTION names it; expected to return an integer count."""
    if not WHM_MAILQ_FUNCTION:
        return None
    data = _api(host, token, WHM_MAILQ_FUNCTION).get("data", {})
    count = data.get("count", data.get("queue_count"))
    return int(count) if count is not None else None


_FETCHERS = {"disk": _disk_pct, "cpu": _cpu_pct, "mem": _mem_pct, "mailq": _mailq_count}


def _fetch_one(host, token, key):
    return _safe(host, key, lambda: _FETCHERS[key](host, token))


def fetch_metrics(host, token):
    """{metric_key: value|None} for one server (convenience / __main__ debug;
    check_all fetches concurrently across all servers)."""
    return {key: _fetch_one(host, token, key) for key in _FETCHERS}


def _evaluate(host, key, value, warn_at, crit_at, now):
    """One metric's threshold state machine for one server -- a per-(server,
    metric) copy of server_health.check_server_metrics's inner block, reusing
    evaluate_metric_zone and the same incident/email code. value None (metric
    unavailable this cycle) -> no-op; never alert on missing data."""
    if value is None:
        return
    state_key = f"whm:{host}:{key}"
    state = db.get_metric_state(state_key)
    status = state["status"] if state else "ok"
    pending_zone = state["pending_zone"] if state else None
    pending_count = state["pending_count"] if state else 0
    incident_id = state["incident_id"] if state else None

    zone = zone_for(value, warn_at, crit_at)
    new_status, new_pending_zone, new_pending_count, transition = evaluate_metric_zone(
        status, pending_zone, pending_count, zone)

    # "<Host> — Disk 85% full": server, metric, value all in the alert (§FR6);
    # severity (warning/critical) is the subject tag format_alert adds.
    detail = f"{_LABEL[key]} {value}{_UNIT[key]} {_PHRASING[key]}"

    if transition in ("open", "escalate"):
        if transition == "open":
            incident_id = db.open_server_incident(host, detail, new_status)
        else:
            db.update_incident_severity(incident_id, new_status)
        subject, body, html_body = email_alerts.format_alert(host, detail, new_status, ts=now)
        email_alerts.send_and_record(incident_id, subject, body, html_body)
    elif transition == "deescalate":
        db.update_incident_severity(incident_id, new_status)
    elif transition == "resolve":
        downtime = db.resolve_server_incident(incident_id)
        subject, body, html_body = email_alerts.format_recovered(
            f"{host} ({_LABEL[key]})", db.format_duration(downtime), ts=now)
        email_alerts.send_and_record(incident_id, subject, body, html_body)
        incident_id = None

    db.set_metric_state(state_key, new_status, new_pending_zone, new_pending_count, incident_id, now)


def _due(host, now):
    gate = db.get_metric_state(f"whm:{host}:_poll")
    return gate is None or now - gate["last_read_ts"] >= CHECK_INTERVAL_SEC


def check_all():
    """One tick across every configured WHM server, each poll-gated to
    CHECK_INTERVAL_SEC. Every (server, metric) reading is fetched CONCURRENTLY
    in a bounded pool -- so slow or hung WHM calls (each with its own timeout)
    can't stall the scheduler loop past roughly one timeout, no matter how many
    servers/metrics there are. Evaluation (fast, DB-only) then runs serially, so
    there are no cross-write races. Additive and fully guarded -> can never slow
    or break the existing uptime checks (§FR8)."""
    now = time.time()
    due = [(host, token) for (host, token) in servers() if _due(host, now)]
    if not due:
        return

    tasks = [(host, token, key) for (host, token) in due for key in _FETCHERS]
    with ThreadPoolExecutor(max_workers=min(MAX_WHM_WORKERS, len(tasks))) as pool:
        readings = list(pool.map(lambda t: (t, _fetch_one(*t)), tasks))

    for (host, _token, key), value in readings:
        warn_at, crit_at = THRESHOLDS[key]
        try:
            _evaluate(host, key, value, warn_at, crit_at, now)
        except Exception as e:
            print(f"[whm_health] {host} {key} evaluation failed: {e}")

    # Advance each server's poll gate even if some metric errored, so a
    # persistently failing server is retried on the normal cadence, not hammered
    # on every 5s tick.
    for host, _token in due:
        try:
            db.set_metric_state(f"whm:{host}:_poll", "ok", None, 0, None, now)
        except Exception as e:
            print(f"[whm_health] {host} gate update failed: {e}")
