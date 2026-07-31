"""Assert-based self-check for gap 4: keyword flows from data.add_target into storage (§7.1).

Run: ./venv/Scripts/python.exe test_crm_keyword.py
"""
import os

import db

db.DB_PATH = "test_rovix_keyword.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)  # a previous failed run may have left one behind
db.init_db()

import data

# No keyword given -> stays None (must not regress existing website monitors).
no_kw_id = data.add_target("acme", "https://acme.example", "website")
assert db.get_monitor(no_kw_id)["keyword"] is None

# Keyword given -> stored and retrievable.
kw_id = data.add_target("crm login", "https://crm.example/login", "crm", keyword="Sign in")
assert db.get_monitor(kw_id)["keyword"] == "Sign in"

os.remove(db.DB_PATH)
print("data.add_target keyword pass-through: OK")
