"""Assert-based self-check for gap 5: REST API endpoints (PRD §11), exercised
via Flask's test client against a bare Flask app -- register_api doesn't care
whether the server is Dash's app.server or a plain Flask() instance.

Run: ./venv/Scripts/python.exe test_api.py
"""
import json
import os

import db

db.DB_PATH = "test_rovix_api.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

from flask import Flask

import api

server = Flask(__name__)
api.register_api(server)


def get_json(resp):
    return json.loads(resp.data)


# --- API key gate (PRD §16 gap 6): checked before any endpoint logic runs ---
unauth_client = server.test_client()
os.environ.pop("API_KEY", None)
r = unauth_client.get("/monitors")
assert r.status_code == 503  # no API_KEY configured -> fail closed

os.environ["API_KEY"] = "test-key-123"
r = unauth_client.get("/monitors")
assert r.status_code == 401  # configured, but no header sent

r = unauth_client.get("/monitors", headers={"X-API-Key": "wrong-key"})
assert r.status_code == 401

client = server.test_client()
client.environ_base["HTTP_X_API_KEY"] = os.environ["API_KEY"]

# --- POST /monitors: create ---
r = client.post("/monitors", json={"name": "Acme", "url": "https://acme.example", "type": "website"})
assert r.status_code == 201, r.status_code
created = get_json(r)
mid = created["id"]
assert created["name"] == "Acme"
assert created["active"] is True

r = client.post("/monitors", json={"name": "Bad"})  # missing url
assert r.status_code == 400

r = client.post("/monitors", json={"name": "Bad", "url": "ftp://nope"})
assert r.status_code == 400

# --- GET /monitors: list (active only) ---
r = client.get("/monitors")
assert r.status_code == 200
assert [m["id"] for m in get_json(r)] == [mid]

# --- PUT /monitors/{id}: edit, partial update preserves unspecified fields ---
r = client.put(f"/monitors/{mid}", json={"name": "Acme Corp", "interval_sec": 120})
assert r.status_code == 200
updated = get_json(r)
assert updated["name"] == "Acme Corp"
assert updated["url"] == "https://acme.example"
assert updated["interval_sec"] == 120

r = client.put("/monitors/999999", json={"name": "Ghost"})
assert r.status_code == 404

# --- GET /status: lightweight live view ---
r = client.get("/status")
assert r.status_code == 200
statuses = get_json(r)
assert statuses[0]["id"] == mid
assert statuses[0]["status"] == "awaiting"

# --- DELETE /monitors/{id}: soft-delete, idempotent, history preserved ---
r = client.delete(f"/monitors/{mid}")
assert r.status_code == 204
assert get_json(client.get("/monitors")) == []  # gone from the active list

r = client.delete(f"/monitors/{mid}")  # already inactive -> still 204, not an error
assert r.status_code == 204

r = client.delete("/monitors/999999")
assert r.status_code == 404

# --- incidents: filters + full detail/timeline ---
import time as _time

mid2 = db.add_monitor("Beta", "https://beta.example", "crm")
inc_id = db.open_incident(mid2, "Beta", "Unreachable: timeout", severity="critical")
db.record_incident_event(inc_id, _time.time(), "check_failure", "timeout")

r = client.get("/incidents")
assert r.status_code == 200
incs = get_json(r)
assert incs[0]["id"] == inc_id
assert incs[0]["target"] == "Beta"

assert len(get_json(client.get("/incidents?status=open"))) == 1
assert get_json(client.get("/incidents?status=resolved")) == []
assert [i["id"] for i in get_json(client.get(f"/incidents?target_id={mid2}"))] == [inc_id]

r = client.get(f"/incidents/{inc_id}")
assert r.status_code == 200
detail = get_json(r)
assert detail["id"] == inc_id
assert detail["target_id"] == mid2
kinds = [e["kind"] for e in detail["timeline"]]
assert kinds[0] == "opened"
assert "check_failure" in kinds  # gap 7: incident_events-sourced, not synthesized from checks

r = client.get("/incidents/999999")
assert r.status_code == 404

# --- POST /incidents/{id}/acknowledge: additive metadata on an open incident ---
r = client.post(f"/incidents/{inc_id}/acknowledge", json={"who": "sam"})
assert r.status_code == 200
assert get_json(r)["acknowledged_by"] == "sam"

# gap 7: the acknowledgment itself is now in the timeline too.
kinds = [e["kind"] for e in get_json(client.get(f"/incidents/{inc_id}"))["timeline"]]
assert kinds == ["opened", "check_failure", "acknowledged"], kinds

r = client.post(f"/incidents/{inc_id}/acknowledge", json={})
assert r.status_code == 400  # missing "who"

db.resolve_incident(mid2)
r = client.post(f"/incidents/{inc_id}/acknowledge", json={"who": "sam"})
assert r.status_code == 409  # can't acknowledge a resolved incident

r = client.post("/incidents/999999/acknowledge", json={"who": "sam"})
assert r.status_code == 404

# --- GET /targets/{id}/detail ---
r = client.get(f"/targets/{mid2}/detail")
assert r.status_code == 200
assert get_json(r)["target"]["id"] == mid2

r = client.get("/targets/999999/detail")
assert r.status_code == 404

# --- GET /server/metrics ---
r = client.get("/server/metrics")
assert r.status_code == 200
assert "cpu_pct" in get_json(r)

# --- TCP/DNS monitor types: url has no http(s):// scheme requirement, tcp needs a port ---
r = client.post("/monitors", json={"name": "db-server", "url": "10.0.0.5", "type": "tcp", "port": 5432})
assert r.status_code == 201, r.status_code
tcp_created = get_json(r)
assert tcp_created["port"] == 5432
tcp_mid = tcp_created["id"]

r = client.post("/monitors", json={"name": "db-server", "url": "10.0.0.5", "type": "tcp"})
assert r.status_code == 400  # missing port

r = client.post("/monitors", json={"name": "acme-dns", "url": "acme.example", "type": "dns"})
assert r.status_code == 201, r.status_code
assert get_json(r)["port"] is None

r = client.put(f"/monitors/{tcp_mid}", json={"port": 5433})
assert r.status_code == 200
assert get_json(r)["port"] == 5433

# --- push monitors: no url required, response exposes the push_token ---
r = client.post("/monitors", json={"name": "nightly-backup", "type": "push", "interval_sec": 3600})
assert r.status_code == 201, r.status_code
push_created = get_json(r)
assert push_created["push_token"], push_created
assert push_created["url"] == ""

os.remove(db.DB_PATH)
print("REST API (§11) endpoint checks: OK")
