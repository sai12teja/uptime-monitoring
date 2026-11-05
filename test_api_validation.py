"""#7: the REST API validates untrusted input and never leaks push_token in the
bulk list.

Run: ./venv/Scripts/python.exe test_api_validation.py
"""
import json
import os

import db

db.DB_PATH = "test_api_validation.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

from flask import Flask

import api

server = Flask(__name__)
api.register_api(server)

os.environ["API_KEY"] = "k"
client = server.test_client()
client.environ_base["HTTP_X_API_KEY"] = "k"


def j(r):
    return json.loads(r.data)


base = {"name": "x", "url": "https://x.test", "type": "website"}


def bad(extra, field):
    r = client.post("/monitors", json={**base, **extra})
    assert r.status_code == 400, (extra, r.status_code, j(r))
    assert field in j(r)["error"], (extra, j(r))


bad({"type": "bogus"}, "type")                 # unknown monitor type
bad({"interval_sec": 1}, "interval_sec")       # would hammer the target every tick
bad({"interval_sec": "600"}, "interval_sec")   # string: would silently stop the monitor
bad({"http_method": "FETCH"}, "http_method")   # not a real method
bad({"timeout_sec": 9999}, "timeout_sec")      # out of range
bad({"retries": -1}, "retries")                # negative

# A valid create still works, and the single-resource response exposes the token.
r = client.post("/monitors", json={"name": "beat", "type": "push", "interval_sec": 3600})
assert r.status_code == 201, (r.status_code, j(r))
assert j(r)["push_token"], j(r)

# ...but the bulk list must NOT leak any push_token.
r = client.get("/monitors")
assert r.status_code == 200
assert all("push_token" not in m for m in j(r)), j(r)

os.remove(db.DB_PATH)
print("API validation + push_token-leak checks passed.")
