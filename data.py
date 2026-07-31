"""Real data layer, backed by db.py + monitor_engine.py (website/CRM checks,
§5.1/§5.2) and server_health.py (server metrics, §5.4).

Correlated-outage detection and feed-staleness are still stubbed below.
"""
import time

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


def add_target(name, url, target_type, keyword=None, port=None, interval_sec=60,
                retries=None, timeout_sec=None, http_method="GET",
                http_body=None, http_body_encoding="json", group_key=None, notify=True):
    return db.add_monitor(name, url, target_type, keyword=keyword, port=port, interval_sec=interval_sec,
                           retries=retries, timeout_sec=timeout_sec, http_method=http_method,
                           http_body=http_body, http_body_encoding=http_body_encoding, group_key=group_key,
                           notify=notify)


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
