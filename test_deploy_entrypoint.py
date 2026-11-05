"""#1: the systemd unit must launch gunicorn against wsgi:server, not
app:server. Importing app.py as a module skips its `__main__` block, so the
background check scheduler and the persisted session key never start (see
wsgi.py) -- the dashboard loads but nothing is ever checked.

Run: ./venv/Scripts/python.exe test_deploy_entrypoint.py
"""
from pathlib import Path

unit = Path(__file__).with_name("deploy") / "rovixdashboard.service"
text = unit.read_text(encoding="utf-8")

assert "wsgi:server" in text, "systemd unit must launch wsgi:server"
assert "app:server" not in text, "app:server skips the scheduler -- see wsgi.py"

print("test_deploy_entrypoint passed.")
