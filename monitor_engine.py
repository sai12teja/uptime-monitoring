"""Real website/CRM HTTP checks (PRD §5.1/§5.2) + flap-protection state
machine (§10: 3 consecutive fails -> DOWN, 2 consecutive successes -> UP)
+ a background scheduler that ticks the checks that are due.

Server health (CPU/RAM/disk) and correlated-outage detection are separate
tasks — this module only owns website/CRM reachability.
"""
import ipaddress
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

import db
import email_alerts
import server_health
import ssl_check

CHECK_TIMEOUT_SEC = 10  # ponytail: flat timeout for every monitor, add a per-monitor
                         # column if some targets genuinely need a different budget
TICK_SEC = 5            # ponytail: poll-loop granularity, not per-monitor cron precision
                         # (fine at 60s+ check intervals) — swap for APScheduler if that changes
MAX_WORKERS = 10         # PRD §18 NFR: "comfortably handle dozens of targets" needs concurrency

MIN_MONITORS_FOR_CORRELATION = 3  # 1-of-1 or 2-of-2 down isn't evidence of a shared cause

CHECKS_RETENTION_DAYS = int(os.environ.get("CHECKS_RETENTION_DAYS", "90"))  # PRD §16
PURGE_INTERVAL_SEC = 24 * 3600  # once a day is plenty at a 90-day-scale window

# Alert digest debounce: a shared host going down takes several monitors
# with it, but each monitor's own check only reaches its fail threshold on
# its own staggered schedule -- so the resulting "opened" events land
# several ticks apart, not all in one. Queuing them and flushing as ONE
# email once this many seconds pass since the first queued item is what
# turns that into one digest instead of one email per affected site.
# 0 collapses to "flush immediately" (every existing single-event test
# relies on this to keep working unchanged).
ALERT_BATCH_WINDOW_SEC = int(os.environ.get("ALERT_BATCH_WINDOW_SEC", "60"))


def compute_is_correlated_outage(monitor_statuses, min_monitors=MIN_MONITORS_FOR_CORRELATION):
    """PRD §9: every hosted site failing at once implies a shared local
    cause (nginx/DNS/network on this host), not many unrelated outages."""
    if len(monitor_statuses) < min_monitors:
        return False
    return all(status == "down" for status in monitor_statuses)


def _encode_http_body(body, encoding):
    """Returns (data_bytes, content_type) for a non-GET request body,
    per the Body Encoding chosen in the Add Monitor form."""
    if encoding == "form":
        pairs = dict(line.split("=", 1) for line in body.splitlines() if line.strip())
        return urllib.parse.urlencode(pairs).encode(), "application/x-www-form-urlencoded"
    if encoding == "text":
        return body.encode(), "text/plain"
    return body.encode(), "application/json"  # default: json


def _is_blocked_host(hostname):
    """SSRF/internal-recon guard (security review H2): a monitor is a
    standing invitation to fetch/connect to whatever hostname a user
    types, on a schedule, forever. Blocks link-local/RFC1918-private/
    reserved targets by default -- cloud metadata endpoints
    (169.254.169.254) and other hosts on the local network that the
    monitoring server can reach but a monitor author normally shouldn't
    be able to probe.

    Loopback (127.0.0.0/8, ::1) is deliberately exempt: monitoring a
    service on the same box the scheduler runs on is a legitimate,
    already-tested use case (this repo's own test suite spins up real
    local HTTP/TCP servers on 127.0.0.1), and it isn't the network-pivot
    risk the rest of this guard exists for -- code that can create a
    monitor already runs in this same process.

    Checked here at connect time, not at monitor creation -- resolving
    fresh on every tick is what actually stops DNS rebinding (a hostname
    that resolved to a public IP at creation time but a private one now).
    Unresolvable hostnames return False so the real check reports the
    DNS failure instead of a misleading "blocked" detail.
    """
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        return False
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private and not ip.is_loopback:
            return True
    return False


def do_http_check(monitor):
    """Returns (ok: bool, response_ms: int, detail: str)."""
    start = time.monotonic()
    hostname = urllib.parse.urlparse(monitor["url"]).hostname
    if hostname and _is_blocked_host(hostname):
        return False, 0, "Blocked: target resolves to a private/internal address"
    timeout = monitor["timeout_sec"] or CHECK_TIMEOUT_SEC
    headers = {"User-Agent": "RovixMonitor/1.0"}
    method = monitor["http_method"] or "GET"
    data = None
    if method != "GET" and monitor["http_body"]:
        data, content_type = _encode_http_body(monitor["http_body"], monitor["http_body_encoding"] or "json")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(monitor["url"], data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(65536)
            response_ms = int((time.monotonic() - start) * 1000)
            keyword = monitor["keyword"]
            if keyword:
                if resp.headers.get("Content-Encoding") == "gzip":
                    # urllib does not auto-decompress. zlib's incremental
                    # decompressor (not gzip.decompress) because our read is
                    # capped at 64KB — a truncated stream must yield the bytes
                    # it can, not raise, or a big page is a false DOWN.
                    try:
                        body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body)
                    except zlib.error:
                        return False, response_ms, f"HTTP {resp.status} but response was not readable"
                if keyword.encode() not in body:
                    return False, response_ms, f"HTTP {resp.status} but expected content missing"
            if resp.status >= 400:
                return False, response_ms, f"HTTP {resp.status}"
            return True, response_ms, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, int((time.monotonic() - start) * 1000), f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, int((time.monotonic() - start) * 1000), f"Unreachable: {e.reason}"
    except TimeoutError:
        return False, int((time.monotonic() - start) * 1000), f"Timeout after {timeout}s"
    except Exception as e:
        return False, int((time.monotonic() - start) * 1000), f"Error: {e}"


def do_tcp_check(monitor):
    """Returns (ok, response_ms, detail) -- same contract as do_http_check."""
    start = time.monotonic()
    if _is_blocked_host(monitor["url"]):
        return False, 0, "Blocked: target resolves to a private/internal address"
    timeout = monitor["timeout_sec"] or CHECK_TIMEOUT_SEC
    try:
        with socket.create_connection((monitor["url"], monitor["port"]), timeout=timeout):
            return True, int((time.monotonic() - start) * 1000), f"Connected to port {monitor['port']}"
    except TimeoutError:
        return False, int((time.monotonic() - start) * 1000), f"Timeout after {timeout}s"
    except OSError as e:
        return False, int((time.monotonic() - start) * 1000), f"Unreachable: {e.strerror or e}"


def do_dns_check(monitor):
    """Returns (ok, response_ms, detail) -- same contract as do_http_check.
    No record-type selection (A/AAAA only, whatever getaddrinfo resolves) --
    see the design doc for why a dns_record_type column was dropped.
    """
    start = time.monotonic()
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(monitor["url"], None)}
    except socket.gaierror as e:
        return False, int((time.monotonic() - start) * 1000), f"DNS resolution failed: {e.strerror or e}"
    response_ms = int((time.monotonic() - start) * 1000)
    expected = monitor["keyword"]
    if expected and expected not in addrs:
        return False, response_ms, f"Resolved to {', '.join(sorted(addrs))}, expected {expected}"
    return True, response_ms, f"Resolved to {', '.join(sorted(addrs))}"


def evaluate_status(status, fails, oks, ok, fail_threshold=3, ok_threshold=2):
    """Pure state transition, PRD §10 (defaults: 3 fails / 2 successes).
    Push monitors pass fail_threshold=1, ok_threshold=1 -- a missed or
    received check-in is an unambiguous signal, unlike a network blip.
    Returns (new_status, new_fails, new_oks, opened_incident, resolved_incident).
    """
    if ok:
        fails, oks = 0, oks + 1
        if status == "down":
            if oks >= ok_threshold:
                return "up", fails, oks, False, True
            return "down", fails, oks, False, False
        return "up", fails, oks, False, False

    fails, oks = fails + 1, 0
    if status != "down" and fails >= fail_threshold:
        return "down", fails, oks, True, False
    return status, fails, oks, False, False


def _record_transition(monitor, now, ok, response_ms, detail, fail_threshold=3, ok_threshold=2):
    """Shared by _check_one (active checks) and the push-ping endpoint
    (passive checks) -- everything from "here's an ok/fail result" onward
    is identical regardless of how that result was obtained. Returns a
    pending email event ("opened"/"resolved", name, detail, incident_id)
    or None, exactly like _check_one always has.
    """
    db.record_check(monitor["id"], now, ok, response_ms, detail)
    new_status, fails, oks, opened, resolved = evaluate_status(
        monitor["status"], monitor["consecutive_fails"], monitor["consecutive_oks"], ok,
        fail_threshold=fail_threshold, ok_threshold=ok_threshold,
    )
    db.update_monitor_state(monitor["id"], new_status, fails, oks, now, response_ms)
    if opened:
        incident_id = db.open_incident(monitor["id"], monitor["name"], detail)
        # Use the incident's own `started` (set inside open_incident via a
        # fresh time.time() call) rather than this function's `now` -- `now`
        # is captured before the check runs and can be seconds earlier on a
        # slow/timed-out check, which would sort this event BEFORE "opened"
        # in the merged timeline.
        started_ts = db.get_incident(incident_id)["started"]
        db.record_incident_event(incident_id, started_ts, "check_failure", detail)
        return ("opened", monitor["name"], detail, incident_id)
    if resolved:
        open_row = db.get_open_incident(monitor["id"], problem_type="http")
        incident_id = open_row["id"] if open_row else None
        downtime_sec = db.resolve_incident(monitor["id"])
        return ("resolved", monitor["name"], db.format_duration(downtime_sec), incident_id)
    if not ok and new_status == "down":
        # Still down, not a fresh transition -- a continuing failure on
        # an already-open incident (PRD §12: incident_events logs "check
        # failures"). No email-worthy event, so returns None as before.
        open_row = db.get_open_incident(monitor["id"], problem_type="http")
        if open_row:
            db.record_incident_event(open_row["id"], now, "check_failure", detail)
    return None


def record_push_ping(monitor, now):
    """A real check-in arriving at /push/<token> (the route looks the
    monitor up by token before calling this). Same 1-ping-recovers
    threshold as the missed-check-in path in _check_one's push branch.
    """
    return _record_transition(monitor, now, True, None, "Check-in received",
                               fail_threshold=1, ok_threshold=1)


def _check_one(monitor, now):
    """Runs the check and commits DB state unconditionally (so each
    target's own incident history / uptime% stays accurate regardless of
    what else is happening this tick). Returns a pending email event
    ("opened"/"resolved", name, detail, incident_id) instead of sending it —
    whether this fires individually or gets folded into one consolidated
    alert is decided after the whole batch settles, by handle_tick_events.
    incident_id lets handle_tick_events log the email as an incident_event
    (PRD §12 gap 7) against the right incident.
    """
    try:
        if monitor["type"] == "push":
            if monitor["last_checked_at"] is None:
                return None  # awaiting its first-ever check-in, not a failure
            detail = f"No check-in received within {monitor['interval_sec']}s"
            return _record_transition(monitor, now, False, None, detail,
                                       fail_threshold=1, ok_threshold=1)
        # Built fresh each call (not a module-level constant) so that
        # test mocks patching monitor_engine.do_http_check etc. by name
        # are picked up -- a dict built once at import time would freeze
        # in the original, unpatched function objects.
        check_fn = {"website": do_http_check, "crm": do_http_check,
                    "tcp": do_tcp_check, "dns": do_dns_check}[monitor["type"]]
        ok, response_ms, detail = check_fn(monitor)
        # retries=0 means Kuma's "down immediately on first fail" (fail_threshold=1);
        # unset (None) reproduces today's global default (fail_threshold=3).
        retries = monitor["retries"] if monitor["retries"] is not None else 2
        return _record_transition(monitor, now, ok, response_ms, detail, fail_threshold=retries + 1)
    except Exception as e:
        print(f"[monitor_engine] check failed for monitor {monitor['id']}: {e}")
    return None


_TYPE_LABEL = {"website": "Website", "crm": "CRM", "tcp": "TCP", "dns": "DNS", "push": "Push"}


def _alert_context(monitor):
    """Real per-monitor detail for the premium alert email (email_alerts.
    format_down/format_recovered) -- the fail threshold from actual config
    and recent checks from the actual checks table, nothing invented.
    None when there's no single monitor to draw from (a server-level or
    correlated-outage incident), so those callers fall back to the plainer
    card in email_alerts instead of rendering with blank/fake stats.
    """
    if monitor is None:
        return None
    retries = monitor["retries"] if monitor["retries"] is not None else 2
    fail_threshold = 1 if monitor["type"] == "push" else retries + 1
    recent = list(reversed(db.list_checks(monitor["id"], limit=4)))
    return {
        "fail_threshold": fail_threshold,
        "response_ms": monitor["last_response_ms"],
        "check_type": _TYPE_LABEL.get(monitor["type"], monitor["type"].title()),
        "interval_sec": monitor["interval_sec"],
        "checks": [(c["ts"], bool(c["ok"]), c["detail"]) for c in recent],
    }


def _incident_context(incident_id):
    """(target url, opened-at ts, notify-enabled, alert context) for an
    incident, for the alert body and the per-monitor notify toggle.

    Looked up here rather than threaded through the event tuple: alerts are
    rare, so a few extra reads cost nothing, and every existing producer of
    that tuple stays untouched. Returns (None, None, True, None) when the
    incident or monitor can't be resolved (e.g. a resolve with no open
    incident row), which just yields a shorter email and defaults to notifying.
    """
    if incident_id is None:
        return None, None, True, None
    inc = db.get_incident(incident_id)
    if inc is None:
        return None, None, True, None
    monitor = db.get_monitor(inc["monitor_id"]) if inc["monitor_id"] is not None else None
    notify = bool(monitor["notify"]) if monitor is not None else True
    return (monitor["url"] if monitor else None), inc["started"], notify, _alert_context(monitor)


def handle_tick_events(events):
    """Decides, once a tick's checks have all committed, whether this
    tick's opened/resolved transitions are 15 unrelated incidents or one
    shared-cause outage (PRD §9/§15) — and sends exactly the alert(s) that
    situation calls for.
    """
    # Runs first and unconditionally (even on the correlated-outage early
    # returns below) so a pending digest batch can't get stuck waiting for
    # a tick that never reaches the per-event loop again.
    _flush_expired_batches(time.time())

    monitors = db.list_monitors()
    total = len(monitors)
    down_count = sum(1 for m in monitors if m["status"] == "down")
    is_correlated_now = compute_is_correlated_outage([m["status"] for m in monitors])

    state = db.get_metric_state("correlated_outage")
    was_correlated = state is not None and state["status"] == "active"

    if is_correlated_now and not was_correlated:
        incident_id = db.open_server_incident(
            "Server", f"Correlated outage — {down_count} of {total} monitored sites down", "critical")
        subject, body, html_body = email_alerts.format_server_alert(
            f"Correlated outage — {down_count} of {total} monitored sites down", "critical",
            ts=time.time())
        email_alerts.send(subject, body, html_body)
        db.record_incident_event(incident_id, time.time(), "email_sent", subject)
        db.set_metric_state("correlated_outage", "active", None, 0, incident_id, time.time())
        return  # this tick's individual events are the same root cause — suppressed

    if was_correlated and not is_correlated_now:
        downtime_sec = db.resolve_server_incident(state["incident_id"])
        subject, body, html_body = email_alerts.format_recovered(
            "Server (correlated outage)", db.format_duration(downtime_sec), ts=time.time())
        email_alerts.send(subject, body, html_body)
        db.record_incident_event(state["incident_id"], time.time(), "email_sent", subject)
        db.set_metric_state("correlated_outage", "inactive", None, 0, None, time.time())
        return  # the individual recovery that triggered this is part of the same event

    if was_correlated and is_correlated_now:
        return  # ongoing, already alerted — no repeat noise

    now = time.time()
    for event in events:
        kind, name, detail, incident_id = event
        url, opened_at, notify_enabled, context = _incident_context(incident_id)
        if not notify_enabled:
            continue
        item = {"name": name, "detail": detail, "url": url, "incident_id": incident_id, "context": context,
                # The incident's own `started` beats `now` for a down item: it's
                # when the check actually failed, not when this tick noticed it.
                "ts": opened_at if kind == "opened" else now}
        _queue_alert("opened" if kind == "opened" else "resolved", item, now)


_PENDING = {"opened": [], "resolved": []}
_BATCH_STARTED_AT = {"opened": None, "resolved": None}


def _queue_alert(kind, item, now):
    """Buffers one down/resolved item and flushes the whole batch (one
    consolidated email) once ALERT_BATCH_WINDOW_SEC has passed since the
    first item queued in it -- see the constant's docstring for why."""
    _PENDING[kind].append(item)
    if _BATCH_STARTED_AT[kind] is None:
        _BATCH_STARTED_AT[kind] = now
    if now - _BATCH_STARTED_AT[kind] >= ALERT_BATCH_WINDOW_SEC:
        _flush_batch(kind)


def _flush_expired_batches(now):
    """Catches a batch that's aged past the window with no new item to
    trigger the check in _queue_alert -- this runs every tick (handle_tick_events
    is always called, even with zero new events) so a lone down event still
    gets mailed after the window elapses instead of waiting forever for a
    second one that may never come."""
    for kind in ("opened", "resolved"):
        if _PENDING[kind] and now - _BATCH_STARTED_AT[kind] >= ALERT_BATCH_WINDOW_SEC:
            _flush_batch(kind)


def _flush_batch(kind):
    items = _PENDING[kind]
    if not items:
        return
    _PENDING[kind] = []
    _BATCH_STARTED_AT[kind] = None
    formatter = email_alerts.format_down_batch if kind == "opened" else email_alerts.format_recovered_batch
    subject, body, html_body = formatter(items)
    email_alerts.send(subject, body, html_body)
    sent_at = time.time()
    for item in items:
        if item["incident_id"] is not None:
            db.record_incident_event(item["incident_id"], sent_at, "email_sent", subject)


def purge_old_checks():
    """PRD §16 retention: deletes checks rows older than CHECKS_RETENTION_DAYS.
    Gated to once per PURGE_INTERVAL_SEC via the same get/set_metric_state
    primitive ssl_check.py uses for its own date-based cadence -- reused
    here purely for its last-run timestamp, ignoring the hysteresis fields.
    """
    state = db.get_metric_state("checks_retention_purge")
    now = time.time()
    if state is not None and now - state["last_read_ts"] < PURGE_INTERVAL_SEC:
        return
    cutoff = now - CHECKS_RETENTION_DAYS * 86400
    deleted = db.delete_old_checks(cutoff)
    if deleted:
        print(f"[monitor_engine] purged {deleted} checks older than {CHECKS_RETENTION_DAYS} days")
    db.set_metric_state("checks_retention_purge", "ok", None, 0, None, now)


def run_due_checks():
    now = time.time()
    due = db.get_due_monitors(now)
    events = []
    if due:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            events = [e for e in pool.map(lambda m: _check_one(m, now), due) if e]
    handle_tick_events(events)


def start_background_scheduler(debug=False):
    def loop():
        while True:
            try:
                run_due_checks()
            except Exception as e:
                print(f"[monitor_engine] scheduler tick failed: {e}")
            try:
                server_health.check_server_metrics()
            except Exception as e:
                print(f"[monitor_engine] server-metrics tick failed: {e}")
            try:
                ssl_check.check_all_ssl(db.list_monitors())
            except Exception as e:
                print(f"[monitor_engine] ssl-check tick failed: {e}")
            try:
                purge_old_checks()
            except Exception as e:
                print(f"[monitor_engine] checks-retention purge failed: {e}")
            time.sleep(TICK_SEC)

    # Under `--debug`, Werkzeug's reloader re-imports app.py in a parent
    # watcher process (no WERKZEUG_RUN_MAIN) before spawning the real server
    # child (WERKZEUG_RUN_MAIN=true) — starting the scheduler in the parent
    # too would double-check every monitor.
    if debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    threading.Thread(target=loop, daemon=True).start()
