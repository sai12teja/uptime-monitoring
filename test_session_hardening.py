"""#6: session-cookie hardening + a stronger minimum password length.

Run: ./venv/Scripts/python.exe test_session_hardening.py
"""
import auth

# A 6-char minimum is too weak; require at least 12.
assert auth.MIN_PASSWORD_LENGTH >= 12, auth.MIN_PASSWORD_LENGTH

# Importing app wires the cookie config onto the Flask server.
import app

cfg = app.app.server.config
assert cfg["SESSION_COOKIE_HTTPONLY"] is True, cfg["SESSION_COOKIE_HTTPONLY"]
# SameSite=Lax stops the session cookie from riding a cross-site POST -- the
# CSRF vector against the /login and /password forms.
assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax", cfg["SESSION_COOKIE_SAMESITE"]
# Secure is opt-in (env flag) so plain-HTTP dev / the current :5000 deploy still
# work; it defaults off.
assert cfg["SESSION_COOKIE_SECURE"] is False, cfg["SESSION_COOKIE_SECURE"]

print("session hardening checks passed.")
