"""TDD self-check for password change + login rate limiting.

Both are security boundaries, so they get real assertions rather than a
manual click-through: a silent failure here means either nobody can change
their password, or an attacker gets unlimited guesses at one.

Run: ./venv/Scripts/python.exe test_password_change.py
"""
import os
import time

import db

db.DB_PATH = "test_password_change.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import auth

# --- db.set_user_password: replaces the stored hash ---
uid = db.add_user("alice", auth.hash_password("original-pw"))
row = db.get_user_by_username("alice")
assert auth.verify_password("original-pw", row["password_hash"])

db.set_user_password(uid, auth.hash_password("new-pw"))
row = db.get_user_by_username("alice")
assert auth.verify_password("new-pw", row["password_hash"]), "new password must verify"
assert not auth.verify_password("original-pw", row["password_hash"]), "old password must stop working"

# The stored value is a HASH, never the plaintext -- a dashboard DB leak must
# not hand over usable credentials.
assert "new-pw" not in row["password_hash"], row["password_hash"]

# --- rate limiting: N failures locks the account, success resets ---
auth._reset_attempts("bob")
for i in range(auth.MAX_LOGIN_ATTEMPTS):
    assert not auth.is_locked_out("bob"), f"locked too early at attempt {i}"
    auth.record_failed_attempt("bob")
assert auth.is_locked_out("bob"), "must lock out after MAX_LOGIN_ATTEMPTS failures"

# A different user is unaffected -- lockout is per-username, so one attacker
# hammering "admin" can't lock out everyone else.
assert not auth.is_locked_out("carol")

# A successful login clears the counter.
auth._reset_attempts("bob")
assert not auth.is_locked_out("bob")

# --- lockout expires so a legitimate user isn't locked out forever ---
auth._reset_attempts("dave")
for _ in range(auth.MAX_LOGIN_ATTEMPTS):
    auth.record_failed_attempt("dave")
assert auth.is_locked_out("dave")
# Rewind every recorded attempt past the window.
auth._ATTEMPTS["dave"] = [t - auth.LOCKOUT_SECONDS - 1 for t in auth._ATTEMPTS["dave"]]
assert not auth.is_locked_out("dave"), "lockout must expire after LOCKOUT_SECONDS"

# --- attempts outside the window don't accumulate into a lockout ---
auth._reset_attempts("erin")
auth._ATTEMPTS["erin"] = [time.time() - auth.LOCKOUT_SECONDS - 10] * (auth.MAX_LOGIN_ATTEMPTS * 2)
assert not auth.is_locked_out("erin"), "stale attempts must not count"

os.remove(db.DB_PATH)
print("All password-change + rate-limit checks passed.")
