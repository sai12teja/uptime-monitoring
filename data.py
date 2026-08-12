"""Real data layer, backed by db.py + monitor_engine.py (website/CRM checks,
§5.1/§5.2) and server_health.py (server metrics, §5.4).

Correlated-outage detection and feed-staleness are still stubbed below.
"""
import time
from urllib.parse import urlparse

import db
import server_health

STALE_LAST_UPDATED = "3 minutes"  # unused until the feed-staleness task lands


def _display_status(row, now):
    if row["last_checked_at"] is None:
        return "awaiting"
    if row["status"] != "down" and now - row["last_checked_at"] > 2 * row["interval_sec"]:
        return "overdue"
    return row["status"]


def _target_dict(row, now):
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "status": _display_status(row, now),
        "last_checked_sec": int(now - row["last_checked_at"]) if row["last_checked_at"] else None,
        "response_ms": row["last_response_ms"],
        "group_key": row["group_key"],
        "subrow_label": row["subrow_label"],
        # Exposed for the card's site icon; no other UI surface shows the
        # raw URL. favicon_url is the icon the site declares in its HTML
        # (resolved once at add time / by the backfill), NULL until then.
        "url": row["url"],
        "favicon_url": row["favicon_url"],
    }


def get_targets():
    now = time.time()
    return [_target_dict(row, now) for row in db.list_monitors()]


def get_server_metrics():
    return server_health.read_all()


def is_correlated_outage():
    state = db.get_metric_state("correlated_outage")
    return state is not None and state["status"] == "active"


def is_stale():
    return False


def is_incident_table_error():
    return False


def summary_counts():
    targets = get_targets()
    up = sum(1 for t in targets if t["status"] == "up")
    down = sum(1 for t in targets if t["status"] == "down")
    return {"up": up, "down": down, "total": len(targets)}


def site_health_counts():
    """Health counted by SITE, not by individual check.

    summary_counts() counts monitor rows -- 216 checks across 42 sites -- so
    "14 down" looked wrong next to a 42-card grid and gave no hint whether
    that meant 14 dead sites or one broken page on a few. A site is:
      down     - every one of its checks is failing (usually DNS: the domain
                 doesn't resolve, so nothing above it can work)
      degraded - some checks fail, some pass (typically one broken page on a
                 server that is otherwise healthy)
      up       - everything passing
    Grouping mirrors the grid exactly (group_key), so the two always agree.
    """
    groups = {}
    for target in get_targets():
        groups.setdefault(target["group_key"] or f"solo-{target['id']}", []).append(target)

    up = degraded = down = 0
    for members in groups.values():
        failing = sum(1 for m in members if m["status"] != "up")
        if failing == 0:
            up += 1
        elif failing == len(members):
            down += 1
        else:
            degraded += 1
    return {"up": up, "degraded": degraded, "down": down, "total": len(groups)}


def monitors_by_type():
    """Active-monitor count per check type, e.g. {"website": 10, "tcp": 3}."""
    counts = {}
    for row in db.list_monitors():
        counts[row["type"]] = counts.get(row["type"], 0) + 1
    return counts


def incidents_in_window(hours):
    """Count of incidents that STARTED within the last `hours` hours."""
    since = time.time() - hours * 3600
    return len(db.list_incidents(since=since))


def avg_incident_duration():
    """Average resolution time (seconds) across all resolved incidents, or
    None if none have resolved yet -- never a fabricated 0."""
    resolved = db.list_incidents(status="resolved")
    if not resolved:
        return None
    return sum(r["resolved"] - r["started"] for r in resolved) / len(resolved)


def _incident_dict(inc):
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(inc["started"]))
    if inc["resolved"] is None:
        resolved, duration = None, "ongoing"
    else:
        resolved = time.strftime("%Y-%m-%d %H:%M", time.localtime(inc["resolved"]))
        duration = db.format_duration(inc["resolved"] - inc["started"])
    return {
        "id": inc["id"],
        "target": inc["target_name"],
        "problem": inc["problem"],
        "severity": inc["severity"],
        "started": started,
        "resolved": resolved,
        "duration": duration,
        "acknowledged_by": inc["acknowledged_by"],
    }


def get_incidents(status=None, target_id=None, since=None, until=None):
    rows = db.list_incidents(status=status, monitor_id=target_id, since=since, until=until)
    return [_incident_dict(inc) for inc in rows]


def get_incident_detail(incident_id):
    inc = db.get_incident(incident_id)
    if inc is None:
        return None

    events = [{"ts": inc["started"], "kind": "opened", "detail": inc["problem"]}]
    events += [{"ts": e["ts"], "kind": e["event_type"], "detail": e["detail"]}
               for e in db.list_incident_events(incident_id)]
    events.sort(key=lambda e: e["ts"])
    timeline = [{**e, "ts": time.strftime("%H:%M:%S", time.localtime(e["ts"]))} for e in events]

    detail = _incident_dict(inc)
    detail["target_id"] = inc["monitor_id"]
    detail["problem_type"] = inc["problem_type"]
    detail["timeline"] = timeline
    return detail


def acknowledge_incident(incident_id, who):
    db.acknowledge_incident(incident_id, who)


def add_target(name, url, target_type, keyword=None, port=None, interval_sec=db.DEFAULT_CHECK_INTERVAL_SEC,
                retries=None, timeout_sec=None, http_method="GET",
                http_body=None, http_body_encoding="json", group_key=None, notify=True,
                subrow_label=None, favicon_url=None):
    return db.add_monitor(name, url, target_type, keyword=keyword, port=port, interval_sec=interval_sec,
                           retries=retries, timeout_sec=timeout_sec, http_method=http_method,
                           http_body=http_body, http_body_encoding=http_body_encoding, group_key=group_key,
                           notify=notify, subrow_label=subrow_label, favicon_url=favicon_url)


def backfill_favicons():
    """Resolves the declared icon for every active monitor that has no
    favicon_url yet. Called once at startup in a background thread (never
    on the request path -- it does one HTTP fetch per distinct host).

    Results are cached per host so a site with 10 page monitors costs one
    fetch, not ten. A host that resolves to nothing is remembered too, so
    it isn't retried on every restart within the same run."""
    import page_discovery  # local import: avoids a cycle at module load

    resolved = {}
    for row in db.monitors_missing_favicon():
        host_key = urlparse(row["url"] if "://" in row["url"] else f"https://{row['url']}").netloc
        if not host_key:
            continue
        if host_key not in resolved:
            try:
                resolved[host_key] = page_discovery.discover_favicon(row["url"])
            except Exception:
                resolved[host_key] = None  # never let a bad site break startup
        if resolved[host_key]:
            db.set_favicon_url(row["id"], resolved[host_key])
    return sum(1 for v in resolved.values() if v)


def update_target(target_id, name, url, keyword, interval_sec, port=None,
                   retries=None, timeout_sec=None, http_method="GET",
                   http_body=None, http_body_encoding="json", notify=True):
    db.update_monitor(target_id, name, url, keyword, interval_sec, port=port,
                       retries=retries, timeout_sec=timeout_sec, http_method=http_method,
                       http_body=http_body, http_body_encoding=http_body_encoding, notify=notify)


def delete_target(target_id):
    db.deactivate_monitor(target_id)


def get_target_detail(target_id):
    row = db.get_monitor(target_id)
    if row is None:
        return None
    now = time.time()

    checks = db.list_checks(target_id, limit=10)
    timeline = [
        (time.strftime("%H:%M", time.localtime(c["ts"])), "ok" if c["ok"] else "fail", c["detail"])
        for c in checks
    ]
    checks_table = [
        (time.strftime("%H:%M:%S", time.localtime(c["ts"])), "OK" if c["ok"] else "FAIL", c["detail"])
        for c in checks
    ]

    total = len(checks)
    ok_count = sum(1 for c in checks if c["ok"])
    uptime = f"{ok_count / total * 100:.2f}%" if total else "—"
    ok_ms = [c["response_ms"] for c in checks if c["ok"] and c["response_ms"] is not None]
    avg_ms = f"{sum(ok_ms) // len(ok_ms)}ms" if ok_ms else "—"

    return {
        "target": _target_dict(row, now),
        "stats": {"uptime_30d": uptime, "avg_ms": avg_ms},
        "timeline": timeline,
        "checks": checks_table,
        "push_token": row["push_token"],
    }
