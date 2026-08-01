"""TDD self-check for the alert batching/digest feature: multiple monitors
that go down (or recover) within a short window must produce ONE
consolidated email, not one per site -- this is what actually happened in
production (a shared host took out 4 sites; each monitor's own staggered
check schedule meant the down events landed 20-30s apart across separate
scheduler ticks, so the naive "send immediately per event" design flooded
20+ near-identical [DOWN] emails instead of one digest).

Run: ./venv/Scripts/python.exe test_alert_batching.py
"""
import os
import time
from unittest.mock import patch

import db

db.DB_PATH = "test_alert_batching.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import email_alerts
import monitor_engine
from monitor_engine import _check_one, _flush_expired_batches, _queue_alert, handle_tick_events

monitor_engine.ALERT_BATCH_WINDOW_SEC = 60  # the real production default, exercised for real here


def _reset_batches():
    monitor_engine._PENDING = {"opened": [], "resolved": []}
    monitor_engine._BATCH_STARTED_AT = {"opened": None, "resolved": None}


# ---------- core primitive: multiple items in-window flush as ONE email ----------

_reset_batches()
sent = []
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append((s, b, h))):
    _queue_alert("opened", {"name": "site-a", "detail": "HTTP 500", "url": "https://a.example",
                             "incident_id": None, "context": None, "ts": 1000.0}, now=1000.0)
    assert sent == [], "must not flush before the window elapses"

    _queue_alert("opened", {"name": "site-b", "detail": "HTTP 503", "url": "https://b.example",
                             "incident_id": None, "context": None, "ts": 1025.0}, now=1025.0)
    assert sent == [], "second item within the window must not flush early either"

    _queue_alert("opened", {"name": "site-c", "detail": "Timeout after 10s", "url": "https://c.example",
                             "incident_id": None, "context": None, "ts": 1050.0}, now=1050.0)
    assert sent == [], "third item still within 60s of the first"

    # A tick with no new event, 65s after the first item -> the window has
    # elapsed, so this must flush even without a triggering new item
    # (exactly what _flush_expired_batches exists for).
    _flush_expired_batches(now=1065.0)

assert len(sent) == 1, f"3 down events in one window must produce exactly 1 email, got {len(sent)}"
subject, body, html = sent[0]
assert subject == "[DOWN] 3 sites down", subject
assert "site-a" in html and "site-b" in html and "site-c" in html, html
assert "HTTP 500" in html and "HTTP 503" in html and "Timeout after 10s" in html, html
assert "3 sites are down" in html
# Never the single-monitor hero/timeline treatment for a multi-item digest --
# that doesn't generalize across different monitors' independent histories.
assert "Failed checks" not in html
assert "Recent checks" not in html

print("All multi-item batching checks passed.")


# ---------- a batch that never grows past 1 item still sends after the window ----------

_reset_batches()
sent = []
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append((s, b, h))):
    _queue_alert("opened", {"name": "solo-site", "detail": "Connection refused", "url": "https://solo.example",
                             "incident_id": None, "context": None, "ts": 2000.0}, now=2000.0)
    assert sent == []
    _flush_expired_batches(now=2061.0)  # 61s later, no second item ever arrived

assert len(sent) == 1
subject, body, html = sent[0]
# A lone item uses format_down's own subject/card, not "[DOWN] 1 sites down".
assert subject == "[DOWN] solo-site — Connection refused", subject
assert "sites are down" not in html
assert "solo-site is down" in html or "solo-site" in html

print("All solo-item-after-window checks passed.")


# ---------- items outside the window start a fresh, separate batch ----------

_reset_batches()
sent = []
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append((s, b, h))):
    _queue_alert("opened", {"name": "early-site", "detail": "boom", "url": None,
                             "incident_id": None, "context": None, "ts": 3000.0}, now=3000.0)
    _flush_expired_batches(now=3061.0)  # first batch flushes alone
    assert len(sent) == 1

    _queue_alert("opened", {"name": "late-site", "detail": "boom2", "url": None,
                             "incident_id": None, "context": None, "ts": 3200.0}, now=3200.0)
    _flush_expired_batches(now=3261.0)

assert len(sent) == 2, "a later, unrelated event must not retroactively join an already-flushed batch"
assert "early-site" in sent[0][2] and "late-site" not in sent[0][2]
assert "late-site" in sent[1][2] and "early-site" not in sent[1][2]

print("All separate-batch checks passed.")


# ---------- down and recovered batch independently, never merged together ----------

_reset_batches()
sent = []
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append((s, b, h))):
    _queue_alert("opened", {"name": "down-site", "detail": "fail", "url": None,
                             "incident_id": None, "context": None, "ts": 4000.0}, now=4000.0)
    _queue_alert("resolved", {"name": "up-site", "detail": "5m", "url": None,
                               "incident_id": None, "context": None, "ts": 4005.0}, now=4005.0)
    _flush_expired_batches(now=4066.0)

assert len(sent) == 2, "down and recovered must flush as two separate emails, never one mixed digest"
subjects = {s for s, _, _ in sent}
assert subjects == {"[DOWN] down-site — fail", "[RECOVERED] up-site — 5m downtime"}, subjects

print("All down/recovered-independent-batching checks passed.")


# ---------- recovered digest uses the recovered wording/icon, not "down" ----------

_reset_batches()
sent = []
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append((s, b, h))):
    _queue_alert("resolved", {"name": "r1", "detail": "3m", "url": "https://r1.example",
                               "incident_id": None, "context": None, "ts": 5000.0}, now=5000.0)
    _queue_alert("resolved", {"name": "r2", "detail": "4m", "url": "https://r2.example",
                               "incident_id": None, "context": None, "ts": 5010.0}, now=5010.0)
    _flush_expired_batches(now=5071.0)

assert len(sent) == 1
subject, body, html = sent[0]
assert subject == "[RECOVERED] 2 sites recovered", subject
assert "2 sites recovered" in html
assert "r1" in html and "3m" in html
assert "r2" in html and "4m" in html
assert "#39ff88" in html, "recovered digest must use the phosphor accent, not coral"

print("All recovered-digest checks passed.")


# ---------- end-to-end through handle_tick_events + real time.time(), staggered
# ticks, reproducing the actual production scenario: 3 monitors on a shared
# host go down on separate ticks (~1s apart here vs ~25s in production --
# same shape, just compressed so the test doesn't need to sleep 60s) ----------

_reset_batches()
monitor_engine.ALERT_BATCH_WINDOW_SEC = 2  # short window so this test finishes in ~2s, not 60s

fleet = [db.add_monitor(f"shared-host-{i}", f"https://s{i}.example", "website") for i in range(3)]
# A healthy peer keeps compute_is_correlated_outage() False -- an all-down
# fleet is a DIFFERENT, already-existing feature (PRD §9) that sends its own
# immediate consolidated email and deliberately bypasses this batching path;
# testing the batching path means avoiding that collision, same as
# test_email_delivery.py does for the same reason.
healthy = db.add_monitor("healthy-peer", "https://peer.example", "website")
db.update_monitor_state(healthy, "up", 0, 2, time.time(), 20)
sent = []
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append((s, b, h))):
    for mid in fleet:
        with patch("monitor_engine.do_http_check", return_value=(False, None, "HTTP 503")):
            for _ in range(3):  # reach the default fail_threshold=3
                event = _check_one(db.get_monitor(mid), time.time())
        assert event is not None and event[0] == "opened", event
        handle_tick_events([event])
        assert sent == [], "must still be buffered, not sent per-monitor"

    time.sleep(2.1)
    handle_tick_events([])  # the empty-events tick that would run every 5s in production

assert len(sent) == 1, f"3 staggered per-monitor down events must still consolidate into 1 email, got {len(sent)}"
subject, body, html = sent[0]
assert subject == "[DOWN] 3 sites down", subject
for i in range(3):
    assert f"shared-host-{i}" in html, html

print("All end-to-end staggered-tick consolidation checks passed.")

os.remove(db.DB_PATH)
print("All alert batching checks passed.")
