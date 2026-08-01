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
from datetime import datetime
from urllib.parse import urlparse

import flask
from dash import Dash, html, dcc, Output, Input, State, ctx, ALL, no_update

import api
import auth
import data
import db
import monitor_engine
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

def build_summary_or_banner():
    if data.is_correlated_outage():
        return html.Div(
            className="outage-banner",
            children="⚠ SERVER UNREACHABLE — ALL CHECKS FAILING",
        )

    counts = data.summary_counts()
    bar = html.Div(
        className="summary-bar calm",
        children=[
            html.H1("Rovix Uptime Monitoring", className="brand"),
            html.Div(
                className="counts",
                children=[
                    html.Strong(f"{counts['up']} up"), " · ",
                    html.Strong(f"{counts['down']} down"), " · ",
                    "server: ", html.Strong("healthy"),
                    html.Span(f" · updated {datetime.now().strftime('%H:%M:%S')}", className="updated-at"),
                ],
            ),
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
    for label in TYPE_LABELS.values():
        suffix = f" ({label})"
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


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
        return html.Button(
            id={"type": "tcard", "index": target["id"]},
            className=f"target-card clickable status-{status}",
            n_clicks=0,
            children=[
                html.Div(target["name"], className="target-card-name"),
                html.Div(
                    className=f"target-card-status status-{status}",
                    children=[
                        html.Span(className="status-dot"),
                        html.Span(STATUS_LABEL[status], className="status-label"),
                    ],
                ),
                html.Div(meta_text, className="target-card-meta"),
            ],
        )

    worst_status = min(group, key=lambda t: STATUS_ORDER[t["status"]])["status"]
    base_name = _strip_type_suffix(group[0]["name"])

    def subrow(t):
        # Same information as a solo card's meta line (status + response
        # time), just laid out per check type -- a grouped card shouldn't
        # tell you less than the cards it replaces.
        children = [
            html.Span(className="status-dot"),
            html.Span(TYPE_LABELS.get(t["type"], t["type"].title()), className="target-card-subtype"),
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

    return html.Div(
        className=f"target-card status-{worst_status}",
        children=[html.Div(base_name, className="target-card-name")] + rows,
    )


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
SCALE_THRESHOLD = 20  # Decision 7.2 — grouping/filter chrome isn't worth it below this


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

def serve_layout():
    return html.Div(
        className="dashboard",
        children=[
            html.Div(id="live-region", **{"aria-live": "polite"}, style={
                "position": "absolute", "width": "1px", "height": "1px", "overflow": "hidden"
            }),
            dcc.Store(id="selected-target"),
            dcc.Store(id="editing-monitor-id"),
            # session storage — survives a page reload so a status change
            # picked up on the next tick is still diffed against the last
            # known baseline, not reset to "everything just changed."
            dcc.Store(id="prev-state", storage_type="session"),
            # Initial render is a skeleton everywhere (§13.2) — the one-shot
            # initial-load-timer below swaps each slot for real content
            # shortly after, same as a real API call would.
            html.Div(id="summary-slot", children=build_skeleton_summary()),
            html.Div(
                className="section-heading-row",
                children=[
                    html.H2("Monitored targets", className="section-heading"),
                    html.Button("+ Add Monitor", id="add-monitor-open", className="ack-btn", n_clicks=0),
                ],
            ),
            # Always mounted (never conditionally created/destroyed) — hidden
            # via CSS below the scale threshold. Keeping one real component
            # in the DOM at all times means exactly one callback can safely
            # own grid-slot; a component that only sometimes exists forces
            # either a client-side "not found" error or a second writer with
            # a race against the periodic refresh (both tried, both worse).
            html.Div(
                id="target-filter-wrapper",
                className="target-filter-wrapper hidden",
                children=dcc.Input(id="target-filter", type="text", placeholder="Filter by name…",
                                    className="target-filter", value="", debounce=True),
            ),
            html.Div(id="grid-slot", children=build_skeleton_grid()),
            html.H2("Server health", className="section-heading"),
            html.Div(id="server-panel-slot", children=build_skeleton_server_panel()),
            html.H2("Incident history", className="section-heading"),
            html.Div(id="incident-slot", children=build_skeleton_incident_table()),
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
                                    dcc.Input(id="add-monitor-url", type="text",
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
                                id="add-monitor-port-wrapper",
                                className="form-field hidden",
                                children=[
                                    html.Label("Port", htmlFor="add-monitor-port", className="form-label"),
                                    dcc.Input(id="add-monitor-port", type="number",
                                              placeholder="e.g. 5432", className="form-input", value=""),
                                ],
                            ),
                            html.Div(
                                id="add-monitor-interval-wrapper",
                                className="form-field hidden",
                                children=[
                                    html.Label("Expected check-in interval (seconds)",
                                                htmlFor="add-monitor-interval", className="form-label"),
                                    dcc.Input(id="add-monitor-interval", type="number",
                                              placeholder="e.g. 86400 for a daily job",
                                              className="form-input", value=""),
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
                                id="add-monitor-retries-wrapper",
                                className="form-field",
                                children=[
                                    html.Label("Retries (extra fails allowed before marking down)",
                                                htmlFor="add-monitor-retries", className="form-label"),
                                    dcc.Input(id="add-monitor-retries", type="number",
                                              placeholder="e.g. 2 (default)", className="form-input", value=""),
                                ],
                            ),
                            html.Div(
                                id="add-monitor-timeout-wrapper",
                                className="form-field",
                                children=[
                                    html.Label("Request timeout (seconds)",
                                                htmlFor="add-monitor-timeout", className="form-label"),
                                    dcc.Input(id="add-monitor-timeout", type="number",
                                              placeholder="e.g. 10 (default)", className="form-input", value=""),
                                ],
                            ),
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
                                id="add-monitor-body-wrapper",
                                className="form-field hidden",
                                children=[
                                    html.Label("Body (optional, for non-GET methods)",
                                                htmlFor="add-monitor-body", className="form-label"),
                                    dcc.Textarea(id="add-monitor-body", className="form-input",
                                                 placeholder='e.g. {"key": "value"}', value=""),
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
    )


SESSION_KEY_PATH = ".session_secret.key"


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
    Output("summary-slot", "children"),
    Output("grid-slot", "children"),
    Output("server-panel-slot", "children"),
    Output("incident-slot", "children"),
    Output("target-filter-wrapper", "className"),
    Input("refresh-interval", "n_intervals"),
    Input("initial-load-timer", "n_intervals"),
    Input("target-filter", "value"),
)
def refresh(_n, _initial, filter_text):
    # Re-reads data.py (real DB-backed checks now) on every tick (periodic
    # refresh, the one-shot initial load, or a filter keystroke).
    # target-filter is always mounted (see layout comment), so this single
    # callback can own grid-slot with no race and no client-side "component
    # not found" error regardless of target count.
    is_scale = len(data.get_targets()) > SCALE_THRESHOLD
    filter_class = "target-filter-wrapper" if is_scale else "target-filter-wrapper hidden"
    return (
        build_summary_or_banner(),
        build_target_grid(filter_text),
        build_server_panel_inner(),
        build_incident_table_inner(),
        filter_class,
    )


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
    Input("add-monitor-type", "value"),
)
def toggle_type_fields(selected_types):
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
    interval_cls = "form-field" if is_push else "form-field hidden"
    retries_cls = "form-field" if has_active_type else "form-field hidden"
    timeout_cls = "form-field" if has_active_type else "form-field hidden"
    method_cls = "form-field" if is_http else "form-field hidden"
    body_cls = "form-field" if is_http else "form-field hidden"
    return (target_cls, port_cls, keyword_cls, interval_cls,
            retries_cls, timeout_cls, method_cls, body_cls)


TYPE_LABELS = {"website": "Website", "crm": "CRM", "tcp": "TCP", "dns": "DNS", "push": "Push"}


def _target_for_type(url, mtype):
    """DNS/TCP checks need a bare hostname; website/crm need the full URL
    with scheme. Multi-select shares one Target field across all selected
    types, so strip the scheme for DNS/TCP if the user typed a full URL."""
    if mtype in ("tcp", "dns") and "://" in url:
        return urlparse(url).hostname or url
    return url


# Dict-based output handling — this callback now serves both Add and Edit
# (same modal, same fields; Edit just pre-fills them and locks the check
# type), so each branch only needs to name the handful of keys it actually
# changes instead of a 19-wide positional tuple.
ADD_EDIT_OUTPUT_IDS = [
    "wrapper_class", "error", "name", "url", "keyword", "summary", "grid",
    "title", "submit_label", "type_value", "type_wrapper_class",
    "port", "interval", "retries", "timeout", "method", "body", "encoding", "notify",
    "editing_id", "detail_content", "delete_confirm_class", "settings_class",
]


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
    State("target-filter", "value"),
    State("selected-target", "data"),
    State("editing-monitor-id", "data"),
    prevent_initial_call=True,
)
def add_edit_monitor(_open, _close, _cancel, _backdrop, _submit, _edit_open,
                      name, url, types, keyword, port, interval, retries, timeout,
                      method, body, encoding, notify, filter_text, selected_id, editing_id):
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
            notify=["notify"],
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
                editing_id, name, _target_for_type(url, types[0]), keyword, interval or 60,
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
        # selected types create one independent monitor per type (same
        # target) rather than one monitor checked multiple ways -- each
        # already gets its own status/incidents/timeline for free. They
        # share a group_key so the grid renders them as one card.
        suffix = len(types) > 1
        group_key = secrets.token_hex(8) if suffix else None
        for mtype in types:
            entry_name = f"{name} ({TYPE_LABELS[mtype]})" if suffix else name
            data.add_target(entry_name, _target_for_type(url, mtype), mtype, keyword=keyword, port=port or None,
                             interval_sec=interval or 60,
                             retries=retries if retries not in (None, "") else None,
                             timeout_sec=timeout if timeout not in (None, "") else None,
                             http_method=method or "GET", http_body=body or None,
                             http_body_encoding=encoding or "json", group_key=group_key,
                             notify=notify_enabled)
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
    app.run(debug=debug, port=8050)
