"""Assert-based self-check for gap 6: dashboard authentication (PRD §16).

Run: ./venv/Scripts/python.exe test_auth.py
"""
import os

import db

db.DB_PATH = "test_rovix_auth.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import auth
from flask import Flask

# --- password hashing ---
h = auth.hash_password("correct horse")
assert h != "correct horse"
assert auth.verify_password("correct horse", h)
assert not auth.verify_password("wrong", h)

# --- db.users CRUD ---
uid = db.add_user("sam", auth.hash_password("hunter2"))
row = db.get_user_by_username("sam")
assert row["id"] == uid
assert db.get_user_by_username("nope") is None
assert [u["username"] for u in db.list_users()] == ["sam"]

# --- session gate on a bare Flask app ---
server = Flask(__name__)
server.secret_key = "test-secret"
auth.register_auth(server)


@server.route("/protected")
def protected():
    return "secret content"


client = server.test_client()

# Unauthenticated -> redirected to /login
r = client.get("/protected")
assert r.status_code == 302
assert r.headers["Location"] == "/login"

# Wrong credentials -> 401, still on login page
r = client.post("/login", data={"username": "sam", "password": "wrong"})
assert r.status_code == 401
assert b"Invalid username or password" in r.data

r = client.get("/protected")
assert r.status_code == 302  # still not logged in

# Correct credentials -> redirected home, session set, protected route works
r = client.post("/login", data={"username": "sam", "password": "hunter2"})
assert r.status_code == 302
assert r.headers["Location"] == "/"

r = client.get("/protected")
assert r.status_code == 200
assert b"secret content" in r.data

# /login itself stays reachable without a session (no redirect loop)
client2 = server.test_client()
r = client2.get("/login")
assert r.status_code == 200

# Logout clears the session
r = client.get("/logout")
assert r.status_code == 302
assert r.headers["Location"] == "/login"

r = client.get("/protected")
assert r.status_code == 302

# --- push blueprint is exempt from the session gate, same as the api blueprint ---
import push as push_module

push_mid = db.add_monitor("nightly-backup", "", "push", interval_sec=60)
token = db.get_monitor(push_mid)["push_token"]

server2 = Flask(__name__)
server2.secret_key = "test-secret"
auth.register_auth(server2)
push_module.register_push(server2)

client3 = server2.test_client()  # no session at all
r = client3.get(f"/push/{token}")
assert r.status_code == 200, r.status_code  # not redirected to /login

os.remove(db.DB_PATH)
print("Dashboard authentication (§16 gap 6) checks: OK")
