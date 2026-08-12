"""TDD self-check for .env loading (env_file.load).

Why this is tested rather than eyeballed: every secret in this app is read
with os.environ.get() and a silent fallback, so a parsing bug doesn't crash
-- it just makes alerts quietly stop sending. The precedence rule (real env
wins) and quote stripping are the two places a wrong result is invisible.

Run: ./venv/Scripts/python.exe test_env_file.py
"""
import os
import tempfile

import env_file


def write(text):
    fd, path = tempfile.mkstemp(suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# --- basic key=value ---
p = write("SMTP_HOST=smtp.example.com\nSMTP_PORT=587\n")
os.environ.pop("SMTP_HOST", None)
os.environ.pop("SMTP_PORT", None)
env_file.load(p)
assert os.environ["SMTP_HOST"] == "smtp.example.com", os.environ.get("SMTP_HOST")
assert os.environ["SMTP_PORT"] == "587"
os.remove(p)

# --- comments, blank lines and `export ` prefix are all tolerated ---
p = write("# a comment\n\nexport ALERT_FROM=bot@example.com\n")
os.environ.pop("ALERT_FROM", None)
env_file.load(p)
assert os.environ["ALERT_FROM"] == "bot@example.com"
os.remove(p)

# --- quotes are stripped, so the credential never carries them ---
p = write("SMTP_PASS=\"pw with spaces\"\nAPI_KEY='single'\n")
os.environ.pop("SMTP_PASS", None)
os.environ.pop("API_KEY", None)
env_file.load(p)
assert os.environ["SMTP_PASS"] == "pw with spaces", repr(os.environ["SMTP_PASS"])
assert os.environ["API_KEY"] == "single"
os.remove(p)

# --- a value containing '=' keeps every character after the FIRST '=' ---
p = write("SESSION_SECRET_KEY=abc==def=\n")
os.environ.pop("SESSION_SECRET_KEY", None)
env_file.load(p)
assert os.environ["SESSION_SECRET_KEY"] == "abc==def=", repr(os.environ["SESSION_SECRET_KEY"])
os.remove(p)

# --- REAL ENV WINS: an injected secret is never clobbered by the file.
# This is the security-relevant case -- a container sets the live password,
# a leftover .env must not silently downgrade it to a stale one. ---
os.environ["SMTP_PASS"] = "injected-by-platform"
p = write("SMTP_PASS=from-file\n")
loaded = env_file.load(p)
assert os.environ["SMTP_PASS"] == "injected-by-platform", os.environ["SMTP_PASS"]
assert "SMTP_PASS" not in loaded, loaded
os.remove(p)
del os.environ["SMTP_PASS"]

# --- missing file is not an error: production may inject everything through
# the real environment and ship no .env at all ---
assert env_file.load("definitely-not-here.env") == []

# --- malformed lines are skipped, not fatal -- one typo must not stop the
# rest of the file (and the secrets after it) from loading ---
p = write("GARBAGE_NO_EQUALS\n=novalue\nGOOD_KEY=kept\n")
os.environ.pop("GOOD_KEY", None)
env_file.load(p)
assert os.environ["GOOD_KEY"] == "kept"
os.remove(p)
del os.environ["GOOD_KEY"]

print("All env-file checks passed.")
