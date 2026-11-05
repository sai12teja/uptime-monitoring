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

# Shared themed <head> for the two standalone auth pages. Injected via .format()
# as a VALUE, so its CSS/JS braces are plain (no doubling). Honors the same
# data-theme/localStorage the Dash app uses, applied before first paint.
_AUTH_HEAD = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<script>
(function () {
  try {
    var t = localStorage.getItem('rovix-theme');
    document.documentElement.setAttribute('data-theme', t === 'light' ? 'light' : 'dark');
  } catch (e) { document.documentElement.setAttribute('data-theme', 'dark'); }
})();
</script>
<style>
:root, :root[data-theme="dark"] {
  --bg:#0b0d12; --panel:#141822; --panel2:#1b2029; --text:#e6e8eb; --muted:#99a1ad;
  --border:#262b36; --accent:#4d8fe0; --accent2:#3f7bc7; --danger:#e5484d; --ok:#3fb27f;
  --glow:rgba(77,143,224,.22); --shadow:0 12px 32px rgba(0,0,0,.45);
}
:root[data-theme="light"] {
  --bg:#f6f7f9; --panel:#ffffff; --panel2:#f2f3f6; --text:#1a1d24; --muted:#5c6470;
  --border:#e4e7ec; --accent:#2f6fd0; --accent2:#21539f; --danger:#d92d20; --ok:#1f9d6b;
  --glow:rgba(47,111,208,.14); --shadow:0 16px 40px rgba(16,24,40,.14);
}
* { box-sizing: border-box; }
body {
  margin:0; min-height:100vh; color:var(--text); background:var(--bg);
  font-family:'IBM Plex Sans', system-ui, -apple-system, sans-serif;
}
.auth-split { display:flex; min-height:100vh; }
.auth-main { flex:1; display:flex; align-items:center; justify-content:center; padding:24px; }
.auth-card {
  width:100%; max-width:380px; background:var(--panel); border:1px solid var(--border);
  border-radius:14px; padding:32px; box-shadow:var(--shadow);
}
.auth-brand { display:flex; align-items:center; gap:10px; font-size:14px; color:var(--muted); margin-bottom:26px; }
.auth-brand b { color:var(--text); font-weight:600; }
.auth-logo, .auth-aside-logo {
  display:inline-flex; align-items:center; justify-content:center; font-weight:700; color:#fff;
  background:linear-gradient(145deg,var(--accent),var(--accent2));
}
.auth-logo { width:28px; height:28px; border-radius:8px; font-size:15px; }
h1 { font-size:22px; margin:0 0 4px; letter-spacing:-0.02em; }
.auth-sub { margin:0 0 18px; color:var(--muted); font-size:14px; }
label { display:block; font-size:13px; color:var(--muted); margin:14px 0 6px; }
input {
  width:100%; padding:10px 12px; border-radius:8px; border:1px solid var(--border);
  background:var(--panel2); color:var(--text); font-size:14px; font-family:inherit;
}
input:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--glow); }
button {
  width:100%; margin-top:22px; padding:11px; border-radius:8px; border:none; cursor:pointer;
  background:var(--accent); color:#fff; font-size:14px; font-weight:600; font-family:inherit;
  transition:background .15s ease;
}
button:hover { background:var(--accent2); }
.error { color:var(--danger); font-size:13px; margin:14px 0 0; }
.ok { color:var(--ok); font-size:13px; margin:14px 0 0; }
.who { font-size:13px; color:var(--muted); margin-bottom:6px; }
.back { display:block; text-align:center; margin-top:16px; font-size:13px; color:var(--muted); text-decoration:none; }
.back:hover { color:var(--text); }
.auth-aside {
  flex:1; display:none; align-items:center; justify-content:center; padding:48px;
  border-left:1px solid var(--border);
  background:
    radial-gradient(ellipse 720px 520px at 28% 8%, var(--glow), transparent 70%),
    linear-gradient(160deg, var(--panel), var(--bg));
}
.auth-aside-inner { max-width:380px; }
.auth-aside-logo { width:44px; height:44px; border-radius:12px; font-size:22px; margin-bottom:24px; }
.auth-aside-title { font-size:26px; font-weight:700; letter-spacing:-0.02em; line-height:1.2; margin-bottom:12px; }
.auth-aside-text { color:var(--muted); font-size:15px; line-height:1.55; }
@media (min-width: 900px) { .auth-aside { display:flex; } }
</style>
"""

LOGIN_PAGE = """<!doctype html>
<html><head><title>Rovix Uptime Monitoring — Log in</title>
{head}
</head>
<body>
  <div class="auth-split">
    <div class="auth-main">
      <form class="auth-card" method="post" action="/login">
        <div class="auth-brand"><span class="auth-logo">R</span><span>Rovix <b>Uptime Monitoring</b></span></div>
        <h1>Sign in</h1>
        <div class="auth-sub">Log in to your monitoring dashboard.</div>
        <label for="username">Username</label>
        <input id="username" name="username" autofocus>
        <label for="password">Password</label>
        <input id="password" name="password" type="password">
        {error_html}
        <button type="submit">Log in</button>
      </form>
    </div>
    <aside class="auth-aside">
      <div class="auth-aside-inner">
        <div class="auth-aside-logo">R</div>
        <div class="auth-aside-title">Know the moment anything goes down.</div>
        <div class="auth-aside-text">Uptime, SSL expiry, and server health — watched continuously, with alerts the second status changes.</div>
      </div>
    </aside>
  </div>
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
{head}
</head>
<body>
  <div class="auth-split">
    <div class="auth-main">
      <form class="auth-card" method="post" action="/password">
        <div class="auth-brand"><span class="auth-logo">R</span><span>Rovix <b>Uptime Monitoring</b></span></div>
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
    </div>
    <aside class="auth-aside">
      <div class="auth-aside-inner">
        <div class="auth-aside-logo">R</div>
        <div class="auth-aside-title">Keep your account secure.</div>
        <div class="auth-aside-text">Choose a strong, unique password — at least 12 characters that you don't use anywhere else.</div>
      </div>
    </aside>
  </div>
</body></html>"""

MIN_PASSWORD_LENGTH = 12


def _password_page(username, error=None, ok=None):
    message_html = ""
    if error:
        message_html = f'<div class="error">{html.escape(error)}</div>'
    elif ok:
        message_html = f'<div class="ok">{html.escape(ok)}</div>'
    # Username comes from the users table (operator-supplied at account
    # creation) -- escaped rather than trusted, same boundary rule the alert
    # emails apply to monitor names.
    return PASSWORD_PAGE.format(head=_AUTH_HEAD, username=html.escape(username), message_html=message_html)


def hash_password(password):
    return generate_password_hash(password)


def verify_password(password, password_hash):
    return check_password_hash(password_hash, password)


def _login_page(error=None):
    error_html = f'<div class="error">{error}</div>' if error else ""
    return LOGIN_PAGE.format(head=_AUTH_HEAD, error_html=error_html)


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
