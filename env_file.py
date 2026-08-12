"""Load .env into os.environ before anything reads a secret.

Deployment trap this exists to close: every secret here is read via
os.environ.get() with a silent fallback -- email_alerts.send() logs "SMTP_HOST
not set, skipping" and returns False rather than raising (§15). Set the vars
in an interactive shell and alerts work; start the same app from a service,
a cron entry, or a fresh terminal and they silently stop. A file the process
reads itself removes that dependency on how it was launched.

Stdlib only (no python-dotenv) -- this project's zero-new-dependency
convention, and the format needed here is a few lines of parsing.

Real environment variables always win: a container/systemd/CI that injects
SMTP_PASS must not be overridden by a stale committed-adjacent file.
"""
import os


def load(path=".env"):
    """Parse `path` and set any key not already in os.environ. Returns the
    list of names it set. Missing file is fine -- production may inject
    everything through the process environment instead."""
    if not os.path.exists(path):
        return []

    loaded = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            # Blank lines and comments. "export FOO=bar" is accepted too so a
            # file can double as something you can `source` from a shell.
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue

            value = value.strip()
            # Strip one layer of matching quotes -- a password with a trailing
            # space or a '#' is only expressible quoted, and the quotes
            # themselves must not end up in the credential.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]

            # Real env wins; never clobber what the platform injected.
            if key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded
