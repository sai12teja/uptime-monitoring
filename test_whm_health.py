"""PRD: WHM/cPanel remote server resource health. Mocked WHM responses (no real
network -- the live token/host is the §7 admin handoff), same style as
test_ssl_check.py. Covers config parsing, the warning/critical state machine
reused from server_health, no-repeat alerting, alert content, graceful
degradation of unavailable metrics, and that one failing server is isolated.

Run: ./venv/Scripts/python.exe test_whm_health.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import whm_health

db.DB_PATH = "test_whm_health.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()


# --- servers(): parse host:token pairs from the env, ignore blanks/garbage ---
with patch.dict(os.environ, {"WHM_SERVERS": "a.example:tok1, b.example:tok2 ,,bad"}):
    assert whm_health.servers() == [("a.example", "tok1"), ("b.example", "tok2")], whm_health.servers()
print("servers() parsing checks passed.")


# --- _evaluate: the warning/critical state machine + alerting, per server ---
HOST = "srv1.example"
sent = []


def ev(value, now):
    sent.clear()
    with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        whm_health._evaluate(HOST, "disk", value, 80, 90, now)


def ssl_incidents():
    return [i for i in db.list_incidents() if i["target_name"] == HOST]


# One read over warning is NOT enough (2-read hysteresis, reused from server_health).
ev(85, 1000)
assert sent == [], sent
assert ssl_incidents() == []

# Second consecutive warning read opens the incident and sends ONE alert that
# names the server, metric, value and severity (§FR6).
ev(85, 1060)
assert len(sent) == 1, sent
assert sent[0] == "[WARNING] srv1.example — Disk 85% full", sent[0]
incs = ssl_incidents()
assert len(incs) == 1 and incs[0]["severity"] == "warning", incs

# A steady warning reading does NOT re-alert every poll (§FR3).
ev(85, 1120)
assert sent == [], sent
assert len(ssl_incidents()) == 1

# Two critical reads escalate the SAME incident (no second incident) and send a
# clearly-distinguished critical alert (§FR4).
ev(95, 1180)
ev(95, 1240)
assert sent == ["[CRITICAL] srv1.example — Disk 95% full"], sent
incs = ssl_incidents()
assert len(incs) == 1 and incs[0]["severity"] == "critical", incs

# Recovery resolves it (two ok reads).
ev(10, 1300)
ev(10, 1360)
assert len(sent) == 1 and sent[0].startswith("[RECOVERED] srv1.example (Disk)"), sent
assert ssl_incidents()[0]["resolved"] is not None
print("_evaluate state-machine + alert-content checks passed.")


# --- a metric that's unavailable this cycle never alerts ---
before = len(db.list_incidents())
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
    whm_health._evaluate("srv2.example", "mem", None, 80, 90, 2000)
assert len(db.list_incidents()) == before, "None value must not open an incident"
print("missing-metric no-alert check passed.")


# --- check_all(): isolates servers; one with no usable metrics doesn't stop
# another from alerting. Gating disabled so two polls (hysteresis) run. ---
def fake_fetch_one(host, token, key):
    # bad.example breaches critical disk; the other server reports nothing usable.
    return 95 if (host == "bad.example" and key == "disk") else None


with patch.dict(os.environ, {"WHM_SERVERS": "quiet.example:t,bad.example:t"}), \
     patch("whm_health.CHECK_INTERVAL_SEC", 0), \
     patch("whm_health._fetch_one", side_effect=fake_fetch_one), \
     patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: None):
    whm_health.check_all()
    whm_health.check_all()

bad = [i for i in db.list_incidents() if i["target_name"] == "bad.example"]
quiet = [i for i in db.list_incidents() if i["target_name"] == "quiet.example"]
assert len(bad) == 1 and bad[0]["severity"] == "critical", bad
assert quiet == [], "a server with no usable metrics must not create incidents"
print("check_all server-isolation checks passed.")


# --- config hardening: a malformed int env var falls back, never crashes ---
with patch.dict(os.environ, {"WHM_X": "not-an-int"}):
    assert whm_health._int_env("WHM_X", 42) == 42
with patch.dict(os.environ, {"WHM_X": "7"}):
    assert whm_health._int_env("WHM_X", 42) == 7
assert whm_health._int_env("WHM_UNSET_XYZ", 9) == 9
print("config-hardening (_int_env) checks passed.")


# --- a WHM-level failure (HTTP 200 + metadata.result=0, e.g. bad token/ACL)
# must raise so the reason is logged, not be swallowed as empty data ---
import json as _json


class _Resp:
    def __init__(self, payload):
        self._p = _json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


with patch("urllib.request.urlopen", return_value=_Resp({"metadata": {"result": 0, "reason": "Access denied"}})):
    try:
        whm_health._api("h", "tok", "getdiskusage")
        assert False, "WHM result=0 must raise"
    except RuntimeError as e:
        assert "Access denied" in str(e), e

with patch("urllib.request.urlopen",
           return_value=_Resp({"metadata": {"result": 1}, "data": {"partition": [{"percentage": 42}]}})):
    assert whm_health._api("h", "tok", "getdiskusage")["data"]["partition"][0]["percentage"] == 42
print("WHM API error-detection checks passed.")


# --- disk parse tolerates a single-partition dict and string percentages ---
with patch("whm_health._api", return_value={"data": {"partition": {"percentage": "73%"}}}):
    assert whm_health._disk_pct("h", "t") == 73.0
with patch("whm_health._api", return_value={"data": {"partition": [{"percentage": 10}, {"percentage": 88}]}}):
    assert whm_health._disk_pct("h", "t") == 88.0
with patch("whm_health._api", return_value={"data": {}}):
    assert whm_health._disk_pct("h", "t") is None
print("disk-parse robustness checks passed.")

os.remove(db.DB_PATH)
print("All whm_health checks passed.")
