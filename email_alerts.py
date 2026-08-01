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


def _fmt_hms(ts):
    if ts is None:
        return None
    return time.strftime("%H:%M:%S", time.localtime(ts))


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


# Box-drawing trace for the premium card -- same "no SVG, no images"
# constraint as _TRACE above, just a sharper glyph set. "recovered" beats
# steadily; "down" flatlines into a break where the signal died.
_PREMIUM_TRACE = {
    "recovered": "──╱╲────╱╲────╱╲────╱╲────╱╲──",
    "down": "──╱╲────╱╲──✗────────────────",
}


def _premium_detail_rows(rows, accent):
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
            f'<td style="padding:12px 0;border-top:1px solid #131f33;font-family:{_MONO};font-size:10px;'
            'letter-spacing:1.5px;text-transform:uppercase;color:#6d8a80;width:130px;'
            f'vertical-align:top;white-space:nowrap;">{_esc(label)}</td>'
            f'<td style="padding:12px 0;border-top:1px solid #131f33;font-family:{_MONO};font-size:13px;'
            f'color:#d7e6df;word-break:break-word;">{cell}</td>'
            '</tr>'
        )
    return "".join(out)


def _premium_hero(cells):
    """Exactly 3 stat cells, hairline-divided -- the numbers a reader wants
    before the sentence, styled larger than everything else on the card."""
    out = []
    for i, (label, value_html) in enumerate(cells):
        border = "" if i == len(cells) - 1 else "border-right:1px solid #16233a;"
        pad_left = "0" if i == 0 else "18px"
        out.append(
            f'<td width="33%" valign="top" style="padding:2px 12px 14px {pad_left};{border}">'
            f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:1.5px;text-transform:uppercase;'
            f'color:#6d8a80;padding-bottom:8px;">{_esc(label)}</div>'
            f'<div style="font-family:{_MONO};font-size:22px;font-weight:700;color:#f2fff7;line-height:1.25;'
            f'word-break:break-word;">{value_html}</div></td>'
        )
    return (
        '<tr><td style="padding:22px 28px 8px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;"><tr>'
        f'{"".join(out)}'
        '</tr></table></td></tr>'
    )


def _premium_timeline(checks):
    """Real rows from db.checks -- never invented log lines. `checks` is a
    list of (ts, ok, detail) tuples, oldest first."""
    if not checks:
        return ""
    rows = []
    for ts, ok, detail in checks:
        dot = _PHOSPHOR if ok else _CORAL
        word_color = _PHOSPHOR if ok else "#b9c7c0"
        word = "UP" if ok else "DOWN"
        rows.append(
            '<tr>'
            f'<td valign="top" style="padding:6px 6px 6px 0;font-family:{_MONO};font-size:11px;color:{dot};">&#9679;</td>'
            f'<td valign="top" width="70" style="padding:6px 0;font-family:{_MONO};font-size:11.5px;'
            f'color:#3d5567;white-space:nowrap;">{_esc(_fmt_hms(ts))}</td>'
            f'<td valign="top" style="padding:6px 0;font-family:{_MONO};font-size:11.5px;color:{word_color};'
            f'word-break:break-word;">{word} &mdash; {_esc(detail)}</td>'
            '</tr>'
        )
    return (
        '<tr><td style="padding:8px 28px 4px;">'
        f'<div style="font-family:{_MONO};font-size:10px;letter-spacing:2px;text-transform:uppercase;'
        'color:#3d5567;padding-bottom:10px;">Recent checks</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'
        f'{"".join(rows)}'
        '</table></td></tr>'
    )


def _premium_card(state, monitor_name, hero, detail_rows, checks, note, dashboard):
    """The dark "phosphor scope" card with real hero stats, detail rows and
    a real recent-checks timeline -- used only when the caller has an
    actual monitor + incident to draw from (see format_down/format_recovered).
    A caller with no such context (a server-level alert, a correlated-outage
    summary) falls back to the simpler _compose_html card instead of this
    one rendering with blank/fabricated stats.

    Table-based, fully inline-styled: mail clients strip <style> blocks,
    <script>, external fonts, remote images and inline SVG, and ignore CSS
    animation, so nothing here depends on any of those.
    """
    accent = _STATUS_COLOR[state]
    recovered = state == "recovered"
    badge_icon = "&#10003;" if recovered else "&#9888;"
    badge_text = "Back online" if recovered else "Outage detected"
    trace = _PREMIUM_TRACE[state]
    signal = "RHYTHM STABLE" if recovered else "NO SIGNAL"
    headline = f"{_esc(monitor_name)} recovered" if recovered else f"{_esc(monitor_name)} is down"
    cta_label = "Open Dashboard" if recovered else "View Live Status"
    cta_ink = "#04170c" if recovered else "#1a050a"

    note_html = ""
    if note:
        note_html = (
            '<tr><td style="padding:16px 28px 0;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;'
            'background:#101823;border:1px solid #1c2a3d;border-radius:10px;"><tr>'
            f'<td style="padding:13px 16px;font-family:{_SANS};font-size:12.5px;line-height:1.5;'
            f'color:#b9c7c0;">{note}</td></tr></table></td></tr>'
        )

    button = ""
    if dashboard:
        button = (
            '<tr><td style="padding:22px 28px 28px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;"><tr>'
            f'<td align="center" bgcolor="{accent}" style="border-radius:10px;">'
            f'<a href="{_esc(dashboard)}" style="display:block;padding:14px 0;font-family:{_SANS};'
            f'font-size:14px;font-weight:700;color:{cta_ink};text-decoration:none;border-radius:10px;">'
            f'{cta_label} &rarr;</a></td></tr></table></td></tr>'
        )
    else:
        button = '<tr><td style="padding:8px 28px 24px;"></td></tr>'

    return (
        f'<div style="background:#05080f;padding:32px 12px;font-family:{_SANS};">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:600px;margin:0 auto;width:100%;">'

        # --- eyebrow ---
        '<tr><td style="padding:0 6px 12px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;"><tr>'
        f'<td align="left" style="font-family:{_MONO};font-size:11px;letter-spacing:2px;'
        f'text-transform:uppercase;color:#6d8a80;"><span style="color:{accent};">&#9679;</span>'
        '&nbsp; ROVIX AI MONITORING</td>'
        '</tr></table></td></tr>'

        # --- card ---
        '<tr><td style="background:#0b121d;border:1px solid #1a2740;border-radius:16px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'

        f'<tr><td style="background:{accent};height:4px;line-height:4px;font-size:0;'
        'border-radius:16px 16px 0 0;">&nbsp;</td></tr>'

        # scope strip
        '<tr><td style="background:#070d16;border-bottom:1px solid #16233a;padding:22px 24px 16px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;"><tr>'
        f'<td align="left" style="font-family:{_MONO};font-size:10px;letter-spacing:2px;color:#3d5567;">SIGNAL</td>'
        f'<td align="right" style="font-family:{_MONO};font-size:10px;letter-spacing:2px;color:{accent};">{signal}</td>'
        '</tr></table>'
        f'<div style="text-align:center;padding-top:12px;font-family:{_MONO};font-size:17px;'
        f'letter-spacing:2px;line-height:1.1;color:{accent};white-space:nowrap;overflow:hidden;">{trace}</div>'
        '</td></tr>'

        # headline
        '<tr><td style="padding:26px 28px 4px;">'
        f'<div style="font-family:{_MONO};font-size:11px;letter-spacing:2.5px;text-transform:uppercase;'
        f'color:{accent};padding-bottom:10px;">{badge_icon}&nbsp; {badge_text}</div>'
        f'<div style="font-family:{_SANS};font-size:23px;line-height:1.3;font-weight:700;color:#f2fff7;">'
        f'{headline}</div></td></tr>'

        f'{_premium_hero(hero)}'
        f'<tr><td style="padding:0 28px 0;">{_premium_detail_rows(detail_rows, accent)}</td></tr>'
        f'{_premium_timeline(checks)}'
        f'{note_html}'
        f'{button}'

        '</table></td></tr>'

        '<tr><td align="center" style="padding:16px 24px 0;font-family:' + _MONO + ';font-size:10.5px;'
        'letter-spacing:.5px;color:#3d5567;">&mdash; Rovix AI Monitoring &middot; phosphor never lies &mdash;'
        '</td></tr>'

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


def format_down(monitor_name, detail, url=None, ts=None, context=None):
    """Returns (subject, plain_text_body, html_body). The plain-text part is
    always the same fixed-width card. The HTML part gets the premium
    scope/hero/timeline card when `context` (real per-monitor data --
    monitor_engine._alert_context) is supplied, and the simpler card
    otherwise -- callers with no single monitor to draw from (a
    correlated-outage summary, an old caller in a test) never get
    fabricated stats.
    """
    subject = f"[DOWN] {monitor_name} — {detail}"
    body, simple_html = _compose(_STATUS_ICON["down"], f"{monitor_name} is DOWN", "Down", [
        ("Reason", detail),
        ("Target", url),
        ("Time", _fmt_ts(ts)),
    ], color=_STATUS_COLOR["down"], state="down", monitor_name=monitor_name)

    if context is None:
        return subject, body, simple_html

    fail_threshold = context["fail_threshold"]
    hero = [
        ("Reason", _esc(detail)),
        ("Failed checks", f"{fail_threshold}/{fail_threshold}"),
        ("Down since", _esc(_fmt_hms(ts)) or "—"),
    ]
    detail_rows = [
        ("Target", url),
        ("Detected at", _fmt_ts(ts)),
        ("Check type", f"{context['check_type']} check every {context['interval_sec']}s"),
    ]
    note = (f"<b style=\"color:#ffb020;\">Auto-recheck is running.</b> You&rsquo;ll get a recovery "
            f"email the moment {_esc(monitor_name)} responds again &mdash; no need to refresh.")
    html_body = _premium_card("down", monitor_name, hero, detail_rows, context["checks"], note,
                               os.environ.get("DASHBOARD_URL"))
    return subject, body, html_body


def format_recovered(monitor_name, downtime, url=None, ts=None, context=None):
    subject = f"[RECOVERED] {monitor_name} — {downtime} downtime"
    body, simple_html = _compose(_STATUS_ICON["recovered"], f"{monitor_name} is back UP", "Recovered", [
        ("Downtime", downtime), ("Target", url), ("Recovered", _fmt_ts(ts)),
    ], color=_STATUS_COLOR["recovered"], state="recovered", monitor_name=monitor_name)

    if context is None:
        return subject, body, simple_html

    response = context["response_ms"]
    hero = [
        ("Downtime", _esc(downtime)),
        ("Failed checks", str(context["fail_threshold"])),
        ("Response now", f"{response}<span style=\"font-size:13px;color:#6d8a80;\">ms</span>" if response is not None else "—"),
    ]
    detail_rows = [
        ("Target", url),
        ("Recovered at", _fmt_ts(ts)),
        ("Check type", f"{context['check_type']} check every {context['interval_sec']}s"),
    ]
    html_body = _premium_card("recovered", monitor_name, hero, detail_rows, context["checks"], None,
                               os.environ.get("DASHBOARD_URL"))
    return subject, body, html_body


def _digest_rows(items, accent):
    out = []
    for item in items:
        name = _esc(item["name"])
        detail = _esc(item["detail"])
        time_str = _esc(_fmt_hms(item.get("ts"))) or ""
        url = item.get("url")
        target_html = ""
        if url:
            target_html = (
                f'<div style="padding-top:4px;"><a href="{_esc(url)}" style="color:{accent};'
                f'text-decoration:none;border-bottom:1px dashed {accent};font-size:11.5px;'
                f'font-family:{_MONO};">{_esc(url)}</a></div>'
            )
        out.append(
            '<tr>'
            '<td style="padding:14px 0;border-top:1px solid #131f33;">'
            f'<div style="font-family:{_SANS};font-size:14px;font-weight:700;color:#f2fff7;">{name}</div>'
            f'<div style="font-family:{_MONO};font-size:12px;color:#b9c7c0;padding-top:4px;">{detail}</div>'
            f'{target_html}</td>'
            f'<td align="right" valign="top" style="padding:14px 0;border-top:1px solid #131f33;'
            f'font-family:{_MONO};font-size:11px;color:#6d8a80;white-space:nowrap;">{time_str}</td>'
            '</tr>'
        )
    return "".join(out)


def _premium_digest(state, items, dashboard):
    """Consolidated card for 2+ monitors that changed state in the same
    short debounce window -- e.g. a shared host going down takes several
    sites with it at once. Same dark phosphor aesthetic as _premium_card,
    but a scannable list of rows instead of one monitor's hero stats and
    recent-checks timeline, which don't generalize across different
    monitors' independent check histories. This is what stops a shared
    outage from flooding one email per affected site.
    """
    accent = _STATUS_COLOR[state]
    recovered = state == "recovered"
    n = len(items)
    badge_icon = "&#10003;" if recovered else "&#9888;"
    badge_text = "Back online" if recovered else "Outage detected"
    headline = f"{n} sites recovered" if recovered else f"{n} sites are down"
    cta_ink = "#04170c" if recovered else "#1a050a"

    button = ""
    if dashboard:
        button = (
            '<tr><td style="padding:22px 28px 28px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;"><tr>'
            f'<td align="center" bgcolor="{accent}" style="border-radius:10px;">'
            f'<a href="{_esc(dashboard)}" style="display:block;padding:14px 0;font-family:{_SANS};'
            f'font-size:14px;font-weight:700;color:{cta_ink};text-decoration:none;border-radius:10px;">'
            'Open Dashboard &rarr;</a></td></tr></table></td></tr>'
        )
    else:
        button = '<tr><td style="padding:8px 28px 24px;"></td></tr>'

    return (
        f'<div style="background:#05080f;padding:32px 12px;font-family:{_SANS};">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:600px;margin:0 auto;width:100%;">'

        '<tr><td style="padding:0 6px 12px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;"><tr>'
        f'<td align="left" style="font-family:{_MONO};font-size:11px;letter-spacing:2px;'
        f'text-transform:uppercase;color:#6d8a80;"><span style="color:{accent};">&#9679;</span>'
        '&nbsp; ROVIX AI MONITORING</td>'
        '</tr></table></td></tr>'

        '<tr><td style="background:#0b121d;border:1px solid #1a2740;border-radius:16px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'

        f'<tr><td style="background:{accent};height:4px;line-height:4px;font-size:0;'
        'border-radius:16px 16px 0 0;">&nbsp;</td></tr>'

        '<tr><td style="padding:26px 28px 4px;">'
        f'<div style="font-family:{_MONO};font-size:11px;letter-spacing:2.5px;text-transform:uppercase;'
        f'color:{accent};padding-bottom:10px;">{badge_icon}&nbsp; {badge_text}</div>'
        f'<div style="font-family:{_SANS};font-size:23px;line-height:1.3;font-weight:700;color:#f2fff7;">'
        f'{headline}</div></td></tr>'

        '<tr><td style="padding:8px 28px 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'
        f'{_digest_rows(items, accent)}'
        '</table></td></tr>'

        f'{button}'
        '</table></td></tr>'

        '<tr><td align="center" style="padding:16px 24px 0;font-family:' + _MONO + ';font-size:10.5px;'
        'letter-spacing:.5px;color:#3d5567;">&mdash; Rovix AI Monitoring &middot; phosphor never lies &mdash;'
        '</td></tr>'

        '</table></div>'
    )


def format_down_batch(items):
    """Returns (subject, plain_text, html) for 1+ monitors that went down
    in the same short debounce window. `items` is a list of dicts with
    keys name/detail/url/ts/context (context optional, same shape
    monitor_engine._alert_context returns). A single item renders exactly
    like format_down(..., context=...) always has -- the common case is
    unaffected. 2+ items render as one consolidated digest instead of N
    separate emails, which is the actual point: a shared host taking out
    several sites at once must not flood an inbox with nearly-identical
    [DOWN] emails.
    """
    if len(items) == 1:
        it = items[0]
        return format_down(it["name"], it["detail"], url=it.get("url"), ts=it.get("ts"),
                            context=it.get("context"))

    subject = f"[DOWN] {len(items)} sites down"
    lines = [f"🔴 ALERT: {len(items)} sites are down", ""]
    for it in items:
        target = f" ({it['url']})" if it.get("url") else ""
        lines.append(f"- {it['name']}: {it['detail']}{target}")
    dashboard = os.environ.get("DASHBOARD_URL")
    if dashboard:
        lines += ["", f"Dashboard: {dashboard}"]
    lines += ["", "— Rovix AI Monitoring"]
    html_body = _premium_digest("down", items, dashboard)
    return subject, "\n".join(lines), html_body


def format_recovered_batch(items):
    """Same idea as format_down_batch, for recovery. `detail` here is the
    downtime string (matching format_recovered's own `downtime` param)."""
    if len(items) == 1:
        it = items[0]
        return format_recovered(it["name"], it["detail"], url=it.get("url"), ts=it.get("ts"),
                                 context=it.get("context"))

    subject = f"[RECOVERED] {len(items)} sites recovered"
    lines = [f"🟢 ALERT: {len(items)} sites recovered", ""]
    for it in items:
        target = f" ({it['url']})" if it.get("url") else ""
        lines.append(f"- {it['name']}: {it['detail']} downtime{target}")
    dashboard = os.environ.get("DASHBOARD_URL")
    if dashboard:
        lines += ["", f"Dashboard: {dashboard}"]
    lines += ["", "— Rovix AI Monitoring"]
    html_body = _premium_digest("recovered", items, dashboard)
    return subject, "\n".join(lines), html_body


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
