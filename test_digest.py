"""#5 Weekly uptime/SLA digest: build_digest summarises per-monitor uptime %,
incident count and downtime over the window (worst-uptime first); maybe_send is
opt-in, sends once then gates, and records its last run. Mocked -- no network.

Run: ./venv/Scripts/python.exe test_digest.py
"""
import os
from unittest.mock import patch

import db
import digest
import email_alerts

db.DB_PATH = "test_digest.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

NOW = 1_000_000.0
DAY = 86400

good = db.add_monitor("good.example", "https://good.example", "website")
bad = db.add_monitor("bad.example", "https://bad.example", "website")

# good: 100 ok checks in-window. bad: 90 ok + 10 fail in-window -> 90% uptime.
for i in range(100):
    db.record_check(good, NOW - DAY + i, True, 100, "ok")
for i in range(90):
    db.record_check(bad, NOW - DAY + i, True, 100, "ok")
for i in range(10):
    db.record_check(bad, NOW - DAY + 90 + i, False, 100, "HTTP 500")
# one resolved incident for bad, inside the window (patch db's clock so both
# started and resolved land on the synthetic timeline, giving a +50s downtime)
with patch("db.time.time", return_value=NOW - DAY + 50):
    db.open_incident(bad, "bad.example", "HTTP 500", problem_type="http")
with patch("db.time.time", return_value=NOW - DAY + 100):
    db.resolve_incident(bad, problem_type="http")
# a check older than the window must be excluded from the % (count stays 100/90).
db.record_check(good, NOW - 30 * DAY, False, 100, "old fail")


# --- build_digest ---
subject, body = digest.build_digest(NOW, window_days=7)
assert "2 monitors" in subject and "1 incidents" in subject, subject
assert "100.00%" in body and "90.00%" in body, body
# worst uptime (bad, 90%) is listed before good (100%).
assert body.index("bad.example") < body.index("good.example"), "worst uptime must lead"
print("build_digest checks passed.")


# --- maybe_send: opt-in + gating ---
sent = []
with patch.dict(os.environ, {}, clear=False):
    os.environ.pop("WEEKLY_DIGEST", None)
    with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        assert digest.maybe_send(NOW) is False     # disabled -> nothing
assert sent == []

with patch.dict(os.environ, {"WEEKLY_DIGEST": "1"}, clear=False), \
     patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
    assert digest.maybe_send(NOW) is True          # first run sends
    assert digest.maybe_send(NOW + 60) is False    # gated (not a week yet)
    assert digest.maybe_send(NOW + 8 * DAY) is True  # a week later -> sends again
assert len(sent) == 2, sent
print("maybe_send opt-in + gating checks passed.")

os.remove(db.DB_PATH)
print("All digest checks passed.")
