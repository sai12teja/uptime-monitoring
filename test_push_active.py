"""#9: a soft-deleted push monitor must stop accepting check-ins -- its token
must not resolve once the monitor is deactivated.

Run: ./venv/Scripts/python.exe test_push_active.py
"""
import os

import db

db.DB_PATH = "test_push_active.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("beat", "", "push")
token = db.get_monitor(mid)["push_token"]

assert db.get_monitor_by_push_token(token) is not None  # active -> accepts pings

db.deactivate_monitor(mid)  # soft-delete
assert db.get_monitor_by_push_token(token) is None, "deleted push monitor must not accept pings"

os.remove(db.DB_PATH)
print("push-token active-filter checks passed.")
