"""End-to-end email delivery check against a REAL local SMTP server, not a
mock: proves smtplib actually connects, speaks the protocol, and that a real
monitor going down/recovering produces a real delivered message.

test_email_alerts.py covers formatting + the skip/failure branches with
mocks; this one covers the wire.

Run: ./venv/Scripts/python.exe test_email_delivery.py
"""
import os
import socket
import threading
import time


class StubSMTP:
    """Minimal SMTP server on a random free port. Captures raw messages."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        f = conn.makefile("rb")

        def reply(line):
            conn.sendall(line.encode() + b"\r\n")

        reply("220 stub ESMTP")
        in_data, body = False, []
        while True:
            raw = f.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if in_data:
                if text == ".":
                    self.messages.append("\n".join(body))
                    body, in_data = [], False
                    reply("250 OK queued")
                else:
                    body.append(text)
                continue
            cmd = text.upper()
            if cmd.startswith(("EHLO", "HELO")):
                reply("250 stub")
            elif cmd.startswith("DATA"):
                reply("354 end with .")
                in_data = True
            elif cmd.startswith("QUIT"):
                reply("221 bye")
                break
            else:
                reply("250 OK")
        conn.close()

    def wait_for(self, count, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.messages) >= count:
                return True
            time.sleep(0.02)
        return False

    def stop(self):
        self.running = False
        self.sock.close()


smtp = StubSMTP()
os.environ["SMTP_HOST"] = "127.0.0.1"
os.environ["SMTP_PORT"] = str(smtp.port)
os.environ["ALERT_FROM"] = "monitor@rovix.test"
os.environ["ALERT_TO"] = "ops@rovix.test"
os.environ.pop("SMTP_USER", None)  # no auth against the stub

import db

db.DB_PATH = "test_email_delivery.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import email_alerts
import monitor_engine

# --- a plain send() actually reaches the server over a real socket ---
assert email_alerts.send("[TEST] subject line", "body text here") is True
assert smtp.wait_for(1), "no message arrived at the SMTP server"
msg = smtp.messages[0]
assert "Subject: [TEST] subject line" in msg, msg
assert "From: monitor@rovix.test" in msg, msg
assert "To: ops@rovix.test" in msg, msg
assert "body text here" in msg, msg

# --- a real monitor going DOWN delivers a real [DOWN] alert ---
# A second, healthy monitor keeps compute_is_correlated_outage() False --
# an all-down fleet is treated as one shared-cause outage and deliberately
# suppresses the individual alerts we're asserting on here.
mid = db.add_monitor("mailer-check", "https://mailer.invalid", "website")
other = db.add_monitor("healthy-peer", "https://peer.invalid", "website")
db.update_monitor_state(other, "up", 0, 2, time.time(), 10)

os.environ["DASHBOARD_URL"] = "https://rovix.example/dash"
incident_id = db.open_incident(mid, "mailer-check", "HTTP 500", "critical")
monitor_engine.handle_tick_events([("opened", "mailer-check", "HTTP 500", incident_id)])
assert smtp.wait_for(2), "down-transition did not send an email"
down_msg = smtp.messages[1]
assert "[DOWN] mailer-check" in down_msg, down_msg
assert "HTTP 500" in down_msg, down_msg
# enriched content: the failing target, when it was detected, where to look
assert "https://mailer.invalid" in down_msg, down_msg
assert "Time     :" in down_msg, down_msg
assert "Dashboard: https://rovix.example/dash" in down_msg, down_msg
# the timestamp must be the incident's own `started`, not send time
started = db.get_incident(incident_id)["started"]
assert time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) in down_msg, down_msg

# the email_sent event must also land on the incident timeline
kinds = [e["event_type"] for e in db.list_incident_events(incident_id)]
assert "email_sent" in kinds, kinds

# --- and the matching RECOVERED alert on the way back up ---
monitor_engine.handle_tick_events([("resolved", "mailer-check", "2m", incident_id)])
assert smtp.wait_for(3), "recovery did not send an email"
up_msg = smtp.messages[2]
assert "[RECOVERED] mailer-check" in up_msg, up_msg
assert "https://mailer.invalid" in up_msg, up_msg
assert "Reason   : Downtime 2m" in up_msg, up_msg

# --- DASHBOARD_URL unset => link omitted entirely, no broken localhost URL ---
del os.environ["DASHBOARD_URL"]
_, no_link_body, _ = email_alerts.format_down("x", "y", url="https://z.invalid", ts=time.time())
assert "Dashboard:" not in no_link_body, no_link_body
assert "https://z.invalid" in no_link_body, no_link_body

# --- a caller with no url/ts still produces a valid, shorter mail ---
_, bare, _ = email_alerts.format_down("x", "boom")
assert "Reason   : boom" in bare, bare
assert "Target" not in bare and "Time" not in bare, bare

# --- an unreachable SMTP host must log-and-skip, never crash the scheduler ---
os.environ["SMTP_PORT"] = "9"  # discard port, nothing listening
assert email_alerts.send("[TEST] unreachable", "body") is False
os.environ["SMTP_PORT"] = str(smtp.port)

# --- no SMTP_HOST configured => skip quietly, still no crash ---
saved = os.environ.pop("SMTP_HOST")
assert email_alerts.send("[TEST] no host", "body") is False
os.environ["SMTP_HOST"] = saved

smtp.stop()
os.remove(db.DB_PATH)
print(f"All email delivery checks passed ({len(smtp.messages)} messages delivered over a real socket).")
