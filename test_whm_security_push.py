"""Push-based WHM security reporting: a root-owned agent on the server POSTs
its own Imunify360 / cPHulk / ModSecurity counts to /whm-security, and Rovix
runs the same threshold/incident/alert/dedup/resolve logic as the pull path --
with no WHM credentials on the Rovix side. Mocked email; no real network.

Run: ./venv/Scripts/python.exe test_whm_security_push.py
"""
import json
import os
from unittest.mock import patch

import db

db.DB_PATH = "test_whm_security_push.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import email_alerts
import whm_security

HOST = "root.userwhm.com"
sent = []


def ingest(counts):
    sent.clear()
    with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
        return whm_security.ingest_push(HOST, counts, now=1000.0 + len(db.list_incidents()))


def host_incidents():
    return [i for i in db.list_incidents() if i["target_name"] == HOST]


# --- validation: bad input is rejected whole, never alerts ---
for bad, why in [
    (None, "non-dict"), ({}, "empty"),
    ({"imunify": -1}, "negative"), ({"imunify": True}, "bool"),
    ({"imunify": 1.5}, "float"), ({"imunify": "2"}, "string"),
    ({"bogus": 1}, "unknown source"),
]:
    accepted, err = whm_security.ingest_push(HOST, bad, now=1.0)
    assert accepted is None and err, f"{why} should be rejected: {(accepted, err)}"
assert whm_security.ingest_push("", {"imunify": 1}, now=1.0)[1], "empty host rejected"
assert host_incidents() == [], "no incident from any rejected report"
print("validation checks passed.")

# --- malware: ANY Imunify detection is immediately critical ---
accepted, err = ingest({"imunify": 2})
assert err is None and accepted == ["imunify"], (accepted, err)
assert len(sent) == 1 and sent[0].startswith("[CRITICAL]") and "Malware" in sent[0], sent
assert len([i for i in host_incidents() if i["resolved"] is None]) == 1

# same detection again -> deduped, no repeat alert
accepted, err = ingest({"imunify": 2})
assert accepted == ["imunify"] and sent == [], sent

# cleaned -> resolve + one recovery email
ingest({"imunify": 0})
assert len(sent) == 1 and sent[0].startswith("[RECOVERED]"), sent
print("malware push checks passed.")

# --- cPHulk count threshold (default 20) ---
ingest({"cphulk": 5})
assert sent == [], "below threshold is quiet"
ingest({"cphulk": 25})
assert len(sent) == 1 and "cPHulk" in sent[0] and "25 failed login(s)" in sent[0], sent
print("cphulk push threshold checks passed.")

# --- multiple sources in one report ---
accepted, err = ingest({"imunify": 3, "modsec": 0})
assert set(accepted) == {"imunify", "modsec"} and err is None, (accepted, err)
print("multi-source push checks passed.")

# --- the HTTP endpoint: API-key gate + wiring ---
from flask import Flask

import api

server = Flask(__name__)
api.register_api(server)
os.environ["API_KEY"] = "k"
client = server.test_client()

# no key -> 401
r = client.post("/whm-security", json={"host": HOST, "counts": {"imunify": 0}})
assert r.status_code == 401, r.status_code

client.environ_base["HTTP_X_API_KEY"] = "k"
# bad body -> 400
r = client.post("/whm-security", json={"host": HOST, "counts": {"bogus": 1}})
assert r.status_code == 400 and "bogus" in json.loads(r.data)["error"], json.loads(r.data)
# valid -> 200 + accepted
r = client.post("/whm-security", json={"host": HOST, "counts": {"cphulk": 0, "modsec": 0}})
assert r.status_code == 200, (r.status_code, r.data)
assert set(json.loads(r.data)["accepted"]) == {"cphulk", "modsec"}, json.loads(r.data)
print("endpoint checks passed.")

os.remove(db.DB_PATH)
print("All whm_security push checks passed.")
