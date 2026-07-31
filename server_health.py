"""Server health metrics (PRD §5.4) — CPU load, memory, disk, inodes, core
service status. Read straight off the box we run on (root access, §5.4), so
no agent and no new dependency: /proc, statvfs, and systemctl cover all of it.

Any metric the platform can't provide returns None, and the dashboard renders
"—" for it. That matters because this project is developed on Windows and
deployed to a Linux server: reporting 0% for "couldn't read it" would be a
silent lie of exactly the kind §13.2 forbids (disk 0% reads as healthy, when
in fact nothing was measured).
"""
import os
import shutil
import subprocess
import time

import db
import email_alerts

# PRD §19 open question — the real service list is unconfirmed. These are the
# §5.4 examples (web/db/mail/dns); correct them once ops confirms the stack.
SERVICES = ["nginx", "postgresql", "postfix", "named"]

DISK_PATH = "/" if os.name != "nt" else "C:\\"  # ponytail: root partition only,
                                                 # read /proc/mounts if per-partition
                                                 # reporting (§5.4) is actually needed

METRICS_TTL_SEC = 60  # §8: server resources change slowly (60-300s), and the
                      # dashboard polls every 15s — without this, every refresh
                      # from every open browser forks a systemctl per service.


def _parse_meminfo(text):
    """Percent of RAM in use, from /proc/meminfo contents.

    Uses MemAvailable (not MemFree) — free memory excludes reclaimable cache
    and would report a healthy box as nearly full.
    """
    fields = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if rest.strip():
            fields[key] = int(rest.split()[0])
    total, available = fields["MemTotal"], fields["MemAvailable"]
    return round((total - available) / total * 100, 1)


def _used_pct(used, free):
    """Percent used, matching what `df` reports.

    used/(used+free), not used/total: on Linux `total` includes root-reserved
    blocks, so used/total reads several points lower than df and would trip
    the §7.2 thresholds late. This hits 100% exactly when writes start failing.
    """
    denominator = used + free
    return round(used / denominator * 100, 1) if denominator else None


def cpu_load_pct():
    """1-minute load average as a percent of available cores.

    Can legitimately exceed 100% (that's a saturated box, not a bug) — the
    dashboard clamps the bar width but shows the true number.
    """
    try:
        return round(os.getloadavg()[0] / (os.cpu_count() or 1) * 100, 1)
    except (OSError, AttributeError):  # no getloadavg on Windows
        return None


def mem_used_pct():
    try:
        with open("/proc/meminfo") as f:
            return _parse_meminfo(f.read())
    except (OSError, KeyError, IndexError, ValueError, ZeroDivisionError):
        return None


def disk_used_pct(path=DISK_PATH):
    try:
        usage = shutil.disk_usage(path)
        return _used_pct(usage.used, usage.free)
    except OSError:
        return None


def inodes_used_pct(path=DISK_PATH):
    """Inode exhaustion fails writes with disk space still free (§5.4)."""
    try:
        st = os.statvfs(path)  # not available on Windows
    except (OSError, AttributeError):
        return None
    if not st.f_files:  # some filesystems (btrfs) report no inode limit
        return None
    return _used_pct(st.f_files - st.f_ffree, st.f_ffree)


def service_states(names=SERVICES):
    """{name: "up"|"down"} via systemctl. Empty dict where systemd isn't there."""
    states = {}
    for name in names:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return {}  # no systemd on this box — report nothing, not "all down"
        states[name] = "up" if result.stdout.strip() == "active" else "down"
    return states


# §7.2 two-level thresholds — the single source of truth. app.py's cards and
# this module's incident logic both read these, so an incident opens exactly
# when the card turns the matching color, never at a different number.
THRESHOLDS = {
    "cpu_pct": (80, 95),
    "mem_pct": (80, 95),
    "disk_pct": (90, 95),
    "inodes_pct": (90, 95),
}
METRIC_LABELS = {"cpu_pct": "CPU", "mem_pct": "Memory", "disk_pct": "Disk", "inodes_pct": "Inodes"}

ZONE_ORDER = {"ok": 0, "warning": 1, "critical": 2}


def zone_for(value, warn_at, crit_at):
    """Classifies a reading against the two §7.2 thresholds. None (metric
    couldn't be read) stays None — never alert on missing data."""
    if value is None:
        return None
    if value >= crit_at:
        return "critical"
    if value >= warn_at:
        return "warning"
    return "ok"


def evaluate_metric_zone(status, pending_zone, pending_count, zone, threshold=2):
    """Pure 3-level state transition (ok/warning/critical) for one server
    metric. Mirrors monitor_engine.evaluate_status's shape, but website/CRM
    only has up/down while server metrics alert at both §7.2 levels.

    status: last committed zone. pending_zone/pending_count: an in-progress
    transition awaiting confirmation. zone: this read's zone (None if
    unreadable this cycle — a transient read failure must not erase
    progress toward an already-accumulating transition).

    Returns (new_status, new_pending_zone, new_pending_count, transition)
    where transition is None, or one of "open"/"escalate"/"deescalate"/"resolve".
    """
    if zone is None:
        return status, pending_zone, pending_count, None

    if zone == status:
        return status, None, 0, None  # stable — clear any in-progress transition

    if zone == pending_zone:
        pending_count += 1
    else:
        pending_zone, pending_count = zone, 1

    if pending_count < threshold:
        return status, pending_zone, pending_count, None

    if zone == "ok":
        return "ok", None, 0, "resolve"
    if ZONE_ORDER[zone] > ZONE_ORDER[status]:
        return zone, None, 0, ("open" if status == "ok" else "escalate")
    return zone, None, 0, "deescalate"


_cache = {"ts": 0.0, "value": None}


def read_all():
    """All metrics, cached for METRICS_TTL_SEC.

    ponytail: unlocked cache — two threads can both compute on a cold entry,
    which just duplicates a cheap idempotent read. Add a lock only if the
    service checks ever get expensive enough that the duplicate work matters.
    """
    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["ts"] < METRICS_TTL_SEC:
        return _cache["value"]

    value = {
        "cpu_pct": cpu_load_pct(),
        "mem_pct": mem_used_pct(),
        "disk_pct": disk_used_pct(),
        "inodes_pct": inodes_used_pct(),
        "services": service_states(),
    }
    _cache.update(ts=now, value=value)
    return value


METRIC_PHRASING = {
    "cpu_pct": "load", "mem_pct": "used", "disk_pct": "full", "inodes_pct": "used",
}


def check_server_metrics():
    """One evaluation tick: read metrics, run each one through the §7.2/§9
    threshold state machine, open/escalate/deescalate/resolve incidents and
    send the matching §14 email. Call once per scheduler tick — internally
    a no-op unless read_all()'s cache has actually produced a fresh reading,
    so calling it more often than METRICS_TTL_SEC is harmless.
    """
    metrics = read_all()
    read_ts = _cache["ts"]

    for key, (warn_at, crit_at) in THRESHOLDS.items():
        state = db.get_metric_state(key)
        if state is not None and state["last_read_ts"] >= read_ts:
            continue  # already evaluated this reading

        status = state["status"] if state else "ok"
        pending_zone = state["pending_zone"] if state else None
        pending_count = state["pending_count"] if state else 0
        incident_id = state["incident_id"] if state else None

        zone = zone_for(metrics[key], warn_at, crit_at)
        new_status, new_pending_zone, new_pending_count, transition = evaluate_metric_zone(
            status, pending_zone, pending_count, zone
        )

        label = METRIC_LABELS[key]
        detail = f"{label} {metrics[key]}% {METRIC_PHRASING[key]}"

        if transition in ("open", "escalate"):
            if transition == "open":
                incident_id = db.open_server_incident("Server", detail, new_status)
            else:
                db.update_incident_severity(incident_id, new_status)
            subject, body, html_body = email_alerts.format_server_alert(detail, new_status, ts=read_ts)
            email_alerts.send(subject, body, html_body)
            db.record_incident_event(incident_id, time.time(), "email_sent", subject)
        elif transition == "deescalate":
            db.update_incident_severity(incident_id, new_status)
        elif transition == "resolve":
            downtime_sec = db.resolve_server_incident(incident_id)
            subject, body, html_body = email_alerts.format_recovered(f"Server ({label})",
                                                                       db.format_duration(downtime_sec),
                                                                       ts=read_ts)
            email_alerts.send(subject, body, html_body)
            db.record_incident_event(incident_id, time.time(), "email_sent", subject)
            incident_id = None

        db.set_metric_state(key, new_status, new_pending_zone, new_pending_count, incident_id, read_ts)


if __name__ == "__main__":
    for key, val in read_all().items():
        print(f"{key}: {val}")
