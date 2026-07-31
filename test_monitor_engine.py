"""Assert-based self-check for the flap-protection state machine (PRD §10),
the keyword check's gzip handling (§5.2, the CRM Tier 1 check), and the
open/resolve -> email wiring (§14).

Run: ./venv/Scripts/python.exe test_monitor_engine.py
"""
import gzip
import os
import time
from unittest.mock import patch

from monitor_engine import do_http_check, evaluate_status

# First-ever check: awaiting -> up on a single success (no flapping to guard against yet).
assert evaluate_status("awaiting", 0, 0, True) == ("up", 0, 1, False, False)

# First-ever check fails: stays awaiting until the 3-fail threshold.
assert evaluate_status("awaiting", 0, 0, False) == ("awaiting", 1, 0, False, False)

# Up, one failure: not enough to flip yet.
assert evaluate_status("up", 0, 0, False) == ("up", 1, 0, False, False)
assert evaluate_status("up", 1, 0, False) == ("up", 2, 0, False, False)

# Third consecutive failure -> DOWN, incident opened.
assert evaluate_status("up", 2, 0, False) == ("down", 3, 0, True, False)

# Down, one success: not enough to recover yet (flap protection).
assert evaluate_status("down", 0, 0, True) == ("down", 0, 1, False, False)

# Down, second consecutive success -> UP, incident resolved.
assert evaluate_status("down", 0, 1, True) == ("up", 0, 2, False, True)

# A single failure after recovering resets the fail counter, doesn't reopen anything.
assert evaluate_status("up", 0, 2, False) == ("up", 1, 0, False, False)


# ---------- Keyword check: gzip-encoded responses (§5.2 CRM Tier 1) ----------

class _FakeResponse:
    def __init__(self, body, headers, status=200):
        self._body, self.headers, self.status = body, headers, status

    def read(self, n):
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _check(body, headers, keyword):
    monitor = {"url": "https://example.invalid", "keyword": keyword,
               "http_method": "GET", "http_body": None, "http_body_encoding": "json",
               "timeout_sec": None}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(body, headers)):
        return do_http_check(monitor)


LOGIN_PAGE = b"<html><body><form id='login'>Sign in</form></body></html>"

# Uncompressed: keyword present -> up, keyword absent -> down.
assert _check(LOGIN_PAGE, {}, "login")[0] is True
assert _check(LOGIN_PAGE, {}, "not-on-this-page")[0] is False

# gzip-encoded: the bug was matching the keyword against raw gzip bytes, which
# never matches -> a false DOWN on every check of a compressed CRM login page.
gzipped = gzip.compress(LOGIN_PAGE)
assert b"login" not in gzipped, "precondition: keyword is not findable in raw gzip bytes"
assert _check(gzipped, {"Content-Encoding": "gzip"}, "login")[0] is True
assert _check(gzipped, {"Content-Encoding": "gzip"}, "not-on-this-page")[0] is False

# Malformed gzip -> reported as unreadable, not crashed and not a silent pass.
ok, _, detail = _check(b"not actually gzip", {"Content-Encoding": "gzip"}, "login")
assert ok is False and "not readable" in detail, detail

print("All evaluate_status checks passed.")
print("All gzip keyword-check assertions passed.")


# ---------- _check_one() -> handle_tick_events() -> email wiring (§14) ----------
# Isolated throwaway DB (same pattern as test_dashboard.py) + a canned
# do_http_check + a captured email_alerts.send. _check_one no longer sends
# email directly (§9/§15 correlated-outage consolidation needs the whole
# tick's results first) — it returns an event, which handle_tick_events
# turns into the actual send, exactly like the real scheduler tick does.
import db
import email_alerts
from monitor_engine import _check_one, handle_tick_events

db.DB_PATH = "test_monitor_engine.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("wiring-test.invalid", "https://wiring-test.invalid", "website")

sent = []
with patch("monitor_engine.do_http_check", return_value=(False, 50, "HTTP 500")):
    event = None
    for _ in range(3):  # 3rd consecutive failure crosses the DOWN threshold
        event = _check_one(db.get_monitor(mid), time.time())
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
    handle_tick_events([event] if event else [])

assert sent == ["[DOWN] wiring-test.invalid — HTTP 500"], sent

sent.clear()
with patch("monitor_engine.do_http_check", return_value=(True, 40, "HTTP 200")):
    event = None
    for _ in range(2):  # 2nd consecutive success crosses back to UP
        event = _check_one(db.get_monitor(mid), time.time())
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
    handle_tick_events([event] if event else [])

assert len(sent) == 1 and sent[0].startswith("[RECOVERED] wiring-test.invalid — "), sent

os.remove(db.DB_PATH)
print("All _check_one email-wiring checks passed.")


# ---------- gap 7: incident_events logged by _check_one/handle_tick_events ----------
db.DB_PATH = "test_monitor_engine_events.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid2 = db.add_monitor("events-test.invalid", "https://events-test.invalid", "website")

sent2 = []
with patch("monitor_engine.do_http_check", return_value=(False, 50, "HTTP 500")):
    events2 = [_check_one(db.get_monitor(mid2), time.time()) for _ in range(3)]
opened_event = next(e for e in events2 if e)
incident_id = opened_event[3]
assert incident_id is not None

# The triggering (3rd) failure is logged as a check_failure event.
kinds = [e["event_type"] for e in db.list_incident_events(incident_id)]
assert kinds == ["check_failure"], kinds

# A further failure while still down logs another check_failure, without
# re-opening or duplicating the incident.
with patch("monitor_engine.do_http_check", return_value=(False, 50, "HTTP 500")):
    still_down_event = _check_one(db.get_monitor(mid2), time.time())
assert still_down_event is None  # no opened/resolved transition -> no email-worthy event
kinds = [e["event_type"] for e in db.list_incident_events(incident_id)]
assert kinds == ["check_failure", "check_failure"], kinds

with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent2.append(s)):
    handle_tick_events([opened_event])
email_events = [e for e in db.list_incident_events(incident_id) if e["event_type"] == "email_sent"]
assert len(email_events) == 1 and email_events[0]["detail"] == sent2[0]

# Recovery: resolved event comes from db.resolve_incident itself (already
# covered by test_incident_events.py); handle_tick_events also logs the
# recovery email against the same incident_id.
with patch("monitor_engine.do_http_check", return_value=(True, 40, "HTTP 200")):
    resolved_event = None
    for _ in range(2):
        resolved_event = _check_one(db.get_monitor(mid2), time.time())
assert resolved_event[3] == incident_id
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent2.append(s)):
    handle_tick_events([resolved_event])
email_events = [e for e in db.list_incident_events(incident_id) if e["event_type"] == "email_sent"]
assert len(email_events) == 2

os.remove(db.DB_PATH)
print("All incident_events (gap 7) wiring checks passed.")
