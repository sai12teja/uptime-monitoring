"""WHM security surfacing: malware (any detection = critical), ModSecurity /
cPHulk (count threshold), dedup per (server, source), resolve when cleared,
and the best-effort count parser. Mocked WHM responses -- no real network.

Run: ./venv/Scripts/python.exe test_whm_security.py
"""
import os
from unittest.mock import patch

import db
import email_alerts
import whm_security

db.DB_PATH = "test_whm_security.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()


# --- _count_from: tolerates the common WHM/plugin response shapes ---
assert whm_security._count_from({"data": {"count": 3}}) == 3
assert whm_security._count_from({"data": {"items": [1, 2]}}) == 2
assert whm_security._count_from({"data": {"detections": [1, 2, 3, 4]}}) == 4
assert whm_security._count_from({"data": []}) == 0
assert whm_security._count_from({"data": {"nothing": "here"}}) == 0
print("_count_from parser checks passed.")


HOST = "srv.example"
sent = []


def ev(source, count):
    sent.clear()
    with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        whm_security._evaluate(HOST, source, count, now=1000.0)


def incidents():
    return [i for i in db.list_incidents() if i["target_name"] == HOST]


# --- malware: ANY detection is immediately critical; cleared -> resolve ---
with patch.dict(whm_security._SOURCES, {"imunify": (whm_security._SOURCES["imunify"][0], 1,
                                                    "Malware (ImunifyAV/360)", "detection(s)")}):
    ev("imunify", 2)
    assert len(sent) == 1 and sent[0].startswith("[CRITICAL] srv.example"), sent
    assert "Malware" in sent[0] and "2 detection(s)" in sent[0], sent[0]
    assert len([i for i in incidents() if i["resolved"] is None]) == 1

    ev("imunify", 2)  # still detected -> no repeat alert
    assert sent == [], sent

    ev("imunify", 0)  # cleaned -> resolve, one recovery email
    assert len(sent) == 1 and sent[0].startswith("[RECOVERED] srv.example"), sent
    assert all(i["resolved"] is not None for i in incidents() if "Malware" in i["problem"])
print("malware detection/resolve checks passed.")


# --- cPHulk: below threshold stays quiet, at/above threshold alerts ---
# default WHM_CPHULK_THRESHOLD = 20
ev("cphulk", 5)
assert sent == [], "below threshold must not alert"
ev("cphulk", 25)
assert len(sent) == 1 and "cPHulk" in sent[0] and "25 failed login(s)" in sent[0], sent
print("cphulk threshold checks passed.")


# --- a dormant source (function unset) + None count never alert ---
before = len(db.list_incidents())
whm_security._evaluate(HOST, "modsec", None, now=2000.0)  # None -> no-op
assert len(db.list_incidents()) == before

with patch.dict(os.environ, {}, clear=False):
    # no WHM_*_FUNCTION set -> check_all is a no-op even with servers present
    with patch("whm_health.servers", return_value=[("x", "y")]):
        whm_security.check_all()  # must not raise, must not alert
print("dormant-source no-op checks passed.")

os.remove(db.DB_PATH)
print("All whm_security checks passed.")
