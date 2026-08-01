"""TDD self-check for the premium down/recovered alert email: real fail
thresholds, real recent-check timeline, real response times -- nothing
invented. A caller with no monitor context (server-level/correlated-outage
alerts) must keep getting the plain card, not blank/fabricated stats.

Run: ./venv/Scripts/python.exe test_premium_email.py
"""
import os
import time
from unittest.mock import patch

import db

db.DB_PATH = "test_premium_email.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import email_alerts
import monitor_engine
from monitor_engine import _alert_context, _check_one, handle_tick_events

monitor_engine.ALERT_BATCH_WINDOW_SEC = 0  # flush every batch immediately, matching this file's per-event assertions

# ---------- monitor_engine._alert_context: real config, not invented ----------

assert _alert_context(None) is None, "no monitor -> no context, never fabricate stats"

web_id = db.add_monitor("acme", "https://acme.example", "website", retries=4, interval_sec=45)
web_row = db.get_monitor(web_id)
ctx = _alert_context(web_row)
assert ctx["fail_threshold"] == 5, ctx  # retries(4) + 1, the real evaluate_status() rule
assert ctx["check_type"] == "Website"
assert ctx["interval_sec"] == 45
assert ctx["checks"] == [], "no checks recorded yet -> empty timeline, not fabricated rows"

# Default retries (None) -> fail_threshold 3, the same default evaluate_status() uses.
default_id = db.add_monitor("default-retries", "https://default.example", "website")
assert _alert_context(db.get_monitor(default_id))["fail_threshold"] == 3

# Push monitors are always fail_threshold=1 regardless of retries -- a single
# missed check-in is unambiguous, matching _check_one's push branch.
push_id = db.add_monitor("push-mon", "", "push", interval_sec=60, retries=4)
assert _alert_context(db.get_monitor(push_id))["fail_threshold"] == 1

print("All _alert_context checks passed.")


# ---------- checks in the context are the REAL rows, most-recent-last ----------

db.record_check(web_id, 1000.0, True, 50, "HTTP 200")
db.record_check(web_id, 1010.0, False, None, "Timeout after 10s")
db.record_check(web_id, 1020.0, False, None, "Connection refused")
ctx = _alert_context(db.get_monitor(web_id))
assert ctx["checks"] == [
    (1000.0, True, "HTTP 200"),
    (1010.0, False, "Timeout after 10s"),
    (1020.0, False, "Connection refused"),
], ctx["checks"]

print("All real-checks-in-context checks passed.")


# ---------- format_down/format_recovered: premium card with real numbers ----------

context = _alert_context(db.get_monitor(web_id))
_, _, html_down = email_alerts.format_down("acme", "Connection refused", url="https://acme.example",
                                             ts=1020.0, context=context)

# The real fail_threshold (5, from retries=4), not the reference design's
# fabricated "3/3".
assert "5/5" in html_down, html_down
assert "3/3" not in html_down
# The real recent checks, verbatim -- not the reference's fake "retry 2/3 failed".
assert "Timeout after 10s" in html_down
assert "Connection refused" in html_down
assert "retry 2/3 failed" not in html_down
assert "RVX-0417" not in html_down, "no fabricated incident id"
assert "218ms" not in html_down, "no fabricated response time on the down variant"

db.update_monitor_state(web_id, "up", 0, 2, time.time(), 187)
context2 = _alert_context(db.get_monitor(web_id))
_, _, html_up = email_alerts.format_recovered("acme", "4m", url="https://acme.example",
                                                ts=time.time(), context=context2)
assert ">187<" in html_up, "real last_response_ms must appear, not a fabricated one"

# No context (server-level / correlated-outage alerts) -> the plain card,
# never rendered with blank "Failed checks"/"Recent checks" sections.
_, _, html_plain = email_alerts.format_down("Server (correlated outage)", "3 of 5 down")
assert "Failed checks" not in html_plain
assert "Recent checks" not in html_plain
assert "Rovix AI Monitoring" in html_plain, "still gets the (simpler) branded card"

print("All format_down/format_recovered premium-context checks passed.")


# ---------- untrusted data is still escaped inside the premium card ----------

evil_monitor_id = db.add_monitor('<script>alert(1)</script>', "https://evil.example", "website")
db.record_check(evil_monitor_id, time.time(), False, None, '<img src=x onerror=alert(2)>')
evil_ctx = _alert_context(db.get_monitor(evil_monitor_id))
_, _, evil_html = email_alerts.format_down('<script>alert(1)</script>', "boom",
                                            url="https://evil.example", ts=time.time(), context=evil_ctx)
assert "<script>alert(1)</script>" not in evil_html, evil_html
assert "<img src=x" not in evil_html, evil_html
assert "&lt;script&gt;" in evil_html
assert "&lt;img" in evil_html

print("All premium-card escaping checks passed.")


# ---------- end-to-end through handle_tick_events with a real incident ----------

fleet_a = db.add_monitor("fleet-a", "https://a.example", "website")
fleet_b = db.add_monitor("fleet-b", "https://b.example", "website")
db.update_monitor_state(fleet_b, "up", 0, 2, time.time(), 30)  # keeps correlated-outage from tripping

with patch("monitor_engine.do_http_check", return_value=(False, None, "HTTP 503")):
    event = _check_one(db.get_monitor(fleet_a), time.time())
with patch("monitor_engine.do_http_check", return_value=(False, None, "HTTP 503")):
    _check_one(db.get_monitor(fleet_a), time.time())
with patch("monitor_engine.do_http_check", return_value=(False, None, "HTTP 503")):
    opened_event = _check_one(db.get_monitor(fleet_a), time.time())

assert opened_event[0] == "opened", opened_event
captured = {}
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: captured.update(subject=s, html=h)):
    handle_tick_events([opened_event])

assert captured.get("html"), "no email was sent for a real incident"
assert "3/3" in captured["html"], "default fail_threshold (3) must show, not a placeholder"
assert "HTTP 503" in captured["html"], "the real check detail must appear in the timeline"
assert "fleet-a" in captured["html"]

os.remove(db.DB_PATH)
print("All handle_tick_events end-to-end premium-email checks passed.")
print("All premium email checks passed.")
