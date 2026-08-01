"""Dashboard authentication (PRD §16 gap 6). Session-cookie login gates every
Dash route; the REST API (api.py) is gated separately by its own API-key
check since it's meant to be called from scripts, not a logged-in browser.

Multiple named accounts, no self-serve signup (PRD doesn't ask for one) --
create accounts with manage_users.py.
"""
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
        user = db.get_user_by_username(username)
        if user is None or not verify_password(password, user["password_hash"]):
            return _login_page(error="Invalid username or password"), 401
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        return redirect("/")

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
