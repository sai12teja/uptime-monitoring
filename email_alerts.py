"""Email alerts on incident open/resolve (PRD §14).

SMTP provider/credentials are an open question (§19) and must never live in
code (§16) — read from environment variables, log-and-skip if unset rather
than crash the scheduler over a missing secret (§15: "log the failure").
"""
import html as _html
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


_STATUS_ICON = {"down": "🔴", "recovered": "🟢", "critical": "🔴", "warning": "🟡"}
_STATUS_COLOR = {"down": "#dc2626", "recovered": "#16a34a", "critical": "#dc2626", "warning": "#d97706"}
_LABEL_WIDTH = 9  # fixed, not content-derived -- keeps every alert's field block
                   # aligned the same way regardless of which fields are present


def _esc(value):
    # Monitor names, check details and URLs are all user-supplied via the Add
    # Monitor form -- they are untrusted input at this boundary and must never
    # be interpolated raw into markup, quote=True so they're also safe inside
    # an href="..." attribute.
    return _html.escape(str(value), quote=True)


def _html_rows(rows):
    out = []
    for label, value in rows:
        text = str(value)
        if label == "Target" and text.startswith(("http://", "https://")):
            cell = f'<a href="{_esc(text)}" style="color:#2563eb;text-decoration:none;">{_esc(text)}</a>'
        else:
            cell = _esc(text)
        out.append(
            '<tr>'
            '<td style="padding:10px 20px 10px 0;color:#6b7280;font-size:13px;'
            f'white-space:nowrap;vertical-align:top;border-bottom:1px solid #f3f4f6;">{_esc(label)}</td>'
            '<td style="padding:10px 0;color:#111827;font-size:14px;font-weight:500;'
            f'word-break:break-word;border-bottom:1px solid #f3f4f6;">{cell}</td>'
            '</tr>'
        )
    return "".join(out)


def _compose_html(icon, headline, color, rows, dashboard):
    """Table-based, fully inline-styled markup -- email clients strip <style>
    blocks and external CSS/images, so everything a client needs to render
    this has to travel inline in the document itself.
    """
    button = ""
    if dashboard:
        button = (
            '<tr><td style="padding:28px 32px 0 32px;">'
            f'<a href="{_esc(dashboard)}" style="display:inline-block;background:#111827;color:#ffffff;'
            'padding:12px 24px;border-radius:6px;text-decoration:none;font-size:14px;font-weight:600;">'
            'Open Dashboard</a></td></tr>'
        )

    return (
        '<div style="background:#f3f4f6;padding:24px 12px;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:600px;margin:0 auto;width:100%;background:#ffffff;border-radius:10px;'
        'overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">'
        f'<tr><td style="background:{color};height:6px;line-height:6px;font-size:0;">&nbsp;</td></tr>'
        '<tr><td style="padding:32px 32px 8px 32px;">'
        f'<div style="font-size:20px;font-weight:700;color:#111827;line-height:1.4;">{icon} {_esc(headline)}</div>'
        '</td></tr>'
        '<tr><td style="padding:16px 32px 0 32px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'
        f'{_html_rows(rows)}'
        '</table></td></tr>'
        f'{button}'
        '<tr><td style="padding:28px 32px 32px 32px;color:#9ca3af;font-size:12px;">'
        '— Rovix AI Monitoring</td></tr>'
        '</table></div>'
    )


def _compose(icon, headline, status, fields, color="#dc2626"):
    """Returns (plain_text, html) for the same alert.

    Fields with an empty value are dropped rather than printed blank, so a
    caller that has no URL or timestamp simply produces a shorter mail.
    DASHBOARD_URL is read per call (not at import) so it can be set after
    this module is imported; unset means the link is omitted entirely
    rather than emitting a localhost URL that's useless from an inbox.
    """
    rows = [("Status", status)] + [(label, value) for label, value in fields if value]
    lines = [f"{icon} ALERT: {headline}", ""] + [f"{label.ljust(_LABEL_WIDTH)}: {value}" for label, value in rows]
    dashboard = os.environ.get("DASHBOARD_URL")
    if dashboard:
        lines += ["", f"Dashboard: {dashboard}"]
    lines += ["", "— Rovix AI Monitoring"]
    return "\n".join(lines), _compose_html(icon, headline, color, rows, dashboard)


def format_down(monitor_name, detail, url=None, ts=None):
    """Returns (subject, plain_text_body, html_body)."""
    subject = f"[DOWN] {monitor_name} — {detail}"
    body, html_body = _compose(_STATUS_ICON["down"], f"{monitor_name} is DOWN", "Down", [
        ("Reason", detail),
        ("Target", url),
        ("Time", _fmt_ts(ts)),
    ], color=_STATUS_COLOR["down"])
    return subject, body, html_body


def format_recovered(monitor_name, downtime, url=None, ts=None):
    subject = f"[RECOVERED] {monitor_name} — {downtime} downtime"
    body, html_body = _compose(_STATUS_ICON["recovered"], f"{monitor_name} is back UP", "Recovered", [
        ("Reason", f"Downtime {downtime}"),
        ("Target", url),
        ("Time", _fmt_ts(ts)),
    ], color=_STATUS_COLOR["recovered"])
    return subject, body, html_body


def format_alert(target_name, detail, severity, url=None, ts=None):
    subject = f"[{severity.upper()}] {target_name} — {detail}"
    icon = _STATUS_ICON.get(severity, _STATUS_ICON["critical"])
    color = _STATUS_COLOR.get(severity, _STATUS_COLOR["critical"])
    body, html_body = _compose(icon, target_name, severity.capitalize(), [
        ("Reason", detail),
        ("Target", url),
        ("Time", _fmt_ts(ts)),
    ], color=color)
    return subject, body, html_body


def format_server_alert(detail, severity, ts=None):
    return format_alert("Server", detail, severity, ts=ts)


def send(subject, body, html_body=None):
    """`body` is the plain-text part; `html_body`, when given, is attached as
    a multipart/alternative HTML part. Optional so a caller with only text
    (and the send()-level tests) still works unchanged -- clients that can't
    or won't render HTML fall back to the text part automatically.
    """
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
        if html_body:
            msg.add_alternative(html_body, subtype="html")

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
