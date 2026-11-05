"""Weekly uptime / SLA digest.

The live dashboard shows "now"; this is the periodic "how did the week go"
summary -- per-monitor uptime %, incident count, and total downtime over a
window -- delivered through the same multi-channel send() (email + webhooks).
Pure reuse of data already stored (checks via db.uptime_since, incidents via
db.list_incidents); no new tables.

Opt-in (WEEKLY_DIGEST=1) so it never surprise-emails; gated to DIGEST_INTERVAL_SEC
via the same metric_state last-run primitive the retention purge uses.
"""
import os
import time

import db
import email_alerts


def _int_env(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except (ValueError, TypeError):
        print(f"[digest] invalid {name}={os.environ.get(name)!r}, using {default}")
        return int(default)


WINDOW_DAYS = _int_env("DIGEST_WINDOW_DAYS", 7)
DIGEST_INTERVAL_SEC = _int_env("DIGEST_INTERVAL_SEC", 7 * 86400)


def _enabled():
    return os.environ.get("WEEKLY_DIGEST", "").lower() in ("1", "true", "yes")


def build_digest(now, window_days=None):
    """(subject, body) summarising the window. Pure -- no send, so it's
    testable on its own. Sorted worst-uptime-first so the problems lead."""
    window_days = window_days or WINDOW_DAYS
    since = now - window_days * 86400
    rows = []
    total_incidents = 0
    for m in db.list_monitors():
        total, ok = db.uptime_since(m["id"], since)
        pct = (ok / total * 100) if total else None
        incidents = db.list_incidents(monitor_id=m["id"], since=since)
        total_incidents += len(incidents)
        downtime = sum((i["resolved"] or now) - i["started"] for i in incidents)
        rows.append((pct, m["name"], len(incidents), downtime))

    # Worst uptime first; monitors with no data (pct None) sort last.
    rows.sort(key=lambda r: (r[0] is not None, r[0] if r[0] is not None else 0))
    lines = []
    for pct, name, n_inc, downtime in rows:
        pct_str = f"{pct:6.2f}%" if pct is not None else "    — "
        lines.append(f"{name[:34]:<34} {pct_str}   incidents {n_inc:>3}   down {db.format_duration(downtime)}")

    subject = f"[DIGEST] Rovix {window_days}-day summary — {len(rows)} monitors, {total_incidents} incidents"
    body = (f"Rovix monitoring summary — last {window_days} days\n"
            f"{len(rows)} monitors, {total_incidents} incidents total\n\n"
            + ("\n".join(lines) if lines else "No monitors configured.") + "\n")
    return subject, body


def maybe_send(now):
    """Send the digest if enabled and due. Called every scheduler tick; a no-op
    otherwise. First run after enabling sends immediately, then every interval."""
    if not _enabled():
        return False
    state = db.get_metric_state("weekly_digest")
    if state is not None and now - state["last_read_ts"] < DIGEST_INTERVAL_SEC:
        return False
    subject, body = build_digest(now)
    email_alerts.send(subject, body)
    db.set_metric_state("weekly_digest", "ok", None, 0, None, now)
    return True
