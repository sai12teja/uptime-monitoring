"""Rovix Monitoring — frontend UI shell (PRD §13), backed by data.py's real
website/CRM checks (db.py + monitor_engine.py) and server metrics
(server_health.py). Correlated-outage detection is still stubbed in data.py
(separate task).

Run: python app.py            (production-ish, no debug UI)
     python app.py --debug    (hot reload + Dash debug tools)
"""

import os
import re
import secrets
import sys
import threading
from datetime import datetime
from urllib.parse import urlparse

import flask
from dash import Dash, html, dcc, Output, Input, State, ctx, ALL, no_update

# Must run before any module below reads a secret: SESSION_SECRET_KEY is read
# at import time further down this file, and email_alerts silently no-ops when
# SMTP_HOST is unset rather than raising. Real env vars still take precedence.
import env_file
env_file.load()

import api
import auth
import data
import db
import monitor_engine
import page_discovery
import push
import server_health

STATUS_ORDER = {"down": 0, "overdue": 1, "awaiting": 2, "up": 3}
STATUS_LABEL = {"up": "Up", "down": "Down", "overdue": "Overdue", "awaiting": "Awaiting first check"}
# Grouped-card rows sit beside a type label in a ~240px card -- the full
# "Awaiting first check" wraps there, so rows use a compact variant.
STATUS_LABEL_SHORT = {**STATUS_LABEL, "awaiting": "Awaiting"}

REFRESH_INTERVAL_MS = 15_000  # §13.2 — "every 10-30 seconds"
# No whitespace (rules out an embedded \r\n, which email.message rejects
# with a ValueError that would otherwise silently kill every future alert
# send -- see email_alerts.send()), one @, a dot in the domain part.
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

TIMELINE_DOT = {"ok": "ok", "up": "ok", "fail": "crit", "down": "crit", "warn": "warn", "info": "muted"}


# ---------- aria-live status-change diffing (Decision 6.2, pure/testable) ----------

def diff_status_messages(prev_statuses, curr_targets):
    """prev_statuses: {target_id: status} from the last tick. curr_targets: data.get_targets().

    Returns human-readable messages ONLY for targets whose status actually
    changed since the last tick — never fires on first load (empty prev) and
    never fires on every no-op refresh poll.
    """
    if not prev_statuses:
        return []
    messages = []
    for t in curr_targets:
        old = prev_statuses.get(t["id"])
        if old is not None and old != t["status"]:
            messages.append(f"{t['name']} is now {STATUS_LABEL[t['status']].lower()}")
    return messages


def diff_outage_message(prev_outage, curr_outage):
    """None on first tick (no prior state); a message only on an actual transition."""
    if prev_outage is None or prev_outage == curr_outage:
        return None
    return "Server unreachable — all checks failing" if curr_outage else "Server recovered — all checks passing"


# ---------- Summary bar / correlated-outage banner (Decision 1.1) ----------

def _format_mttr(avg_seconds):
    """Display text for the average-resolution-time stat -- "—" (never a
    fabricated 0) until at least one incident has actually resolved."""
    return db.format_duration(avg_seconds) if avg_seconds is not None else "—"


def _window_button_class(window_hours, this_window):
    return "window-btn active" if window_hours == this_window else "window-btn"


def _server_status_label(metrics):
    """Real aggregate server-health label, not a hardcoded "healthy" --
    critical if any metric hits the crit threshold, degraded at the warn
    threshold, matching build_stat_card's existing 80/95 thresholds."""
    values = [metrics.get(k) for k in ("cpu_pct", "mem_pct", "disk_pct", "inodes_pct")]
    values = [v for v in values if v is not None]
    if not values:
        return "unknown"
    if any(v >= 95 for v in values):
        return "critical"
    if any(v >= 80 for v in values):
        return "degraded"
    return "healthy"


INCIDENT_WINDOWS = [("1h", 1), ("24h", 24), ("7d", 24 * 7)]

# Sidebar pages (Vapi-inspired redesign): all three stay mounted in the DOM
# at all times -- switching pages just toggles which one's CSS class is
# visible, matching the show/hide pattern already used for modals. This
# means every existing callback keeps targeting the same component ids
# regardless of which page is on screen, with zero re-plumbing.
PAGES = ["monitors", "incidents", "server-health"]
PAGE_TITLES = {"monitors": "Monitors", "incidents": "Incidents", "server-health": "Server Health"}


def _nav_item_class(active_page, this_page):
    return "sidebar-nav-item active" if active_page == this_page else "sidebar-nav-item"


def _page_wrapper_class(active_page, this_page):
    return "page-section" if active_page == this_page else "page-section hidden"


def _page_title(active_page):
    return PAGE_TITLES.get(active_page, "Monitors")


def build_topbar(active_page="monitors"):
    """Persistent header (page title + global actions) -- lives OUTSIDE the
    per-page toggled sections, so it stays visible and functional no matter
    which sidebar page is active."""
    return html.Div(
        className="content-topbar",
        children=[
            html.H1(_page_title(active_page), className="page-title"),
            html.Div(
                className="header-actions",
                children=[
                    html.Span(f"updated {datetime.now().strftime('%H:%M:%S')}", className="updated-at"),
                    html.Button("⚙", id="settings-open", className="ack-btn settings-gear",
                                n_clicks=0, title="Notification settings"),
                    html.A("Logout", href="/logout", className="logout-link"),
                ],
            ),
        ],
    )


def build_skeleton_topbar():
    # settings-open/logout-link are real, not placeholders -- this project
    # already learned the hard way (detail-edit-btn) that a callback Input
    # id only mounted after the first real render can silently eat a click
    # that lands before the swap. Only the title text is skeletal.
    return html.Div(
        className="content-topbar",
        children=[
            html.Div(className="skeleton-block", style={"width": "120px", "height": "20px"}),
            html.Div(
                className="header-actions",
                children=[
                    html.Button("⚙", id="settings-open", className="ack-btn settings-gear",
                                n_clicks=0, title="Notification settings"),
                    html.A("Logout", href="/logout", className="logout-link"),
                ],
            ),
        ],
    )


def build_summary_or_banner(window_hours=24):
    if data.is_correlated_outage():
        return html.Div(
            className="outage-banner",
            children="⚠ SERVER UNREACHABLE — ALL CHECKS FAILING",
        )

    counts = data.summary_counts()
    server_status = _server_status_label(server_health.read_all())
    avg_mttr = data.avg_incident_duration()
    incident_count = data.incidents_in_window(window_hours)
    by_type = data.monitors_by_type()

    # Counted by SITE, matching the grid's own grouping. The old card counted
    # individual checks, so "14 down" sat beside a 42-card grid with no way to
    # tell whether that meant 14 dead sites or one broken page on a handful --
    # and "down" hid the difference between a domain that doesn't resolve and
    # a healthy server with one erroring page.
    sites = data.site_health_counts()

    def stat_row(status_class, label, value, hint):
        return html.Div(className="summary-stat-row", children=[
            html.Span(className=f"summary-stat-icon status-{status_class}"),
            html.Span(label, className="summary-stat-label"),
            html.Span(hint, className="summary-stat-hint"),
            html.Span(str(value), className="summary-stat-value"),
        ])

    status_card = html.Div(
        className="summary-stat-card",
        children=[
            html.Div(f"Site Status · {sites['total']} sites",
                     className="summary-stat-title"),
            stat_row("down", "Down", sites["down"], "every check failing"),
            stat_row("overdue", "Degraded", sites["degraded"], "some checks failing"),
            stat_row("up", "Up", sites["up"], "all checks passing"),
            html.Div(
                f"{counts['down']} of {counts['total']} individual checks failing  ·  "
                f"avg fix time {_format_mttr(avg_mttr)}  ·  this server: {server_status}",
                className="summary-stat-footer",
            ),
        ],
    )

    incidents_card = html.Div(
        className="summary-stat-card",
        children=[
            html.Div(className="summary-stat-header", children=[
                html.Div("Incidents", className="summary-stat-title"),
                html.Div(className="window-toggle", children=[
                    html.Button(label, id=f"incident-window-{label}", n_clicks=0,
                                className=_window_button_class(window_hours, hours))
                    for label, hours in INCIDENT_WINDOWS
                ]),
            ]),
            html.Div(str(incident_count), className="summary-stat-big-number"),
            html.Div(
                "No incidents in this window" if incident_count == 0
                else f"incident{'s' if incident_count != 1 else ''} started in this window",
                className="summary-stat-footer",
            ),
        ],
    )

    type_rows = [
        html.Div(className="summary-stat-row", children=[
            html.Span(className=f"summary-stat-icon type-{t}"),
            html.Span(TYPE_LABELS.get(t, t.title()), className="summary-stat-label"),
            html.Span(str(n), className="summary-stat-value"),
        ])
        for t, n in sorted(by_type.items())
    ] if by_type else [html.Div("No monitors configured yet", className="summary-stat-footer")]

    type_card = html.Div(
        className="summary-stat-card",
        children=[html.Div("Monitors by Type", className="summary-stat-title")] + type_rows,
    )

    children = [html.Div(className="summary-stat-row-outer",
                          children=[status_card, incidents_card, type_card])]

    if counts["total"] > 0 and counts["down"] == 0:
        # The real-data equivalent of a "No Issues Found" hero -- shown only
        # when there's something to celebrate, not decoration on every load.
        children.append(html.Div(
            className="all-clear-hero",
            children=[
                html.Div("✓", className="all-clear-icon"),
                html.Div("All Systems Operational", className="all-clear-title"),
                html.Div(f"All {counts['total']} monitored targets are up.", className="all-clear-desc"),
            ],
        ))

    bar = html.Div(className="summary-bar calm", children=children)

    if data.is_stale():
        # Decision 2.2 — keep last-known state visible, add a persistent
        # staleness notice rather than going blank or hiding the summary bar.
        stale_banner = html.Div(
            className="stale-banner",
            children=f"⚠ Data may be stale — dashboard connection lost, "
                      f"last updated {data.STALE_LAST_UPDATED} ago",
        )
        return html.Div([stale_banner, bar])

    return bar


# ---------- Target cards ----------

def _strip_type_suffix(name):
    # Path suffix (" — /about") is always the outer layer -- entry names are
    # built as "base [(Type)] [— path]" -- so strip it before the type suffix.
    if " — " in name:
        name = name.rsplit(" — ", 1)[0]
    for label in TYPE_LABELS.values():
        suffix = f" ({label})"
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _group_status_tier(group):
    """3-tier health signal for a grouped card: green only when every
    sub-monitor is up, red only when every sub-monitor is down, grey for
    any real mix (including one still awaiting/overdue) -- a single bad
    sub-monitor no longer paints the whole card red on its own."""
    statuses = [t["status"] for t in group]
    if all(s == "up" for s in statuses):
        return "up"
    if all(s == "down" for s in statuses):
        return "down"
    return "mixed"


def _favicon_url(url):
    """The site's OWN /favicon.ico, loaded straight by the browser -- no
    third-party favicon service, which would hand every monitored domain
    (including internal/client sites) to that service. Returns None when
    there's no host to ask (push monitors have no target)."""
    if not url:
        return None
    # TCP/DNS monitors store a bare hostname with no scheme; urlparse would
    # read that as a path, so give it one before parsing.
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"


def _site_link(url):
    """(href, hostname) for the card's clickable domain line, or None when
    there's no host to link (push monitors have no target).

    Always links the site ROOT, not the monitor's specific path: a card can
    represent several page monitors, and the domain is what the label shows,
    so linking one arbitrary sub-page would contradict the visible text.
    TCP/DNS rows store a bare hostname, hence the same scheme fill-in as
    _favicon_url."""
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.netloc}", parsed.hostname


def _badge_letter(name):
    """First real character of the name, for the fallback badge shown when
    a site has no favicon."""
    for char in (name or ""):
        if char.isalnum():
            return char.upper()
    return "?"


def _badge_hue(name):
    """Deterministic hue per name so a site's fallback badge is the same
    colour on every render (a random one would flicker each refresh)."""
    return sum(ord(c) for c in (name or "")) * 47 % 360


def _group_favicon_target(group):
    """The group member whose url can actually produce a favicon. A group
    led by a Push monitor (url="") would otherwise fall back to a letter
    badge even though its Website sibling knows the real host."""
    for target in group:
        if _favicon_url(target.get("url")):
            return target
    return None


def build_site_link(url):
    """Clickable domain line for a card. Returns an empty span when the
    monitor has no linkable host, so the caller's layout row keeps its shape
    either way.

    target/rel: opens in a new tab so clicking never navigates the dashboard
    away mid-incident; noopener+noreferrer because these are third-party
    sites and window.opener access / referrer leakage are both unwanted."""
    link = _site_link(url)
    if link is None:
        return html.Span(className="target-card-link-empty")
    href, hostname = link
    return html.A(
        hostname,
        href=href,
        target="_blank",
        rel="noopener noreferrer",
        className="target-card-link",
        title=f"Open {hostname} in a new tab",
    )


def build_site_icon(url, name, favicon_url=None):
    """Site icon with a coloured letter badge behind it. The badge is a real
    element underneath, not an onerror JS handler: if the icon 404s or the
    site blocks hotlinking, the (empty-alt) img renders nothing and the
    badge simply shows through. No JS, no layout shift either way.

    Prefers the icon the site actually declares in its HTML (resolved once
    and stored); /favicon.ico is only a last resort, since in practice most
    real sites 404 there while declaring a custom path."""
    children = [html.Span(_badge_letter(name), className="site-icon-letter")]
    src = favicon_url or _favicon_url(url)
    if src:
        # No loading="lazy" -- dash 2.18.2's html.Img doesn't accept it.
        children.append(html.Img(src=src, alt="", className="site-icon-img"))
    return html.Span(
        className="site-icon",
        style={"--badge-hue": str(_badge_hue(name))},
        children=children,
    )


def _group_summary_text(group):
    """At-a-glance meta line for a COLLAPSED grouped card, e.g.
    "6 checks · 2 down". Down count always leads when there is one -- it's
    the number worth reading first. With no downs but something not yet
    reporting (awaiting/overdue), report the up count rather than claiming
    "all up", which would be a lie about monitors that haven't checked."""
    total = len(group)
    label = f"{total} check{'s' if total != 1 else ''}"
    down = sum(1 for t in group if t["status"] == "down")
    up = sum(1 for t in group if t["status"] == "up")
    if down == total:
        return f"{label} · all down"
    if down:
        return f"{label} · {down} down"
    if up == total:
        return f"{label} · all up"
    return f"{label} · {up} up"


def build_target_card(group):
    """`group` is one-or-more target dicts. A single target (the common
    case, and always true for monitors added one at a time) renders exactly
    as before. Multiple targets sharing a group_key (one multi-select Add
    Monitor submission) render as one card with a status line per check
    type, since they're really one logical target checked several ways."""
    if len(group) == 1:
        target = group[0]
        status = target["status"]

        meta_parts = []
        if target["last_checked_sec"] is not None:
            meta_parts.append(f"checked {target['last_checked_sec']}s ago")
        if target["response_ms"] is not None:
            meta_parts.append(f"{target['response_ms']}ms")
        meta_text = " · ".join(meta_parts) if meta_parts else "no checks yet"

        # A real <button>, not a div+role+tabIndex fake — Enter/Space work
        # natively without any extra JS, div-based fakes silently fail this.
        #
        # An <a> cannot be nested inside that <button> (invalid HTML; browsers
        # disagree on which one a click activates), so the meta line moves OUT
        # of the button and shares a normal flex row with the link. Earlier
        # this link was absolutely positioned over the card instead, which
        # printed it on top of the meta text -- a real flex row simply cannot
        # overlap, so the layout is correct by construction rather than by
        # tuned percentages.
        card = html.Button(
            id={"type": "tcard", "index": target["id"]},
            className=f"target-card-hit clickable status-{status}",
            n_clicks=0,
            children=[
                html.Div(
                    className="target-card-name",
                    children=[
                        build_site_icon(target.get("url"), target["name"],
                                        target.get("favicon_url")),
                        html.Span(target["name"], className="target-card-title"),
                    ],
                ),
                html.Div(
                    className=f"target-card-status status-{status}",
                    children=[
                        html.Span(className="status-dot"),
                        html.Span(STATUS_LABEL[status], className="status-label"),
                    ],
                ),
            ],
        )
        return html.Div(
            className=f"target-card status-{status}",
            children=[
                card,
                html.Div(
                    className="target-card-meta-row",
                    children=[
                        html.Span(meta_text, className="target-card-meta"),
                        build_site_link(target.get("url")),
                    ],
                ),
            ],
        )

    tier = _group_status_tier(group)
    base_name = _strip_type_suffix(group[0]["name"])

    def subrow(t):
        # Same information as a solo card's meta line (status + response
        # time), just laid out per check type -- a grouped card shouldn't
        # tell you less than the cards it replaces.
        # subrow_label disambiguates same-type entries (several pages of one
        # site); falls back to the check-type label when there's only one
        # entry per type (the original multi-select-check-type case).
        label = t.get("subrow_label") or TYPE_LABELS.get(t["type"], t["type"].title())
        children = [
            html.Span(className="status-dot"),
            html.Span(label, className="target-card-subtype"),
            html.Span(STATUS_LABEL_SHORT[t["status"]], className="status-label target-card-substatus"),
        ]
        if t["response_ms"] is not None:
            children.append(html.Span(f"· {t['response_ms']}ms", className="target-card-subtime"))
        return html.Button(
            id={"type": "tcard", "index": t["id"]},
            className=f"target-card-subrow clickable status-{t['status']}",
            n_clicks=0,
            children=children,
        )

    def type_rank(t):
        # Stable, meaningful order (primary check first) rather than
        # alphabetical -- rows must not reshuffle as statuses change.
        return GROUP_ORDER.index(t["type"]) if t["type"] in GROUP_ORDER else len(GROUP_ORDER)

    rows = [subrow(t) for t in sorted(group, key=type_rank)]
    icon_target = _group_favicon_target(group) or {}

    # <details>/<summary> -- collapsed by default; expanding reveals the
    # sub-rows, which scroll internally (.target-card-subrows) rather than
    # growing the whole card when a site has many pages. Native, no
    # JS/callback needed. The collapsed face carries a status dot, the site
    # name and a summary meta line, so it matches a solo card's height
    # instead of rendering as a one-line bar next to them.
    return html.Details(
        open=False,
        className=f"target-card clickable status-{tier}",
        children=[
            # Both the title row AND the meta/link row live INSIDE <summary>:
            # a collapsed <details> renders only its first child, so anything
            # left outside the summary is invisible until the card is expanded
            # -- which is what blanked the check summary and the site link on
            # every grouped card. The summary is the collapsed card's face.
            html.Summary(
                className=f"target-card-summary status-{tier}",
                children=[
                    html.Div(
                        className=f"target-card-name status-{tier}",
                        children=[
                            build_site_icon(icon_target.get("url"), base_name,
                                            icon_target.get("favicon_url")),
                            html.Span(base_name, className="target-card-title"),
                            html.Span(className="status-dot"),
                        ],
                    ),
                    html.Div(
                        className="target-card-meta-row",
                        children=[
                            html.Span(_group_summary_text(group), className="target-card-meta"),
                            build_site_link(icon_target.get("url")),
                        ],
                    ),
                ],
            ),
            html.Div(rows, className="target-card-subrows"),
        ],
    )


def _target_matches(target, term):
    """One monitor vs one lowercase search term. Matches the name, the URL/
    host, the check type, and the status text -- so "down", "dns", "crm" and
    a bare domain are all usable searches, not just an exact name prefix."""
    haystack = " ".join(str(x or "").lower() for x in (
        target.get("name"), target.get("url"), target.get("type"),
        target.get("status"), target.get("subrow_label"),
    ))
    return term in haystack


def _filter_groups(groups, filter_text):
    """Keeps a whole card when ANY of its checks match, so searching a domain
    returns the site's card intact rather than a fragment of its rows.
    Multiple words are AND-ed (each must match somewhere in the group), which
    makes "nawab down" a useful query."""
    terms = (filter_text or "").strip().lower().split()
    if not terms:
        return groups
    return [
        group for group in groups
        if all(any(_target_matches(t, term) for t in group) for term in terms)
    ]


def _group_targets(targets):
    """Partition targets into render groups: those sharing a group_key
    (one multi-select submission) become one group, everything else is
    its own single-item group."""
    groups = {}
    order = []
    for t in targets:
        key = t["group_key"] or f"solo-{t['id']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)
    return [groups[key] for key in order]


GROUP_LABELS = {"website": "Websites", "crm": "CRM", "tcp": "TCP Ports", "dns": "DNS"}
GROUP_ORDER = ["website", "crm", "tcp", "dns"]
SCALE_THRESHOLD = 200  # Decision 7.2 — the type-sectioned grid kicks in above this
# Separate from SCALE_THRESHOLD on purpose: the two used to share one constant,
# so raising the grouping threshold to keep one-card-per-domain silently hid the
# search box as well. A filter is useful long before the grid needs restructuring.
FILTER_THRESHOLD = 8


def build_target_grid(filter_text=None):
    groups = _group_targets(data.get_targets())
    groups.sort(key=lambda g: min(STATUS_ORDER[t["status"]] for t in g))
    if not groups:
        # Empty state (§13.2) — a feature, not a blank grid.
        return html.Div(
            className="empty-state",
            children=[
                html.Div("No monitors configured yet", className="empty-state-title"),
                html.Div("Use the + Add Monitor button above to add your first site.",
                         className="empty-state-hint"),
            ],
        )

    if len(groups) <= SCALE_THRESHOLD:
        # Filter applies HERE too. It used to be honoured only in the
        # type-sectioned branch below, so in the normal grouped view the box
        # was inert -- typing filtered nothing.
        groups = _filter_groups(groups, filter_text)
        if not groups:
            return html.Div(
                className="empty-state",
                children=[
                    html.Div("No matching monitors", className="empty-state-title"),
                    html.Div(f"Nothing matches “{filter_text}”. Try a site name, domain, or status "
                             f"like “down”.", className="empty-state-hint"),
                ],
            )
        return html.Div(className="target-grid", children=[build_target_card(g) for g in groups])

    # Decision 7.2 — above ~20-25 targets, group by type + add a name filter
    # so the grid stays scannable instead of becoming a wall of cards. This
    # view is unaffected by multi-select grouping above -- at this scale
    # targets are sectioned by check type, so it stays one row per monitor.
    targets = data.get_targets()
    filter_text = (filter_text or "").strip().lower()
    filtered = [t for t in targets if filter_text in t["name"].lower()] if filter_text else targets

    type_groups = []
    for group_type in GROUP_ORDER:
        group_targets = [t for t in filtered if t["type"] == group_type]
        if not group_targets:
            continue
        type_groups.append(
            html.Details(
                open=True,
                className="target-group",
                children=[
                    html.Summary(f"{GROUP_LABELS.get(group_type, group_type.title())} ({len(group_targets)})",
                                  className="group-summary"),
                    html.Div(className="target-grid", children=[build_target_card([t]) for t in group_targets]),
                ],
            )
        )

    if not type_groups:
        type_groups = [html.Div("No targets match that filter.", className="empty-state-hint")]

    return html.Div(type_groups, className="target-groups")


# ---------- Server health ----------

def build_stat_card(label, value, warn_at=80, crit_at=95):
    if value is None:
        # Metric not readable on this platform (server_health.py). Show "—",
        # never 0% — a fake zero reads as "healthy" when nothing was measured.
        level, display, bar_width = "unknown", "—", 0
    else:
        level = "crit" if value >= crit_at else "warn" if value >= warn_at else "ok"
        display = f"{value}%"
        bar_width = min(value, 100)  # load average legitimately exceeds 100%

    return html.Div(
        className=f"stat-card level-{level}",
        children=[
            html.Div(
                className="stat-head",
                children=[
                    html.Span(label, className="stat-label"),
                    html.Span(display, className="stat-value"),
                ],
            ),
            html.Div(
                className="stat-bar",
                children=html.Div(className="stat-bar-fill", style={"width": f"{bar_width}%"}),
            ),
        ],
    )


def build_server_panel_inner():
    m = data.get_server_metrics()
    # Same THRESHOLDS the incident logic uses (server_health.py) — a card
    # only turns red/amber exactly when that also opens/escalates an incident.
    stats = [
        build_stat_card(server_health.METRIC_LABELS[key], m[key],
                         warn_at=server_health.THRESHOLDS[key][0],
                         crit_at=server_health.THRESHOLDS[key][1])
        for key in ("cpu_pct", "mem_pct", "disk_pct", "inodes_pct")
    ]

    service_rows = [
        html.Div(
            className="service-row",
            children=[
                html.Span(
                    className="service-name",
                    children=[
                        html.Span(className=f"status-dot service-dot-{'ok' if state == 'up' else 'crit'}"),
                        html.Span(name),
                    ],
                ),
                # Color + text label, never color alone (§13.2)
                html.Span(state, className=f"service-status service-status-{'ok' if state == 'up' else 'crit'}"),
            ],
        )
        for name, state in m["services"].items()
    ]
    if not service_rows:
        service_rows = [html.Div("Service status unavailable on this host.",
                                 className="empty-state-hint")]

    return html.Div(
        children=[
            html.Div(className="server-panel", children=stats),
            html.Div(
                className="services-card",
                children=[html.Div("Core services", className="services-card-label")] + service_rows,
            ),
        ]
    )


# ---------- Loading skeletons (§13.2 — "never a blank white page") ----------

def build_skeleton_summary():
    return html.Div(
        className="summary-bar calm",
        children=[
            html.Div(className="skeleton-block", style={"width": "140px", "height": "20px"}),
            html.Div(className="skeleton-block", style={"width": "220px", "height": "16px"}),
        ],
    )


def build_skeleton_grid(count=4):
    def skeleton_card():
        return html.Div(
            className="target-card skeleton-card",
            children=[
                html.Div(className="skeleton-block", style={"width": "70%", "height": "16px", "marginBottom": "10px"}),
                html.Div(className="skeleton-block", style={"width": "50%", "height": "14px", "marginBottom": "8px"}),
                html.Div(className="skeleton-block", style={"width": "60%", "height": "12px"}),
            ],
        )
    return html.Div(className="target-grid", children=[skeleton_card() for _ in range(count)])


def build_skeleton_server_panel():
    def skeleton_stat():
        return html.Div(
            className="stat-card skeleton-card",
            children=[
                html.Div(className="skeleton-block", style={"width": "50%", "height": "14px", "marginBottom": "12px"}),
                html.Div(className="skeleton-block", style={"width": "100%", "height": "20px"}),
            ],
        )
    return html.Div(
        children=[
            html.Div(className="server-panel", children=[skeleton_stat() for _ in range(4)]),
            html.Div(
                className="services-card skeleton-card",
                children=[html.Div(className="skeleton-block", style={"width": "35%", "height": "14px"})],
            ),
        ]
    )


def build_skeleton_incident_table(rows=3):
    def skeleton_row():
        return html.Tr([
            html.Td(html.Div(className="skeleton-block", style={"height": "14px"}))
            for _ in range(7)
        ])
    return html.Table(className="incident-table", children=[html.Tbody([skeleton_row() for _ in range(rows)])])


# ---------- Incident history ----------

def build_incident_table_inner():
    if data.is_incident_table_error():
        # §13.2 — a section fails on its own without taking down the page.
        return html.Div(
            className="section-error",
            children=[
                html.Div("Couldn't load incident history", className="section-error-title"),
                html.Div("The incident history couldn't be loaded.", className="empty-state-hint"),
                html.Button("Retry", id="incident-retry-btn", className="ack-btn", n_clicks=0,
                            style={"marginTop": "12px"}),
            ],
        )

    incidents = data.get_incidents()
    if not incidents:
        return html.Div(
            className="empty-state",
            children=[
                html.Div("No incidents recorded", className="empty-state-title"),
                html.Div("All systems have been healthy since monitoring began.", className="empty-state-hint"),
            ],
        )

    rows = []
    for inc in incidents:
        is_open = inc["resolved"] is None
        if inc["acknowledged_by"]:
            ack_cell = html.Span(f"seen by {inc['acknowledged_by']}", className="ack-marker")
        elif is_open:
            ack_cell = html.Button(
                "Acknowledge",
                id={"type": "ack", "index": inc["id"]},
                className="ack-btn",
                n_clicks=0,
            )
        else:
            ack_cell = ""

        rows.append(
            html.Tr(
                [
                    html.Td(inc["target"], **{"data-label": "Target"}),
                    html.Td(inc["problem"], **{"data-label": "Problem"}),
                    html.Td(inc["severity"].capitalize(),
                            className=f"severity-{inc['severity']}", **{"data-label": "Severity"}),
                    html.Td(inc["started"], **{"data-label": "Started"}),
                    html.Td(inc["resolved"] or "ongoing", **{"data-label": "Resolved"}),
                    html.Td(inc["duration"] if not is_open else "—", **{"data-label": "Duration"}),
                    html.Td(ack_cell, **{"data-label": "Ack"}),
                ]
            )
        )

    return html.Table(
        className="incident-table",
        children=[
            html.Thead(
                html.Tr([html.Th(h) for h in
                         ["Target", "Problem", "Severity", "Started", "Resolved", "Duration", "Ack"]])
            ),
            html.Tbody(rows),
        ],
    )


# ---------- Target detail slide-over (§13.6) ----------

def build_detail_content(target_id):
    detail = data.get_target_detail(target_id)
    if detail is None:
        return None

    t = detail["target"]
    status = t["status"]

    timeline_items = [
        html.Div(
            className="timeline-item",
            children=[
                html.Span(className=f"timeline-dot dot-{TIMELINE_DOT[kind]}"),
                html.Span(time, className="timeline-time"),
                html.Span(text, className="timeline-text"),
            ],
        )
        for time, kind, text in detail["timeline"]
    ]

    if detail["checks"]:
        checks_table = html.Table(
            className="mini-table",
            children=[
                html.Thead(html.Tr([html.Th("Time"), html.Th("Result"), html.Th("Detail")])),
                html.Tbody([
                    html.Tr([
                        html.Td(ts),
                        html.Td(result, className="check-ok" if result == "OK" else "check-fail"),
                        html.Td(detail_text),
                    ])
                    for ts, result, detail_text in detail["checks"]
                ]),
            ],
        )
    else:
        checks_table = html.Div("No checks recorded yet.", className="empty-state-hint")

    push_url_block = []
    if detail["push_token"]:
        # DASHBOARD_URL (already used by email_alerts.py) wins over the
        # incoming Host header when set -- request.host_url alone is
        # spoofable behind a reverse proxy without a pinned SERVER_NAME
        # (security review L2), which would show/copy a push URL pointing
        # at an attacker-controlled domain.
        base = os.environ.get("DASHBOARD_URL") or flask.request.host_url
        push_url = f"{base.rstrip('/')}/push/{detail['push_token']}"
        push_url_block = [
            html.Div("Push URL — ping this from your cron job/script", className="detail-section-label"),
            html.Div(push_url, className="push-url"),
        ]

    return [
        html.Div(
            className="detail-header",
            children=[
                html.Div(t["name"], className="detail-name", id="detail-panel-title"),
                html.Span(STATUS_LABEL[status], className=f"status-pill pill-{status}"),
            ],
        ),
        html.Div(
            className="detail-actions",
            children=[
                html.Button("Edit", id="detail-edit-btn", className="ack-btn", n_clicks=0),
                html.Button("Delete", id="detail-delete-btn", className="ack-btn delete-btn", n_clicks=0),
            ],
        ),
        *push_url_block,
        html.Div(
            className="detail-stats",
            children=[
                html.Div(className="detail-stat", children=[
                    html.Div("30-day uptime", className="detail-stat-label"),
                    html.Div(detail["stats"]["uptime_30d"], className="detail-stat-value"),
                ]),
                html.Div(className="detail-stat", children=[
                    html.Div("Avg response", className="detail-stat-label"),
                    html.Div(detail["stats"]["avg_ms"], className="detail-stat-value"),
                ]),
            ],
        ),
        html.Div("Timeline", className="detail-section-label"),
        html.Div(className="timeline", children=timeline_items),
        html.Div("Recent checks", className="detail-section-label"),
        checks_table,
    ]


# ---------- Layout ----------

def build_sidebar(active_page="monitors"):
    nav_items = [("monitors", "Monitors"), ("incidents", "Incidents"),
                 ("server-health", "Server Health")]
    return html.Div(
        className="sidebar",
        children=[
            html.Div("Rovix Uptime Monitoring", className="sidebar-brand"),
            html.Div(
                className="sidebar-nav",
                children=[
                    html.Button(label, id=f"nav-{page}", n_clicks=0,
                                className=_nav_item_class(active_page, page))
                    for page, label in nav_items
                ],
            ),
        ],
    )


def serve_layout():
    return html.Div(
        className="app-shell",
        children=[
            build_sidebar(),
            html.Div(
                className="main-content dashboard",
                children=[
                    html.Div(id="live-region", **{"aria-live": "polite"}, style={
                        "position": "absolute", "width": "1px", "height": "1px", "overflow": "hidden"
                    }),
                    dcc.Store(id="selected-target"),
                    dcc.Store(id="editing-monitor-id"),
                    dcc.Store(id="incident-window-store", data=24),
                    dcc.Store(id="active-page-store", data="monitors"),
                    # session storage — survives a page reload so a status change
                    # picked up on the next tick is still diffed against the last
                    # known baseline, not reset to "everything just changed."
                    dcc.Store(id="prev-state", storage_type="session"),
                    # Persistent header (page title + settings/logout) -- lives
                    # outside the toggled page sections below, so it's visible
                    # and functional regardless of which sidebar page is active.
                    html.Div(id="topbar-slot", children=build_skeleton_topbar()),

                    # ---- Page: Monitors ----
                    html.Div(
                        id="page-monitors",
                        className=_page_wrapper_class("monitors", "monitors"),
                        children=[
                            # Initial render is a skeleton everywhere (§13.2) — the
                            # one-shot initial-load-timer swaps each slot for real
                            # content shortly after, same as a real API call would.
                            html.Div(id="summary-slot", children=build_skeleton_summary()),
                            html.Div(
                                className="section-heading-row",
                                children=[
                                    html.H2("Monitored targets", className="section-heading"),
                                    html.Button("+ Add Monitor", id="add-monitor-open",
                                                className="ack-btn", n_clicks=0),
                                ],
                            ),
                            # Always mounted (never conditionally created/destroyed) —
                            # hidden via CSS below the scale threshold. Keeping one real
                            # component in the DOM at all times means exactly one
                            # callback can safely own grid-slot; a component that only
                            # sometimes exists forces either a client-side "not found"
                            # error or a second writer racing the periodic refresh.
                            html.Div(
                                id="target-filter-wrapper",
                                className="target-filter-wrapper hidden",
                                # debounce is in SECONDS, not milliseconds --
                                # 0.3 waits 300ms after the last keystroke.
                                # (True would only fire on Enter/blur, which
                                # makes a search box feel dead while typing;
                                # a plain 300 means a 5-MINUTE wait.)
                                children=dcc.Input(id="target-filter", type="text",
                                                    placeholder="Search name, domain, type or status…",
                                                    className="target-filter", value="", debounce=0.3),
                            ),
                            html.Div(id="grid-slot", children=build_skeleton_grid()),
                        ],
                    ),

                    # ---- Page: Incidents ----
                    html.Div(
                        id="page-incidents",
                        className=_page_wrapper_class("monitors", "incidents"),
                        children=[
                            html.H2("Incident history", className="section-heading"),
                            html.Div(id="incident-slot", children=build_skeleton_incident_table()),
                        ],
                    ),

                    # ---- Page: Server Health ----
                    html.Div(
                        id="page-server-health",
                        className=_page_wrapper_class("monitors", "server-health"),
                        children=[
                            html.H2("Server health", className="section-heading"),
                            html.Div(id="server-panel-slot", children=build_skeleton_server_panel()),
                        ],
                    ),
            # Detail slide-over skeleton — always present so callbacks can
            # reference detail-close/detail-backdrop; visibility via CSS class.
            html.Div(
                id="detail-wrapper",
                className="detail-hidden",
                children=[
                    html.Div(id="detail-backdrop", n_clicks=0),
                    html.Div(
                        className="detail-panel",
                        role="dialog",
                        **{
                            "aria-modal": "true",
                            "aria-labelledby": "detail-panel-title",
                            "tabIndex": "-1",
                        },
                        children=[
                            html.Button("×", id="detail-close", className="detail-close",
                                        n_clicks=0, title="Close"),
                            html.Div(
                                id="detail-content",
                                # detail-edit-btn/detail-delete-btn must exist from initial
                                # page load, not just after the first card click -- they're
                                # registered as callback Inputs, and if a user's very first
                                # interaction is "+ Add Monitor" (before ever opening a
                                # detail panel), the browser doesn't have these ids yet and
                                # dash-renderer throws "nonexistent object used in an Input"
                                # -- which broke that very first click. build_detail_content()
                                # replaces this whole subtree once a target is selected, using
                                # the same ids, so this placeholder is only ever seen while
                                # detail-wrapper itself is hidden.
                                children=[
                                    html.Div(
                                        className="detail-actions",
                                        children=[
                                            html.Button("Edit", id="detail-edit-btn",
                                                        className="ack-btn", n_clicks=0),
                                            html.Button("Delete", id="detail-delete-btn",
                                                        className="ack-btn delete-btn", n_clicks=0),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            # Add Monitor modal — same always-mounted/hidden-via-CSS pattern
            # as the detail panel, so it can share the same focus-trap logic.
            html.Div(
                id="add-monitor-wrapper",
                className="modal-wrapper addmonitor-hidden",
                children=[
                    html.Div(id="add-monitor-backdrop", n_clicks=0, className="modal-backdrop"),
                    html.Div(
                        className="add-monitor-modal",
                        role="dialog",
                        **{
                            "aria-modal": "true",
                            "aria-labelledby": "add-monitor-title",
                            "tabIndex": "-1",
                        },
                        children=[
                            html.Button("×", id="add-monitor-close", className="detail-close",
                                        n_clicks=0, title="Close"),
                            html.Div("Add Monitor", className="detail-name", id="add-monitor-title"),
                            html.Div(id="add-monitor-error", className="form-error"),
                            html.Label("Name", htmlFor="add-monitor-name", className="form-label"),
                            dcc.Input(id="add-monitor-name", type="text",
                                      placeholder="e.g. Client Homepage", className="form-input",
                                      value=""),
                            html.Div(
                                id="add-monitor-target-wrapper",
                                className="form-field",
                                children=[
                                    html.Label("Target", htmlFor="add-monitor-url", className="form-label"),
                                    # autoComplete off -- this field id is reused for every monitor ever
                                    # added, so browsers accumulate every past URL as an autofill
                                    # suggestion; picking the wrong one silently points a new monitor at
                                    # someone else's site instead of the one just typed.
                                    dcc.Input(id="add-monitor-url", type="text", autoComplete="off",
                                              placeholder="https://example.com", className="form-input",
                                              value=""),
                                ],
                            ),
                            html.Div(
                                id="add-monitor-type-wrapper",
                                className="form-field",
                                children=[
                                    html.Label("Check type (select one or more)", className="form-label"),
                                    dcc.Checklist(
                                        id="add-monitor-type",
                                        className="form-radio",
                                        value=["website"],
                                        options=[
                                            {"label": "Website", "value": "website"},
                                            {"label": "CRM", "value": "crm"},
                                            {"label": "TCP Port", "value": "tcp"},
                                            {"label": "DNS", "value": "dns"},
                                            {"label": "Push/Passive", "value": "push"},
                                        ],
                                    ),
                                ],
                            ),
                            html.Div(
                                className="form-row",
                                children=[
                                    html.Div(
                                        id="add-monitor-port-wrapper",
                                        className="form-field hidden",
                                        children=[
                                            html.Label("Port", htmlFor="add-monitor-port",
                                                        className="form-label"),
                                            dcc.Input(id="add-monitor-port", type="number",
                                                      placeholder="e.g. 5432", className="form-input", value=""),
                                        ],
                                    ),
                                    html.Div(
                                        id="add-monitor-interval-wrapper",
                                        className="form-field",
                                        children=[
                                            html.Label("Check interval (seconds)",
                                                        htmlFor="add-monitor-interval", className="form-label"),
                                            dcc.Input(id="add-monitor-interval", type="number",
                                                      placeholder="e.g. 60 (default)",
                                                      className="form-input", value=""),
                                        ],
                                    ),
                                ],
                            ),
                            html.Div(
                                id="add-monitor-paths-wrapper",
                                className="form-field hidden",
                                children=[
                                    html.Div(
                                        className="discover-row",
                                        children=[
                                            html.Label("Extra pages to monitor (one path per line, optional)",
                                                        htmlFor="add-monitor-paths", className="form-label"),
                                            html.Button("Discover pages", id="add-monitor-discover-btn",
                                                        className="ack-btn discover-btn", n_clicks=0),
                                        ],
                                    ),
                                    html.Div(id="add-monitor-discover-status", className="discover-status"),
                                    dcc.Checklist(id="add-monitor-discovered",
                                                  className="form-radio discover-checklist",
                                                  options=[], value=[]),
                                    dcc.Textarea(id="add-monitor-paths", className="form-input",
                                                 placeholder="/\n/about\n/contact", value=""),
                                ],
                            ),
                            html.Div(
                                id="add-monitor-keyword-wrapper",
                                className="form-field",
                                children=[
                                    html.Label("Expected value (optional)", htmlFor="add-monitor-keyword",
                                               className="form-label"),
                                    dcc.Input(id="add-monitor-keyword", type="text",
                                              placeholder="e.g. Sign in, or an expected IP for DNS",
                                              className="form-input", value=""),
                                ],
                            ),
                            html.Div(
                                className="form-field",
                                children=[
                                    dcc.Checklist(
                                        id="add-monitor-notify",
                                        className="form-radio",
                                        value=["notify"],
                                        options=[{"label": "Email alerts for this monitor", "value": "notify"}],
                                    ),
                                ],
                            ),
                            html.Div(
                                className="form-row",
                                children=[
                                    html.Div(
                                        id="add-monitor-retries-wrapper",
                                        className="form-field",
                                        children=[
                                            html.Label("Retries (extra fails before marking down)",
                                                        htmlFor="add-monitor-retries", className="form-label"),
                                            dcc.Input(id="add-monitor-retries", type="number",
                                                      placeholder="e.g. 2 (default)", className="form-input",
                                                      value=""),
                                        ],
                                    ),
                                    html.Div(
                                        id="add-monitor-timeout-wrapper",
                                        className="form-field",
                                        children=[
                                            html.Label("Request timeout (seconds)",
                                                        htmlFor="add-monitor-timeout", className="form-label"),
                                            dcc.Input(id="add-monitor-timeout", type="number",
                                                      placeholder="e.g. 10 (default)", className="form-input",
                                                      value=""),
                                        ],
                                    ),
                                ],
                            ),
                            html.Div(
                                className="form-row",
                                children=[
                                    html.Div(
                                        id="add-monitor-method-wrapper",
                                        className="form-field hidden",
                                        children=[
                                            html.Label("HTTP Method", htmlFor="add-monitor-method",
                                                        className="form-label"),
                                            dcc.Dropdown(id="add-monitor-method", className="form-dropdown",
                                                         value="GET", clearable=False,
                                                         options=["GET", "POST", "PUT", "PATCH", "DELETE"]),
                                        ],
                                    ),
                                    html.Div(
                                        id="add-monitor-encoding-wrapper",
                                        className="form-field hidden",
                                        children=[
                                            html.Label("Body Encoding", htmlFor="add-monitor-encoding",
                                                        className="form-label"),
                                            dcc.Dropdown(id="add-monitor-encoding", className="form-dropdown",
                                                         value="json", clearable=False,
                                                         options=[
                                                             {"label": "JSON", "value": "json"},
                                                             {"label": "Form (key=value per line)", "value": "form"},
                                                             {"label": "Plain text", "value": "text"},
                                                         ]),
                                        ],
                                    ),
                                ],
                            ),
                            html.Div(
                                id="add-monitor-body-wrapper",
                                className="form-field hidden",
                                children=[
                                    html.Label("Body (optional, for non-GET methods)",
                                                htmlFor="add-monitor-body", className="form-label"),
                                    dcc.Textarea(id="add-monitor-body", className="form-input",
                                                 placeholder='e.g. {"key": "value"}', value=""),
                                ],
                            ),
                            html.Div(
                                className="form-actions",
                                children=[
                                    html.Button("Cancel", id="add-monitor-cancel", className="ack-btn",
                                                n_clicks=0),
                                    html.Button("Add Monitor", id="add-monitor-submit",
                                                className="ack-btn submit-btn", n_clicks=0),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            # Delete-confirm modal — same always-mounted/hidden-via-CSS pattern.
            html.Div(
                id="delete-confirm-wrapper",
                className="modal-wrapper addmonitor-hidden",
                children=[
                    html.Div(id="delete-confirm-backdrop", n_clicks=0, className="modal-backdrop"),
                    html.Div(
                        className="add-monitor-modal",
                        role="dialog",
                        **{
                            "aria-modal": "true",
                            "aria-labelledby": "delete-confirm-title",
                            "tabIndex": "-1",
                        },
                        children=[
                            html.Div("Delete this monitor?", className="detail-name",
                                     id="delete-confirm-title"),
                            html.Div(
                                "This removes it from the dashboard. Its recorded history and "
                                "incidents stay in the database but won't be shown.",
                                className="form-label",
                            ),
                            html.Div(
                                className="form-actions",
                                children=[
                                    html.Button("Cancel", id="delete-confirm-cancel", className="ack-btn",
                                                n_clicks=0),
                                    html.Button("Delete Monitor", id="delete-confirm-submit",
                                                className="ack-btn submit-btn delete-btn", n_clicks=0),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            # Settings modal — same always-mounted/hidden-via-CSS pattern as
            # Add Monitor/Delete confirm. Up to 3 alert email addresses plus
            # a global mute, stored in the settings table (db.get/update_settings).
            html.Div(
                id="settings-wrapper",
                className="modal-wrapper addmonitor-hidden",
                children=[
                    html.Div(id="settings-backdrop", n_clicks=0, className="modal-backdrop"),
                    html.Div(
                        className="add-monitor-modal",
                        role="dialog",
                        **{
                            "aria-modal": "true",
                            "aria-labelledby": "settings-title",
                            "tabIndex": "-1",
                        },
                        children=[
                            html.Button("×", id="settings-close", className="detail-close",
                                        n_clicks=0, title="Close"),
                            html.Div("Notification Settings", className="detail-name", id="settings-title"),
                            html.Div(id="settings-error", className="form-error"),
                            html.Label("Alert email 1", htmlFor="settings-email-1", className="form-label"),
                            dcc.Input(id="settings-email-1", type="email", placeholder="ops@example.com",
                                      className="form-input", value=""),
                            html.Label("Alert email 2 (optional)", htmlFor="settings-email-2",
                                        className="form-label"),
                            dcc.Input(id="settings-email-2", type="email", placeholder="",
                                      className="form-input", value=""),
                            html.Label("Alert email 3 (optional)", htmlFor="settings-email-3",
                                        className="form-label"),
                            dcc.Input(id="settings-email-3", type="email", placeholder="",
                                      className="form-input", value=""),
                            html.Div(
                                className="form-field",
                                children=[
                                    dcc.Checklist(
                                        id="settings-notify-enabled",
                                        className="form-radio",
                                        value=["enabled"],
                                        options=[{"label": "Send email alerts", "value": "enabled"}],
                                    ),
                                ],
                            ),
                            html.Div(
                                className="form-actions",
                                children=[
                                    html.Button("Cancel", id="settings-cancel", className="ack-btn",
                                                n_clicks=0),
                                    html.Button("Save Settings", id="settings-submit",
                                                className="ack-btn submit-btn", n_clicks=0),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            dcc.Interval(id="refresh-interval", interval=REFRESH_INTERVAL_MS, n_intervals=0),
            dcc.Interval(id="initial-load-timer", interval=700, n_intervals=0, max_intervals=1),
                ],
            ),
        ],
    )


# ROVIX_SESSION_KEY_PATH: same reasoning as db.py's ROVIX_DB_PATH -- lets a
# deployment point this at a mounted volume directory. Unset everywhere
# except the Docker Compose setup, so this is a no-op elsewhere.
SESSION_KEY_PATH = os.environ.get("ROVIX_SESSION_KEY_PATH", ".session_secret.key")


def _load_or_create_secret_key():
    # Never a hardcoded default -- PRD §16 "never in plain code". If
    # SESSION_SECRET_KEY isn't set, persist a generated one to a local file
    # instead of regenerating on every restart (security review L1) --
    # otherwise every restart silently logs out every user. The file is
    # gitignored (*.key) like any other local secret.
    #
    # Only called from the __main__ block below, not at module import --
    # every test file in this repo imports app.py just to exercise its
    # callback functions, and none of them should write a real secret
    # file into the project directory as a side effect of that import
    # (same reasoning as db.py's DB_PATH being swappable per test).
    env_key = os.environ.get("SESSION_SECRET_KEY")
    if env_key:
        return env_key
    if os.path.exists(SESSION_KEY_PATH):
        with open(SESSION_KEY_PATH) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SESSION_KEY_PATH, "w") as f:
        f.write(key)
    return key


app = Dash(__name__, title="Rovix Uptime Monitoring", suppress_callback_exceptions=True)
app.layout = serve_layout
# Ephemeral here (matches pre-L1-fix behavior) -- merely importing this
# module (every UI test file does) must not touch disk. The real running
# server overrides this with the persisted key in the __main__ block below.
app.server.secret_key = os.environ.get("SESSION_SECRET_KEY") or secrets.token_hex(32)
auth.register_auth(app.server)  # PRD §16 gap 6 — session-login gate for the whole dashboard
api.register_api(app.server)  # PRD §11 — same process/port as the dashboard
push.register_push(app.server)  # push/passive monitor check-in endpoint


# ---------- Callbacks ----------

@app.callback(
    Output("topbar-slot", "children"),
    Output("summary-slot", "children"),
    Output("grid-slot", "children"),
    Output("server-panel-slot", "children"),
    Output("incident-slot", "children"),
    Output("target-filter-wrapper", "className"),
    Input("refresh-interval", "n_intervals"),
    Input("initial-load-timer", "n_intervals"),
    Input("target-filter", "value"),
    Input("incident-window-store", "data"),
    Input("active-page-store", "data"),
)
def refresh(_n, _initial, filter_text, window_hours, active_page):
    # Re-reads data.py (real DB-backed checks now) on every tick (periodic
    # refresh, the one-shot initial load, a filter keystroke, an
    # incident-window toggle click, or a sidebar page switch -- the last one
    # only actually needs topbar-slot's title text, but re-running the rest
    # is cheap and keeps this the single owner of these slots). target-filter
    # is always mounted (see layout comment), so this single callback can own
    # grid-slot with no race and no client-side "component not found" error
    # regardless of target count.
    show_filter = len(_group_targets(data.get_targets())) > FILTER_THRESHOLD
    filter_class = "target-filter-wrapper" if show_filter else "target-filter-wrapper hidden"
    return (
        build_topbar(active_page or "monitors"),
        build_summary_or_banner(window_hours or 24),
        build_target_grid(filter_text),
        build_server_panel_inner(),
        build_incident_table_inner(),
        filter_class,
    )


@app.callback(
    Output("incident-window-store", "data"),
    [Input(f"incident-window-{label}", "n_clicks") for label, _ in INCIDENT_WINDOWS],
    prevent_initial_call=True,
)
def set_incident_window(*_clicks):
    trig = ctx.triggered_id or ""
    label = trig.removeprefix("incident-window-")
    for window_label, hours in INCIDENT_WINDOWS:
        if window_label == label:
            return hours
    return no_update


@app.callback(
    Output("active-page-store", "data"),
    [Input(f"nav-{page}", "n_clicks") for page in PAGES],
    prevent_initial_call=True,
)
def set_active_page(*_clicks):
    trig = ctx.triggered_id or ""
    page = trig.removeprefix("nav-")
    return page if page in PAGES else no_update


@app.callback(
    [Output(f"nav-{page}", "className") for page in PAGES],
    [Output(f"page-{page}", "className") for page in PAGES],
    Input("active-page-store", "data"),
)
def render_active_page(active_page):
    active_page = active_page or "monitors"
    nav_classes = [_nav_item_class(active_page, page) for page in PAGES]
    page_classes = [_page_wrapper_class(active_page, page) for page in PAGES]
    return (*nav_classes, *page_classes)


@app.callback(
    Output("live-region", "children"),
    Output("prev-state", "data"),
    Input("refresh-interval", "n_intervals"),
    State("prev-state", "data"),
)
def announce_changes(_n, prev_state):
    targets = data.get_targets()
    outage = data.is_correlated_outage()
    prev_state = prev_state or {}

    messages = diff_status_messages(prev_state.get("targets") or {}, targets)
    outage_msg = diff_outage_message(prev_state.get("outage"), outage)
    if outage_msg:
        messages = [outage_msg] + messages

    new_state = {"targets": {t["id"]: t["status"] for t in targets}, "outage": outage}
    announcement = " ".join(messages) if messages else no_update
    return announcement, new_state


@app.callback(
    Output("selected-target", "data"),
    Input({"type": "tcard", "index": ALL}, "n_clicks"),
    Input("detail-close", "n_clicks"),
    Input("detail-backdrop", "n_clicks"),
    State("selected-target", "data"),
    prevent_initial_call=True,
)
def select_target(card_clicks, _close, _backdrop, current):
    trig = ctx.triggered_id
    if trig in ("detail-close", "detail-backdrop"):
        return None
    if isinstance(trig, dict) and trig.get("type") == "tcard":
        if not any(c for c in card_clicks if c):
            return current  # initial render noise, not a real click
        return trig["index"]
    return current


@app.callback(
    Output("detail-wrapper", "className"),
    Output("detail-content", "children"),
    Input("selected-target", "data"),
)
def render_detail(target_id):
    if target_id is None:
        return "detail-hidden", None
    return "detail-open", build_detail_content(target_id)


@app.callback(
    Output("incident-slot", "children", allow_duplicate=True),
    Input({"type": "ack", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def acknowledge(clicks):
    trig = ctx.triggered_id
    if isinstance(trig, dict) and trig.get("type") == "ack" and any(c for c in clicks if c):
        data.acknowledge_incident(trig["index"], "You")
    return build_incident_table_inner()


@app.callback(
    Output("incident-slot", "children", allow_duplicate=True),
    Input("incident-retry-btn", "n_clicks"),
    prevent_initial_call=True,
)
def retry_incidents(_n):
    # Mock retry — re-attempts the same "load" the section failed on. Stays
    # in the error state if INCIDENT_TABLE_ERROR hasn't been cleared, same as
    # a real retry against a backend that's still down.
    return build_incident_table_inner()


@app.callback(
    Output("add-monitor-target-wrapper", "className"),
    Output("add-monitor-port-wrapper", "className"),
    Output("add-monitor-keyword-wrapper", "className"),
    Output("add-monitor-interval-wrapper", "className"),
    Output("add-monitor-retries-wrapper", "className"),
    Output("add-monitor-timeout-wrapper", "className"),
    Output("add-monitor-method-wrapper", "className"),
    Output("add-monitor-body-wrapper", "className"),
    Output("add-monitor-encoding-wrapper", "className"),
    Output("add-monitor-paths-wrapper", "className"),
    Input("add-monitor-type", "value"),
    State("editing-monitor-id", "data"),
)
def toggle_type_fields(selected_types, editing_id):
    # A push-only selection needs just a name + expected check-in interval
    # -- no target to reach out to, no port, no expected-value match, no
    # retries/timeout (push has its own fixed 1-miss/1-recovery semantics),
    # no HTTP options. Combined with an active type (e.g. Website + Push,
    # grouped on one card), both sets of fields apply at once: the active
    # type still needs its target/keyword/etc, and push still needs its
    # interval -- neither hides the other.
    selected_types = selected_types or []
    is_push = "push" in selected_types
    has_active_type = bool(set(selected_types) - {"push"})
    is_http = bool({"website", "crm"} & set(selected_types))
    target_cls = "form-field" if has_active_type else "form-field hidden"
    port_cls = "form-field" if "tcp" in selected_types else "form-field hidden"
    keyword_cls = "form-field" if has_active_type else "form-field hidden"
    # Interval used to be push-only ("expected check-in interval"); it's a
    # real per-monitor check-frequency knob for every type now (§ page
    # monitoring — sub-pages default slower than the homepage), so it's
    # always shown once a type is selected.
    interval_cls = "form-field" if (has_active_type or is_push) else "form-field hidden"
    retries_cls = "form-field" if has_active_type else "form-field hidden"
    timeout_cls = "form-field" if has_active_type else "form-field hidden"
    method_cls = "form-field" if is_http else "form-field hidden"
    body_cls = "form-field" if is_http else "form-field hidden"
    encoding_cls = "form-field" if is_http else "form-field hidden"
    # Paths only fan out on a fresh Add submission -- editing is always a
    # single existing row, so the field is hidden rather than shown-but-inert.
    paths_cls = "form-field" if (is_http and editing_id is None) else "form-field hidden"
    return (target_cls, port_cls, keyword_cls, interval_cls,
            retries_cls, timeout_cls, method_cls, body_cls, encoding_cls, paths_cls)


TYPE_LABELS = {"website": "Website", "crm": "CRM", "tcp": "TCP", "dns": "DNS", "push": "Push"}


def _target_for_type(url, mtype):
    """DNS/TCP checks need a bare hostname; website/crm need the full URL
    with scheme. Multi-select shares one Target field across all selected
    types, so strip the scheme for DNS/TCP if the user typed a full URL."""
    if mtype in ("tcp", "dns") and "://" in url:
        return urlparse(url).hostname or url
    return url


def _parse_paths(raw_text):
    """Split the Paths textarea into stripped, non-blank path strings. Blank
    input means "no fan-out" -- callers get [None] so a single iteration
    with the plain Target URL behaves exactly like today."""
    paths = [line.strip() for line in (raw_text or "").splitlines() if line.strip()]
    return paths or [None]


def _merge_paths(discovered_checked, manual_text):
    """Combines checked "Discover pages" results with whatever's still
    hand-typed in the Extra Pages textarea, deduped, discovered paths
    first. If Discover was never used (discovered_checked empty), this is
    identical to _parse_paths(manual_text) -- today's behavior, unchanged."""
    manual = _parse_paths(manual_text)
    manual = [] if manual == [None] else manual
    combined = list(discovered_checked or [])
    for path in manual:
        if path not in combined:
            combined.append(path)
    return combined or [None]


def _url_with_path(base_url, path):
    if path is None:
        return base_url
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _default_interval(path, explicit_interval):
    """Every check type/path defaults to db.DEFAULT_CHECK_INTERVAL_SEC now
    (used to be 60s homepage / 300s sub-page). An explicit value always
    wins. `path` kept in the signature for callers/tests -- no longer
    changes the outcome, but removing it would be a wider signature churn
    for zero behavior gain."""
    return explicit_interval or db.DEFAULT_CHECK_INTERVAL_SEC


def _build_entries(name, types, paths):
    """Cross check-types x paths into one entry per monitor row. Suffixes
    the shared `name` and sets a `subrow_label` only when there's more than
    one entry to disambiguate -- a plain single-type, no-path submission
    (the common case) comes back untouched. Paths only apply to HTTP-style
    types (website/crm); TCP/DNS/Push always get exactly one entry."""
    type_suffix = len(types) > 1
    path_suffix = len(paths) > 1
    entries = []
    for mtype in types:
        applicable_paths = paths if mtype in ("website", "crm") else [None]
        for path in applicable_paths:
            entry_name = name
            if type_suffix:
                entry_name += f" ({TYPE_LABELS[mtype]})"
            if path_suffix and path is not None:
                entry_name += f" — {path}"

            if path_suffix and path is not None and type_suffix:
                subrow_label = f"{TYPE_LABELS[mtype]} {path}"
            elif path_suffix and path is not None:
                subrow_label = path
            else:
                subrow_label = None

            entries.append({"type": mtype, "path": path, "name": entry_name, "subrow_label": subrow_label})
    return entries


# Dict-based output handling — this callback now serves both Add and Edit
# (same modal, same fields; Edit just pre-fills them and locks the check
# type), so each branch only needs to name the handful of keys it actually
# changes instead of a 19-wide positional tuple.
ADD_EDIT_OUTPUT_IDS = [
    "wrapper_class", "error", "name", "url", "keyword", "summary", "grid",
    "title", "submit_label", "type_value", "type_wrapper_class",
    "port", "interval", "retries", "timeout", "method", "body", "encoding", "notify", "paths",
    "discovered_options", "discovered_value", "discover_status",
    "editing_id", "detail_content", "delete_confirm_class", "settings_class",
]


@app.callback(
    Output("add-monitor-discovered", "options", allow_duplicate=True),
    Output("add-monitor-discovered", "value", allow_duplicate=True),
    Output("add-monitor-discover-status", "children", allow_duplicate=True),
    Input("add-monitor-discover-btn", "n_clicks"),
    State("add-monitor-url", "value"),
    prevent_initial_call=True,
)
def discover_pages_callback(_n, url):
    url = (url or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return [], [], "Enter a valid http:// or https:// Target above first."

    paths, total, source = page_discovery.discover_pages(url)

    if source == "blocked":
        return [], [], "That target isn't allowed (resolves to a private/internal address)."
    if source == "none":
        return [], [], "Couldn't find any pages automatically — add paths manually below."

    options = [{"label": p, "value": p} for p in paths]
    status = f"Found {len(paths)} page{'s' if len(paths) != 1 else ''} via {source}"
    remaining = total - len(paths)
    if remaining > 0:
        status += f" ({remaining} more not shown)"
    # All pre-checked by default (matches the UptimeRobot reference) -- the
    # user unchecks what they don't want rather than hunting for a "select
    # all" button.
    return options, paths, status


@app.callback(
    Output("add-monitor-wrapper", "className"),
    Output("add-monitor-error", "children"),
    Output("add-monitor-name", "value"),
    Output("add-monitor-url", "value"),
    Output("add-monitor-keyword", "value"),
    Output("summary-slot", "children", allow_duplicate=True),
    Output("grid-slot", "children", allow_duplicate=True),
    Output("add-monitor-title", "children"),
    Output("add-monitor-submit", "children"),
    Output("add-monitor-type", "value"),
    Output("add-monitor-type-wrapper", "className"),
    Output("add-monitor-port", "value"),
    Output("add-monitor-interval", "value"),
    Output("add-monitor-retries", "value"),
    Output("add-monitor-timeout", "value"),
    Output("add-monitor-method", "value"),
    Output("add-monitor-body", "value"),
    Output("add-monitor-encoding", "value"),
    Output("add-monitor-notify", "value"),
    Output("add-monitor-paths", "value"),
    Output("add-monitor-discovered", "options", allow_duplicate=True),
    Output("add-monitor-discovered", "value", allow_duplicate=True),
    Output("add-monitor-discover-status", "children", allow_duplicate=True),
    Output("editing-monitor-id", "data"),
    Output("detail-content", "children", allow_duplicate=True),
    Output("delete-confirm-wrapper", "className", allow_duplicate=True),
    Output("settings-wrapper", "className", allow_duplicate=True),
    Input("add-monitor-open", "n_clicks"),
    Input("add-monitor-close", "n_clicks"),
    Input("add-monitor-cancel", "n_clicks"),
    Input("add-monitor-backdrop", "n_clicks"),
    Input("add-monitor-submit", "n_clicks"),
    Input("detail-edit-btn", "n_clicks"),
    State("add-monitor-name", "value"),
    State("add-monitor-url", "value"),
    State("add-monitor-type", "value"),
    State("add-monitor-keyword", "value"),
    State("add-monitor-port", "value"),
    State("add-monitor-interval", "value"),
    State("add-monitor-retries", "value"),
    State("add-monitor-timeout", "value"),
    State("add-monitor-method", "value"),
    State("add-monitor-body", "value"),
    State("add-monitor-encoding", "value"),
    State("add-monitor-notify", "value"),
    State("add-monitor-paths", "value"),
    State("add-monitor-discovered", "value"),
    State("target-filter", "value"),
    State("selected-target", "data"),
    State("editing-monitor-id", "data"),
    prevent_initial_call=True,
)
def add_edit_monitor(_open, _close, _cancel, _backdrop, _submit, _edit_open,
                      name, url, types, keyword, port, interval, retries, timeout,
                      method, body, encoding, notify, paths_text, discovered_checked,
                      filter_text, selected_id, editing_id):
    trig = ctx.triggered_id
    out = {key: no_update for key in ADD_EDIT_OUTPUT_IDS}

    def result():
        return tuple(out[key] for key in ADD_EDIT_OUTPUT_IDS)

    if trig == "add-monitor-open":
        out.update(
            wrapper_class="modal-wrapper addmonitor-open", error="", name="", url="", keyword="",
            title="Add Monitor", submit_label="Add Monitor",
            type_value=["website"], type_wrapper_class="form-field",
            port="", interval="", retries="", timeout="", method="GET", body="", encoding="json",
            notify=["notify"], paths="",
            discovered_options=[], discovered_value=[], discover_status="",
            editing_id=None, delete_confirm_class="modal-wrapper addmonitor-hidden",
            settings_class="modal-wrapper addmonitor-hidden",
        )
        return result()

    if trig == "detail-edit-btn":
        if not _edit_open:
            return result()  # button just (re)mounted when the detail panel opened, not a real click
        row = db.get_monitor(selected_id)
        if row is None:
            return result()
        out.update(
            wrapper_class="modal-wrapper addmonitor-open", error="",
            delete_confirm_class="modal-wrapper addmonitor-hidden",
            settings_class="modal-wrapper addmonitor-hidden",
            name=row["name"], url=row["url"], keyword=row["keyword"] or "",
            title="Edit Monitor", submit_label="Save Changes",
            type_value=[row["type"]], type_wrapper_class="form-field hidden",
            port=row["port"] if row["port"] is not None else "",
            interval=row["interval_sec"],
            retries=row["retries"] if row["retries"] is not None else "",
            timeout=row["timeout_sec"] if row["timeout_sec"] is not None else "",
            method=row["http_method"] or "GET",
            body=row["http_body"] or "",
            encoding=row["http_body_encoding"] or "json",
            notify=["notify"] if row["notify"] else [],
            paths="",  # editing is always a single row -- no fan-out, field is ignored on submit
            discovered_options=[], discovered_value=[], discover_status="",
            editing_id=selected_id,
        )
        return result()

    if trig in ("add-monitor-close", "add-monitor-cancel", "add-monitor-backdrop"):
        out.update(wrapper_class="modal-wrapper addmonitor-hidden")
        return result()

    if trig == "add-monitor-submit":
        name = (name or "").strip()
        url = (url or "").strip()
        types = types or ["website"]
        keyword = (keyword or "").strip() or None

        def error(msg):
            out.update(error=msg)
            return result()

        if not name or not types:
            return error("Name and at least one check type are required.")
        for mtype in types:
            if mtype != "push" and not url:
                return error("Target is required for this check type.")
            if mtype in ("website", "crm") and not (url.startswith("http://") or url.startswith("https://")):
                return error("URL must start with http:// or https://")
            if mtype == "tcp" and not port:
                return error("Port is required for TCP monitors.")
            if mtype == "push" and not interval:
                return error("Expected check-in interval is required for push monitors.")

        notify_enabled = bool(notify)

        if editing_id is not None:
            # Type is locked in edit mode (pre-filled, checklist hidden), so
            # `types` is always the single original type here.
            data.update_target(
                editing_id, name, _target_for_type(url, types[0]), keyword,
                interval or db.DEFAULT_CHECK_INTERVAL_SEC,
                port=port or None,
                retries=retries if retries not in (None, "") else None,
                timeout_sec=timeout if timeout not in (None, "") else None,
                http_method=method or "GET", http_body=body or None,
                http_body_encoding=encoding or "json", notify=notify_enabled,
            )
            out.update(
                wrapper_class="modal-wrapper addmonitor-hidden",
                summary=build_summary_or_banner(), grid=build_target_grid(filter_text),
                editing_id=None, detail_content=build_detail_content(editing_id),
            )
            return result()

        # Mock add — swap for POST /monitors later (§11). New monitor starts
        # "awaiting" (Decision 2.1) since nothing has checked it yet. Multiple
        # selected types, and/or multiple paths of the same site, each create
        # one independent monitor (same target host) rather than one monitor
        # checked several ways -- each already gets its own status/incidents/
        # timeline for free. They share a group_key so the grid renders them
        # as one card.
        paths = _merge_paths(discovered_checked, paths_text)
        entries = _build_entries(name, types, paths)
        group_key = secrets.token_hex(8) if len(entries) > 1 else None
        explicit_interval = interval if interval not in (None, "") else None
        # One icon lookup for the whole submission -- every entry shares the
        # same host, so N page monitors must not mean N homepage fetches.
        # Failure is non-fatal: the card falls back to its letter badge.
        try:
            site_favicon = page_discovery.discover_favicon(url) if url else None
        except Exception:
            site_favicon = None
        for entry in entries:
            target_url = _target_for_type(_url_with_path(url, entry["path"]), entry["type"])
            data.add_target(entry["name"], target_url, entry["type"], keyword=keyword, port=port or None,
                             interval_sec=_default_interval(entry["path"], explicit_interval),
                             retries=retries if retries not in (None, "") else None,
                             timeout_sec=timeout if timeout not in (None, "") else None,
                             http_method=method or "GET", http_body=body or None,
                             http_body_encoding=encoding or "json", group_key=group_key,
                             notify=notify_enabled, subrow_label=entry["subrow_label"],
                             favicon_url=site_favicon)
        out.update(wrapper_class="modal-wrapper addmonitor-hidden",
                   summary=build_summary_or_banner(), grid=build_target_grid(filter_text))
        return result()

    return result()


@app.callback(
    Output("delete-confirm-wrapper", "className"),
    Output("selected-target", "data", allow_duplicate=True),
    Output("summary-slot", "children", allow_duplicate=True),
    Output("grid-slot", "children", allow_duplicate=True),
    Output("add-monitor-wrapper", "className", allow_duplicate=True),
    Output("settings-wrapper", "className", allow_duplicate=True),
    Input("detail-delete-btn", "n_clicks"),
    Input("delete-confirm-cancel", "n_clicks"),
    Input("delete-confirm-backdrop", "n_clicks"),
    Input("delete-confirm-submit", "n_clicks"),
    State("selected-target", "data"),
    State("target-filter", "value"),
    prevent_initial_call=True,
)
def delete_monitor(_open, _cancel, _backdrop, _submit, selected_id, filter_text):
    trig = ctx.triggered_id
    if trig == "detail-delete-btn":
        if not _open:
            return (no_update,) * 6  # button just (re)mounted, not a real click
        # Force the Add/Edit and Settings modals closed too -- they share
        # identical centered-overlay CSS, so more than one open at once
        # (e.g. a fast double click across adjacent buttons) renders as one
        # stacked on top of the other.
        return ("modal-wrapper addmonitor-open", no_update, no_update, no_update,
                "modal-wrapper addmonitor-hidden", "modal-wrapper addmonitor-hidden")
    if trig in ("delete-confirm-cancel", "delete-confirm-backdrop"):
        return ("modal-wrapper addmonitor-hidden",) + (no_update,) * 5
    if trig == "delete-confirm-submit":
        data.delete_target(selected_id)
        return ("modal-wrapper addmonitor-hidden", None,
                build_summary_or_banner(), build_target_grid(filter_text), no_update, no_update)
    return (no_update,) * 6


@app.callback(
    Output("settings-wrapper", "className"),
    Output("settings-error", "children"),
    Output("settings-email-1", "value"),
    Output("settings-email-2", "value"),
    Output("settings-email-3", "value"),
    Output("settings-notify-enabled", "value"),
    Output("add-monitor-wrapper", "className", allow_duplicate=True),
    Output("delete-confirm-wrapper", "className", allow_duplicate=True),
    Input("settings-open", "n_clicks"),
    Input("settings-close", "n_clicks"),
    Input("settings-cancel", "n_clicks"),
    Input("settings-backdrop", "n_clicks"),
    Input("settings-submit", "n_clicks"),
    State("settings-email-1", "value"),
    State("settings-email-2", "value"),
    State("settings-email-3", "value"),
    State("settings-notify-enabled", "value"),
    prevent_initial_call=True,
)
def open_edit_settings(_open, _close, _cancel, _backdrop, _submit, email_1, email_2, email_3, enabled):
    trig = ctx.triggered_id
    if trig == "settings-open":
        if not _open:
            return (no_update,) * 8  # gear button lives in summary-slot, remounted on every
            # periodic refresh (build_summary_or_banner) -- same phantom-trigger shape as
            # detail-edit-btn/detail-delete-btn, guarded the same way.
        row = db.get_settings()
        emails = (row["email_1"] or "", row["email_2"] or "", row["email_3"] or "") if row else ("", "", "")
        notify_value = ["enabled"] if (row is None or row["notify_enabled"]) else []
        # Force the Add/Edit and Delete-confirm modals closed too -- same
        # stacked-overlay risk as the Edit/Delete guard above.
        return ("modal-wrapper addmonitor-open", "", *emails, notify_value,
                "modal-wrapper addmonitor-hidden", "modal-wrapper addmonitor-hidden")

    if trig in ("settings-close", "settings-cancel", "settings-backdrop"):
        return ("modal-wrapper addmonitor-hidden", no_update, no_update, no_update, no_update, no_update,
                no_update, no_update)

    if trig == "settings-submit":
        emails = [(e or "").strip() for e in (email_1, email_2, email_3)]
        for e in emails:
            if e and not EMAIL_RE.match(e):
                return (no_update, "Enter a valid email address.", no_update, no_update, no_update,
                        no_update, no_update, no_update)
        e1, e2, e3 = (e or None for e in emails)
        db.update_settings(e1, e2, e3, bool(enabled))
        return ("modal-wrapper addmonitor-hidden", "", no_update, no_update, no_update, no_update,
                no_update, no_update)

    return (no_update,) * 8


if __name__ == "__main__":
    debug = "--debug" in sys.argv
    app.server.secret_key = _load_or_create_secret_key()  # persist across real restarts (L1)
    monitor_engine.start_background_scheduler(debug=debug)
    # Resolve site icons for any monitor that doesn't have one yet. Daemon
    # thread so it never delays startup or blocks shutdown; cards render
    # letter badges until it finishes, then pick up icons on the next tick.
    threading.Thread(target=data.backfill_favicons, daemon=True).start()
    if debug:
        # Dash's own dev server -- hot reload + the Dash debug UI. Flask's
        # dev server underneath prints its own "do not use in production"
        # warning on every start, which is correct: it's single-threaded and
        # not what should be facing the public ngrok tunnel.
        app.run(debug=True, port=8050)
    else:
        from waitress import serve
        serve(app.server, host="0.0.0.0", port=8050, threads=8)
