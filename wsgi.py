"""Gunicorn/Docker entrypoint.

Importing app.py as a plain module (what "gunicorn app:server" does) runs
everything at module level but SKIPS app.py's `if __name__ == "__main__":`
block entirely -- __name__ is "app" when imported, never "__main__". That
block is not incidental: it sets the session secret key, starts
monitor_engine's background check scheduler, and kicks off the favicon
backfill thread. Without it the app would still SERVE PAGES, but no monitor
would ever actually be checked and every login session would reset on
restart -- a much quieter failure than a crash, and worse.

This file does exactly what that block does, then exposes `server` (Dash's
underlying Flask app) for gunicorn to find via "gunicorn wsgi:server".
app.py itself is unchanged -- Windows dev and the systemd deployment both
still use its own __main__ block directly.
"""
import threading

import app as app_module
import data
import monitor_engine

app_module.app.server.secret_key = app_module._load_or_create_secret_key()
monitor_engine.start_background_scheduler(debug=False)
threading.Thread(target=data.backfill_favicons, daemon=True).start()

server = app_module.app.server
