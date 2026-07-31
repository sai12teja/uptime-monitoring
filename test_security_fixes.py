"""TDD self-check for two low-severity security-review fixes:

L1 - the Flask session secret must persist across restarts (via a local
     key file) when SESSION_SECRET_KEY isn't set, instead of regenerating
     randomly every time and silently logging everyone out.
L2 - the displayed push URL must prefer DASHBOARD_URL over the incoming
     Host header, which is spoofable behind an unpinned reverse proxy.

Run: ./venv/Scripts/python.exe test_security_fixes.py
"""
import os
from unittest.mock import patch

import db

db.DB_PATH = "test_security_fixes.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data
import app

# ---------- L1: session secret persistence ----------

key_path = app.SESSION_KEY_PATH
if os.path.exists(key_path):
    os.remove(key_path)

with patch.dict("os.environ", {}, clear=True):
    first = app._load_or_create_secret_key()
    assert os.path.exists(key_path), "no SESSION_SECRET_KEY -> must persist a generated key to disk"

    second = app._load_or_create_secret_key()
    assert second == first, "a second call (simulating a restart) must reuse the same persisted key"

with patch.dict("os.environ", {"SESSION_SECRET_KEY": "explicit-env-key"}, clear=True):
    assert app._load_or_create_secret_key() == "explicit-env-key", "an explicit env var must win over the file"

os.remove(key_path)
print("All session-secret persistence checks passed.")


# ---------- L2: push URL prefers DASHBOARD_URL over the Host header ----------

push_id = data.add_target("push test", "", "push", interval_sec=86400)

with app.app.server.test_request_context("/", base_url="http://attacker.example"):
    with patch.dict("os.environ", {}, clear=True):
        content = app.build_detail_content(push_id)
        rendered = str(content)
        assert "http://attacker.example/push/" in rendered, "no DASHBOARD_URL set -> falls back to Host header (old behavior)"

    with patch.dict("os.environ", {"DASHBOARD_URL": "https://dashboard.trusted.example"}, clear=True):
        content = app.build_detail_content(push_id)
        rendered = str(content)
        assert "attacker.example" not in rendered, "a spoofed Host header must not leak into the displayed push URL"
        assert "https://dashboard.trusted.example/push/" in rendered

print("All push-URL Host-header checks passed.")

os.remove(db.DB_PATH)
print("All security-fix checks passed.")
