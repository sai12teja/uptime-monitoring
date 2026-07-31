"""Email alerts on incident open/resolve (PRD §14).

SMTP provider/credentials are an open question (§19) and must never live in
code (§16) — read from environment variables, log-and-skip if unset rather
than crash the scheduler over a missing secret (§15: "log the failure").
"""
import os
import smtplib
import time
from email.message import EmailMessage

import db


def _fmt_ts(ts):
    """Local time — whoever reads the alert is in the same timezone as the
    box being monitored. None when the caller has no timestamp to give."""
    if ts is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts))


def _compose(headline, fields):
    """Headline, then an aligned label/value block, then the dashboard link.

    Fields with an empty value are dropped rather than printed blank, so a
    caller that has no URL or timestamp simply produces a shorter mail.
    DASHBOARD_URL is read per call (not at import) so it can be set after
    this module is imported; unset means the link is omitted entirely
    rather than emitting a localhost URL that's useless from an inbox.
    """
    rows = [(label, value) for label, value in fields if value]
    width = max((len(label) for label, _ in rows), default=0)
    lines = [headline, ""] + [f"{(label + ':').ljust(width + 1)} {value}" for label, value in rows]
    dashboard = os.environ.get("DASHBOARD_URL")
    if dashboard:
        lines += ["", f"Dashboard: {dashboard}"]
    return "\n".join(lines)


def format_down(monitor_name, detail, url=None, ts=None):
    subject = f"[DOWN] {monitor_name} — {detail}"
    body = _compose(f"{monitor_name} is DOWN.", [
        ("Problem", detail),
        ("Target", url),
        ("Detected", _fmt_ts(ts)),
    ])
    return subject, body


def format_recovered(monitor_name, downtime, url=None, ts=None):
    subject = f"[RECOVERED] {monitor_name} — {downtime} downtime"
    body = _compose(f"{monitor_name} has RECOVERED.", [
        ("Total downtime", downtime),
        ("Target", url),
        ("Recovered", _fmt_ts(ts)),
    ])
    return subject, body


def format_alert(target_name, detail, severity, url=None, ts=None):
    subject = f"[{severity.upper()}] {target_name} — {detail}"
    body = _compose(f"{target_name} alert ({severity}).", [
        ("Problem", detail),
        ("Target", url),
        ("Detected", _fmt_ts(ts)),
    ])
    return subject, body


def format_server_alert(detail, severity, ts=None):
    return format_alert("Server", detail, severity, ts=ts)


def send(subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"[email_alerts] SMTP_HOST not set, skipping: {subject}")
        return False

    settings = db.get_settings()
    if settings is not None and not settings["notify_enabled"]:
        print(f"[email_alerts] notifications muted, skipping: {subject}")
        return False

    to = os.environ.get("ALERT_TO", "")
    if settings is not None:
        emails = [e for e in (settings["email_1"], settings["email_2"], settings["email_3"]) if e]
        if emails:
            to = ", ".join(emails)

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("ALERT_FROM", "monitor@localhost")
        # A malformed saved address (e.g. a stray embedded newline) must not
        # take down every future alert -- ValueError here is as recoverable
        # as an SMTP failure, not a reason to crash the scheduler.
        msg["To"] = to
        msg.set_content(body)

        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as server:
            user = os.environ.get("SMTP_USER")
            if user:
                server.starttls()
                server.login(user, os.environ.get("SMTP_PASS", ""))
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError, ValueError) as e:
        print(f"[email_alerts] send failed: {e}")
        return False
