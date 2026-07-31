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
# Phosphor-terminal palette (matches the dashboard's own dark treatment).
_PHOSPHOR = "#39ff88"
_CORAL = "#ff4d5e"
_AMBER = "#ffb020"
_STATUS_COLOR = {"down": _CORAL, "recovered": _PHOSPHOR, "critical": _CORAL, "warning": _AMBER}
# The "scope" trace is monospace text art, not SVG or an image: Gmail strips
# inline SVG and blocks data: URIs, so a drawn waveform would render as a
# broken box for most recipients. Text art survives every client and fits
# the terminal aesthetic.
_TRACE = {
    "down": "_______________________________",
    "recovered": "___/\\______/\\______/\\______/\\___",
    "warning": "___/\\___/\\______/\\___/\\________",
}
_TRACE_NOTE = {
    "down": "NO SIGNAL",
    "recovered": "RHYTHM STABLE",
    "warning": "IRREGULAR",
}
_LABEL_WIDTH = 9  # fixed, not content-derived -- keeps every alert's field block
                   # aligned the same way regardless of which fields are present
_MONO = "'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"
_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _esc(value):
    # Monitor names, check details and URLs are all user-supplied via the Add
    # Monitor form -- they are untrusted input at this boundary and must never
    # be interpolated raw into markup, quote=True so they're also safe inside
    # an href="..." attribute.
    return _html.escape(str(value), quote=True)


def _html_rows(rows, accent):
    """Vitals grid -- one label/value pair per row, hairline-separated."""
    out = []
    for label, value in rows:
        text = str(value)
        if label == "Target" and text.startswith(("http://", "https://")):
            cell = (f'<a href="{_esc(text)}" style="color:{accent};text-decoration:none;'
                    f'border-bottom:1px dashed {accent};">{_esc(text)}</a>')
        else:
            cell = _esc(text)
        out.append(
            '<tr>'
            f'<td style="padding:12px 20px 12px 0;color:#6d8a80;font-size:10px;font-family:{_MONO};'
            'letter-spacing:.1em;text-transform:uppercase;white-space:nowrap;vertical-align:top;'
            f'border-bottom:1px solid #131f30;">{_esc(label)}</td>'
            f'<td style="padding:12px 0;color:#d7e6df;font-size:13px;font-family:{_MONO};'
            f'word-break:break-word;border-bottom:1px solid #131f30;">{cell}</td>'
            '</tr>'
        )
    return "".join(out)


def _compose_html(icon, headline, accent, rows, dashboard, state, monitor_name):
    """Table-based, fully inline-styled dark "phosphor scope" markup.

    Everything a client needs travels inline in the document: mail clients
    strip <style> blocks, <script>, external fonts and remote images, and
    ignore CSS animation/position, so the live scanline/confetti/counters of
    the interactive design have no email equivalent. What carries over is
    the look -- dark panel, phosphor trace, mono vitals -- built only from
    tables, inline styles and text.
    """
    trace = _TRACE.get(state, _TRACE["down"])
    note = _TRACE_NOTE.get(state, "")
    state_word = {"down": "DOWN", "recovered": "RECOVERED", "warning": "DEGRADED"}.get(state, "ALERT")

    button = ""
    if dashboard:
        button = (
            '<tr><td style="padding:26px 24px 0 24px;">'
            f'<a href="{_esc(dashboard)}" style="display:inline-block;background:{accent};color:#04170c;'
            f'padding:13px 26px;border-radius:10px;text-decoration:none;font-size:13px;font-weight:700;'
            f'font-family:{_SANS};">Open Dashboard &rarr;</a></td></tr>'
        )

    return (
        f'<div style="background:#060a12;padding:26px 12px;font-family:{_SANS};">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:560px;margin:0 auto;width:100%;background:#0c131f;'
        'border:1px solid #17243a;border-radius:18px;overflow:hidden;">'

        # --- status bar ---
        '<tr><td style="background:#08101c;border-bottom:1px solid #17243a;padding:13px 20px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'
        f'<tr><td style="font-family:{_MONO};font-size:11px;color:#6d8a80;letter-spacing:.08em;">'
        f'ROVIX AI MONITORING</td>'
        f'<td align="right" style="font-family:{_MONO};font-size:11px;color:{accent};'
        f'letter-spacing:.08em;">&#9679; {state_word}</td></tr>'
        '</table></td></tr>'

        # --- scope trace ---
        '<tr><td style="background:#050b12;padding:22px 20px;text-align:center;">'
        f'<div style="font-family:{_MONO};font-size:20px;line-height:1.2;color:{accent};'
        f'letter-spacing:2px;white-space:nowrap;overflow:hidden;">{trace}</div>'
        f'<div style="font-family:{_MONO};font-size:10px;color:#6d8a80;letter-spacing:.18em;'
        f'padding-top:10px;">{note}</div>'
        '</td></tr>'

        # --- headline ---
        '<tr><td style="padding:26px 24px 4px 24px;">'
        f'<div style="font-size:19px;font-weight:700;color:#f2fff7;line-height:1.4;">'
        f'{icon} {_esc(headline)}</div></td></tr>'

        # --- vitals ---
        '<tr><td style="padding:14px 24px 0 24px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'
        f'{_html_rows(rows, accent)}'
        '</table></td></tr>'

        f'{button}'

        '<tr><td style="padding:26px 24px 24px 24px;text-align:center;'
        f'font-family:{_MONO};font-size:10px;color:#6d8a80;letter-spacing:.06em;">'
        '&mdash; Rovix AI Monitoring</td></tr>'
        '</table></div>'
    )


def _compose(icon, headline, status, fields, color=_CORAL, state="down", monitor_name=""):
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
    return "\n".join(lines), _compose_html(icon, headline, color, rows, dashboard, state, monitor_name)


def format_down(monitor_name, detail, url=None, ts=None):
    """Returns (subject, plain_text_body, html_body)."""
    subject = f"[DOWN] {monitor_name} — {detail}"
    body, html_body = _compose(_STATUS_ICON["down"], f"{monitor_name} is DOWN", "Down", [
        ("Reason", detail),
        ("Target", url),
        ("Time", _fmt_ts(ts)),
    ], color=_STATUS_COLOR["down"], state="down", monitor_name=monitor_name)
    return subject, body, html_body


def format_recovered(monitor_name, downtime, url=None, ts=None):
    subject = f"[RECOVERED] {monitor_name} — {downtime} downtime"
    body, html_body = _compose(_STATUS_ICON["recovered"], f"{monitor_name} is back UP", "Recovered", [
        ("Downtime", downtime),
        ("Target", url),
        ("Recovered", _fmt_ts(ts)),
    ], color=_STATUS_COLOR["recovered"], state="recovered", monitor_name=monitor_name)
    return subject, body, html_body


def format_alert(target_name, detail, severity, url=None, ts=None):
    subject = f"[{severity.upper()}] {target_name} — {detail}"
    icon = _STATUS_ICON.get(severity, _STATUS_ICON["critical"])
    color = _STATUS_COLOR.get(severity, _STATUS_COLOR["critical"])
    # critical shares the flatline trace with a hard down; warning gets the
    # irregular one -- both read at a glance without needing the label.
    state = "warning" if severity == "warning" else "down"
    body, html_body = _compose(icon, target_name, severity.capitalize(), [
        ("Reason", detail),
        ("Target", url),
        ("Time", _fmt_ts(ts)),
    ], color=color, state=state, monitor_name=target_name)
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
