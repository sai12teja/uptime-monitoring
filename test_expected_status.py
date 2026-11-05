"""④ Expected-status for API monitors: do_http_check treats a specific status
code as the success condition (so a normally-200 API is DOWN on a 500, and an
endpoint that correctly returns 401/403 is UP), across both the success path
and urllib's HTTPError path. Plus the db round-trip and the REST API
create/validate/edit/serialize. Mocked -- no real network.

Run: ./venv/Scripts/python.exe test_expected_status.py
"""
import json
import os
from unittest.mock import patch

import urllib.error

import db
import monitor_engine as me

db.DB_PATH = "test_expected_status.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()


# --- db round-trip: stored, read back, updatable ---
mid = db.add_monitor("api", "https://api.example", "website", expected_status=201)
assert db.get_monitor(mid)["expected_status"] == 201
db.update_monitor(mid, "api", "https://api.example", None, 600, expected_status=202)
assert db.get_monitor(mid)["expected_status"] == 202
print("db round-trip checks passed.")


# --- do_http_check honours expected_status (success path) ---
class _Resp:
    def __init__(self, status):
        self.status, self.headers = status, {}

    def read(self, n=None):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


BASE = {"url": "https://api.example", "keyword": None, "http_method": "GET",
        "http_body": None, "http_body_encoding": "json", "timeout_sec": None,
        "expected_status": None}


def check(monitor, resp=None, raise_err=None):
    with patch("ssrf_guard.resolve", return_value=set()), \
         patch("monitor_engine._GUARDED_OPENER.open",
               **({"side_effect": raise_err} if raise_err else {"return_value": resp})):
        return me.do_http_check(monitor)


# expected 200, got 200 -> up
ok, _, _ = check({**BASE, "expected_status": 200}, resp=_Resp(200))
assert ok is True

# expected 201, got 200 -> down, with a clear reason
ok, _, detail = check({**BASE, "expected_status": 201}, resp=_Resp(200))
assert ok is False and "expected 201" in detail, detail

# no expected_status set -> unchanged default (200 is up)
ok, _, _ = check(BASE, resp=_Resp(200))
assert ok is True

# --- expected_status on an ERROR code (urllib raises HTTPError first) ---
# expected 401, endpoint returns 401 -> UP (correctly-protected endpoint)
err401 = urllib.error.HTTPError("https://api.example", 401, "Unauthorized", {}, None)
ok, _, _ = check({**BASE, "expected_status": 401}, raise_err=err401)
assert ok is True, "an endpoint expected to return 401 must read as up"

# expected 200 but a 500 arrives -> DOWN
err500 = urllib.error.HTTPError("https://api.example", 500, "Server Error", {}, None)
ok, _, detail = check({**BASE, "expected_status": 200}, raise_err=err500)
assert ok is False and "500" in detail, detail
print("do_http_check expected_status checks passed.")


# --- REST API: create/validate/edit/serialize ---
from flask import Flask

import api

server = Flask(__name__)
api.register_api(server)
os.environ["API_KEY"] = "k"
client = server.test_client()
client.environ_base["HTTP_X_API_KEY"] = "k"


def j(r):
    return json.loads(r.data)


r = client.post("/monitors", json={"name": "a", "url": "https://a.test", "type": "website", "expected_status": 201})
assert r.status_code == 201 and j(r)["expected_status"] == 201, (r.status_code, j(r))
api_mid = j(r)["id"]

# out-of-range code -> 400
r = client.post("/monitors", json={"name": "b", "url": "https://b.test", "type": "website", "expected_status": 700})
assert r.status_code == 400 and "expected_status" in j(r)["error"], j(r)

# edit updates it
r = client.put(f"/monitors/{api_mid}", json={"expected_status": 204})
assert r.status_code == 200 and j(r)["expected_status"] == 204, j(r)

# edit WITHOUT it preserves the stored value
r = client.put(f"/monitors/{api_mid}", json={"name": "a2"})
assert j(r)["expected_status"] == 204, j(r)

# it also appears in the list serialization
assert any(m["expected_status"] == 204 for m in j(client.get("/monitors"))), "expected_status in list"
print("REST API expected_status checks passed.")

os.remove(db.DB_PATH)
print("All expected_status checks passed.")
