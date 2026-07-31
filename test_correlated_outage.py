"""TDD self-check for correlated-outage detection (PRD §9/§15).

Run: ./venv/Scripts/python.exe test_correlated_outage.py
"""
from monitor_engine import compute_is_correlated_outage, MIN_MONITORS_FOR_CORRELATION

assert MIN_MONITORS_FOR_CORRELATION == 3

# No monitors, or fewer than the minimum — never correlated, even if 100% of
# a tiny set is down (1-out-of-1 or 2-out-of-2 isn't evidence of a shared cause).
assert compute_is_correlated_outage([]) is False
assert compute_is_correlated_outage(["down"]) is False
assert compute_is_correlated_outage(["down", "down"]) is False

# At the minimum, all down -> correlated.
assert compute_is_correlated_outage(["down", "down", "down"]) is True

# Below 100% — even one survivor means it's not a shared-cause outage.
assert compute_is_correlated_outage(["down", "down", "up"]) is False
assert compute_is_correlated_outage(["down", "down", "overdue"]) is False
assert compute_is_correlated_outage(["down", "down", "awaiting"]) is False

# Larger set, all down.
assert compute_is_correlated_outage(["down"] * 15) is True

# Larger set, one recovering — not correlated anymore, even at 14/15.
assert compute_is_correlated_outage(["down"] * 14 + ["up"]) is False

print("All compute_is_correlated_outage checks passed.")


# ---------- handle_tick_events() — the email-consolidation decision ----------
# Isolated throwaway DB, same pattern as every other integration test this
# session. Monitor states are seeded directly (not via real HTTP checks) —
# this tests the consolidation decision, not the checking itself.
import os
import time
from unittest.mock import patch

import db
import email_alerts
from monitor_engine import handle_tick_events

db.DB_PATH = "test_correlated_outage.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

sent = []


def seed(name, status):
    mid = db.add_monitor(name, f"https://{name}", "website")
    db.update_monitor_state(mid, status, 0, 0, time.time(), None)
    return mid


def fire(events):
    sent.clear()
    with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        handle_tick_events(events)


# Case 1: below the minimum monitor count — individual email fires normally,
# no consolidation logic kicks in even though 100% of these 2 are down.
m1 = seed("site1.invalid", "down")
m2 = seed("site2.invalid", "down")
fire([("opened", "site1.invalid", "HTTP 500", None)])
assert sent == ["[DOWN] site1.invalid — HTTP 500"], sent

os.remove(db.DB_PATH)
db.init_db()
sent.clear()

# Case 2: 3 monitors, all down -> ONE consolidated critical email, not 3.
seed("a.invalid", "down")
seed("b.invalid", "down")
seed("c.invalid", "down")
fire([("opened", "a.invalid", "timeout", None), ("opened", "b.invalid", "timeout", None), ("opened", "c.invalid", "timeout", None)])
assert sent == ["[CRITICAL] Server — Correlated outage — 3 of 3 monitored sites down"], sent
state = db.get_metric_state("correlated_outage")
assert state["status"] == "active" and state["incident_id"] is not None
incidents = [i for i in db.list_incidents() if i["target_name"] == "Server"]
assert len(incidents) == 1 and incidents[0]["resolved"] is None

# Case 3: still correlated, no change — silent, no repeated email.
fire([])
assert sent == [], sent

# Case 4: one recovers (now 2 of 3 down) -> exits correlated state -> ONE
# recovery email, the individual site's own [RECOVERED] is suppressed.
with db._conn() as conn:
    conn.execute("UPDATE monitors SET status='up' WHERE name='a.invalid'")
fire([("resolved", "a.invalid", "5m", None)])
assert len(sent) == 1 and sent[0].startswith("[RECOVERED] Server (correlated outage) — "), sent
assert db.get_metric_state("correlated_outage")["status"] == "inactive"

os.remove(db.DB_PATH)
print("All handle_tick_events checks passed.")
