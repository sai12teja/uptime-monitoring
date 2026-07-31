"""TDD self-check for email alerts (PRD §14).

Run: ./venv/Scripts/python.exe test_email_alerts.py
"""
import os
import smtplib
from unittest.mock import MagicMock, patch

import db

# Isolated throwaway DB, swapped in before anything (including send(), which
# reads db.get_settings() internally) runs -- send()'s ALERT_TO-fallback
# tests below assert no settings row exists, which would silently break if
# this ever ran against the real rovix.db and someone had saved real
# Settings-modal values there (exactly what happened once in dev testing).
db.DB_PATH = "test_email_alerts.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

from email_alerts import format_down, format_recovered, format_alert, format_server_alert, send

# Subject line format is specified exactly in §14: front-loaded severity +
# target so triage is possible from a phone lock-screen notification.
subject, body, html_body = format_down("clientsite.com", "HTTP 500")
assert subject == "[DOWN] clientsite.com — HTTP 500", subject
assert "clientsite.com" in body and "HTTP 500" in body

subject, body, html_body = format_recovered("clientsite.com", "12m")
assert subject == "[RECOVERED] clientsite.com — 12m downtime", subject
assert "clientsite.com" in body and "12m" in body

# §14 server examples: "[CRITICAL] Server — disk 96% full" / "[WARNING] Server — disk 92% full"
subject, body, html_body = format_server_alert("Disk 96% full", "critical")
assert subject == "[CRITICAL] Server — Disk 96% full", subject

subject, body, html_body = format_server_alert("Disk 92% full", "warning")
assert subject == "[WARNING] Server — Disk 92% full", subject

# format_alert: the general form format_server_alert is now a thin wrapper
# around — same [SEVERITY] target — detail shape, any target name (e.g. a
# specific site's SSL alert, not just "Server").
subject, body, html_body = format_alert("clientsite.com", "SSL certificate expires in 10 days", "warning")
assert subject == "[WARNING] clientsite.com — SSL certificate expires in 10 days", subject
assert format_server_alert("Disk 96% full", "critical") == format_alert("Server", "Disk 96% full", "critical")

print("All email_alerts format checks passed.")


# ---------- HTML alternative part ----------

_, text, html_body = format_down("clientsite.com", "HTTP 500",
                                  url="https://clientsite.com/health", ts=1700000000)

# Same facts as the text part, marked up.
assert "clientsite.com is DOWN" in html_body, html_body
assert "HTTP 500" in html_body
assert "Rovix AI Monitoring" in html_body
# Styling must be inline -- email clients strip <style> blocks and external CSS.
assert "<style" not in html_body.lower(), "inline styles only; clients strip <style> blocks"
assert 'style="' in html_body
# A URL target becomes a clickable link.
assert 'href="https://clientsite.com/health"' in html_body

# Severity drives the accent color: red for down, green for recovered, amber for warning.
assert "#dc2626" in html_body
_, _, up_html = format_recovered("clientsite.com", "12m")
assert "#16a34a" in up_html
_, _, warn_html = format_alert("clientsite.com", "SSL expiring", "warning")
assert "#d97706" in warn_html

# Monitor names/details are user-supplied via the Add Monitor form -- they are
# untrusted at this boundary and must be escaped, never interpolated raw into
# the markup (broken layout at best, script injection in the client at worst).
_, _, evil_html = format_down('<script>alert(1)</script>', 'a "quoted" & <b>bold</b> detail')
assert "<script>" not in evil_html, evil_html
assert "&lt;script&gt;" in evil_html
assert "&amp;" in evil_html and "&lt;b&gt;" in evil_html

# DASHBOARD_URL drives the button; unset means no button at all rather than a
# dead localhost link (same rule the text part follows).
with patch.dict("os.environ", {"DASHBOARD_URL": "https://dash.example"}, clear=True):
    _, _, linked = format_down("x", "y")
assert 'href="https://dash.example"' in linked and "Open Dashboard" in linked
with patch.dict("os.environ", {}, clear=True):
    _, _, unlinked = format_down("x", "y")
assert "Open Dashboard" not in unlinked

print("All email_alerts HTML checks passed.")


# ---------- send() — the SMTP I/O boundary ----------

# §16: credentials never in code — unconfigured (no SMTP_HOST) must not
# attempt a network call, just log-and-skip (§15: "log the failure").
with patch.dict("os.environ", {}, clear=True), patch("smtplib.SMTP") as mock_smtp:
    result = send("[DOWN] test — HTTP 500", "body")
    assert result is False
    mock_smtp.assert_not_called()

# Configured: connects to the right host/port, sends starttls+login when a
# user is set, and hands smtplib a message with the exact subject/to/from.
env = {
    "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "2525",
    "SMTP_USER": "alerts@example.com", "SMTP_PASS": "secret",
    "ALERT_FROM": "alerts@example.com", "ALERT_TO": "oncall@example.com",
}
with patch.dict("os.environ", env, clear=True), patch("smtplib.SMTP") as mock_smtp:
    server = MagicMock()
    mock_smtp.return_value.__enter__.return_value = server

    result = send("[DOWN] clientsite.com — HTTP 500", "body text")

    assert result is True
    mock_smtp.assert_called_once_with("smtp.example.com", 2525, timeout=10)
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("alerts@example.com", "secret")
    sent_msg = server.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "[DOWN] clientsite.com — HTTP 500"
    assert sent_msg["From"] == "alerts@example.com"
    assert sent_msg["To"] == "oncall@example.com"
    # No html_body passed -> stays a plain single-part text message, so a
    # caller that only has text is unaffected by the HTML feature.
    assert not sent_msg.is_multipart(), "text-only send must not become multipart"

# With html_body: a proper multipart/alternative carrying BOTH parts, so
# clients that can't render HTML still get the readable text version.
with patch.dict("os.environ", env, clear=True), patch("smtplib.SMTP") as mock_smtp:
    server = MagicMock()
    mock_smtp.return_value.__enter__.return_value = server

    assert send("[DOWN] x", "plain fallback", "<div>rich body</div>") is True

    sent_msg = server.send_message.call_args[0][0]
    assert sent_msg.is_multipart()
    assert sent_msg.get_content_type() == "multipart/alternative", sent_msg.get_content_type()
    types = {p.get_content_type() for p in sent_msg.iter_parts()}
    assert types == {"text/plain", "text/html"}, types
    assert "plain fallback" in sent_msg.get_body(("plain",)).get_content()
    assert "rich body" in sent_msg.get_body(("html",)).get_content()

# SMTP failure (e.g. auth rejected, connection refused) is caught and
# reported, not raised — a bad mail server must not crash the scheduler.
with patch.dict("os.environ", {"SMTP_HOST": "smtp.example.com"}, clear=True), \
     patch("smtplib.SMTP", side_effect=smtplib.SMTPConnectError(421, "refused")):
    result = send("[DOWN] test", "body")
    assert result is False

print("All email_alerts send() checks passed.")


# ---------- db.resolve_incident() downtime (needed for the recovery email's
# "total downtime", §14) ----------

mid = db.add_monitor("downtime-test.invalid", "https://downtime-test.invalid", "website")
db.open_incident(mid, "downtime-test.invalid", "HTTP 500")

downtime_sec = db.resolve_incident(mid)
assert downtime_sec is not None and 0 <= downtime_sec < 5, downtime_sec

# No open incident for this monitor -> None, not a crash.
assert db.resolve_incident(mid) is None

os.remove(db.DB_PATH)
print("All db.resolve_incident downtime checks passed.")
