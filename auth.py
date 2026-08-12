"""Dashboard authentication (PRD §16 gap 6). Session-cookie login gates every
Dash route; the REST API (api.py) is gated separately by its own API-key
check since it's meant to be called from scripts, not a logged-in browser.

Multiple named accounts, no self-serve signup (PRD doesn't ask for one) --
create accounts with manage_users.py.
"""
import html
import threading
import time

from flask import redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import db

LOGIN_PAGE = """<!doctype html>
<html><head><title>Rovix Uptime Monitoring — Log in</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #12161c; color: #e6e9ef;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  form {{ background: #1b2028; padding: 2rem; border-radius: 8px; min-width: 280px; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 1rem; }}
  label {{ display: block; font-size: 0.85rem; margin-bottom: 0.25rem; }}
  input {{ width: 100%; padding: 0.5rem; margin-bottom: 1rem; border-radius: 4px;
           border: 1px solid #333; background: #0f1319; color: #e6e9ef; box-sizing: border-box; }}
  button {{ width: 100%; padding: 0.5rem; border-radius: 4px; border: none;
            background: #3b82f6; color: white; cursor: pointer; }}
  .error {{ color: #f87171; font-size: 0.85rem; margin-bottom: 1rem; }}
</style></head>
<body>
  <form method="post" action="/login">
    <h1>Rovix Uptime Monitoring</h1>
    {error_html}
    <label for="username">Username</label>
    <input id="username" name="username" autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password">
    <button type="submit">Log in</button>
  </form>
</body></html>"""


MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 900  # 15 minutes

# username -> list of failed-attempt timestamps.
# ponytail: in-process dict, fine for this single-process deployment; move to
# the DB or Redis if the app is ever run under multiple workers, since each
# worker would otherwise keep its own independent counter.
_ATTEMPTS = {}
_ATTEMPTS_LOCK = threading.Lock()


def _recent_attempts(username, now=None):
    """Failed attempts still inside the lockout window. Prunes expired ones
    as a side effect, so the dict can't grow forever."""
    now = now or time.time()
    with _ATTEMPTS_LOCK:
        recent = [t for t in _ATTEMPTS.get(username, []) if now - t < LOCKOUT_SECONDS]
        if recent:
            _ATTEMPTS[username] = recent
        else:
            _ATTEMPTS.pop(username, None)
        return recent


def is_locked_out(username):
    return len(_recent_attempts(username)) >= MAX_LOGIN_ATTEMPTS


def record_failed_attempt(username):
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.setdefault(username, []).append(time.time())


def _reset_attempts(username):
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.pop(username, None)


PASSWORD_PAGE = """<!doctype html>
<html><head><title>Rovix Uptime Monitoring — Change password</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #12161c; color: #e6e9ef;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  form {{ background: #1b2028; padding: 2rem; border-radius: 8px; min-width: 320px; }}
  h1 {{ font-size: 1.1rem; margin: 0 0 0.25rem; }}
  .who {{ font-size: 0.8rem; color: #9aa3ad; margin-bottom: 1rem; }}
  label {{ display: block; font-size: 0.85rem; margin-bottom: 0.25rem; }}
  input {{ width: 100%; padding: 0.5rem; margin-bottom: 1rem; border-radius: 4px;
           border: 1px solid #333; background: #0f1319; color: #e6e9ef; box-sizing: border-box; }}
  button {{ width: 100%; padding: 0.5rem; border-radius: 4px; border: none;
            background: #3b82f6; color: white; cursor: pointer; }}
  .error {{ color: #f87171; font-size: 0.85rem; margin-bottom: 1rem; }}
  .ok {{ color: #4ade80; font-size: 0.85rem; margin-bottom: 1rem; }}
  .back {{ display: block; text-align: center; margin-top: 1rem; font-size: 0.85rem; color: #9aa3ad; }}
</style></head>
<body>
  <form method="post" action="/password">
    <h1>Change password</h1>
    <div class="who">Signed in as {username}</div>
    {message_html}
    <label for="current">Current password</label>
    <input id="current" name="current" type="password" autofocus>
    <label for="new">New password</label>
    <input id="new" name="new" type="password">
    <label for="confirm">Confirm new password</label>
    <input id="confirm" name="confirm" type="password">
    <button type="submit">Change password</button>
    <a class="back" href="/">← Back to dashboard</a>
  </form>
</body></html>"""

MIN_PASSWORD_LENGTH = 6


def _password_page(username, error=None, ok=None):
    message_html = ""
    if error:
        message_html = f'<div class="error">{html.escape(error)}</div>'
    elif ok:
        message_html = f'<div class="ok">{html.escape(ok)}</div>'
    # Username comes from the users table (operator-supplied at account
    # creation) -- escaped rather than trusted, same boundary rule the alert
    # emails apply to monitor names.
    return PASSWORD_PAGE.format(username=html.escape(username), message_html=message_html)


def hash_password(password):
    return generate_password_hash(password)


def verify_password(password, password_hash):
    return check_password_hash(password_hash, password)


def _login_page(error=None):
    error_html = f'<div class="error">{error}</div>' if error else ""
    return LOGIN_PAGE.format(error_html=error_html)


def register_auth(server):
    @server.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return _login_page()
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        # Checked BEFORE verifying the password: a locked account must cost an
        # attacker the full window regardless of whether they guessed right.
        if is_locked_out(username):
            return _login_page(
                error=f"Too many failed attempts. Try again in "
                      f"{LOCKOUT_SECONDS // 60} minutes."), 429

        user = db.get_user_by_username(username)
        if user is None or not verify_password(password, user["password_hash"]):
            record_failed_attempt(username)
            remaining = MAX_LOGIN_ATTEMPTS - len(_recent_attempts(username))
            hint = f" {remaining} attempt{'s' if remaining != 1 else ''} remaining." if remaining > 0 else ""
            # Same message whether the user exists or not -- distinguishing
            # them would confirm valid usernames to an attacker.
            return _login_page(error=f"Invalid username or password.{hint}"), 401

        _reset_attempts(username)
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        return redirect("/")

    @server.route("/password", methods=["GET", "POST"])
    def change_password():
        user_id = session.get("user_id")
        if not user_id:
            return redirect("/login")
        user = db.get_user_by_id(user_id)
        if user is None:
            # Account deleted out from under a live session.
            session.clear()
            return redirect("/login")

        username = user["username"]
        if request.method == "GET":
            return _password_page(username)

        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")

        # Re-check the current password even though the session is already
        # authenticated: it stops an unattended logged-in browser from being
        # used to take the account over.
        if not verify_password(current, user["password_hash"]):
            return _password_page(username, error="Current password is incorrect."), 401
        if len(new) < MIN_PASSWORD_LENGTH:
            return _password_page(
                username,
                error=f"New password must be at least {MIN_PASSWORD_LENGTH} characters."), 400
        if new != confirm:
            return _password_page(username, error="New passwords do not match."), 400
        if new == current:
            return _password_page(username, error="New password must differ from the current one."), 400

        db.set_user_password(user_id, hash_password(new))
        return _password_page(username, ok="Password changed.")

    @server.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @server.before_request
    def require_login():
        if request.path in ("/login", "/logout") or request.blueprint in ("api", "push"):
            return None
        if not session.get("user_id"):
            return redirect("/login")
        return None
