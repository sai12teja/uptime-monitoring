"""TDD self-check for the new dashboard stat-card data (visual redesign,
inspired by a reference dashboard's stat-card row): monitors-by-type
breakdown, incident count in a rolling time window, and average incident
resolution time (the honest equivalent of "MTTR" using real resolved
incidents, never a fabricated number).

Run: ./venv/Scripts/python.exe test_dashboard_stats.py
"""
import os
import sqlite3
import time

import db

db.DB_PATH = "test_dashboard_stats.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data


def _backdate_incident(incident_id, started_ago_sec, resolved_ago_sec=None):
    conn = sqlite3.connect(db.DB_PATH)
    started = time.time() - started_ago_sec
    resolved = (time.time() - resolved_ago_sec) if resolved_ago_sec is not None else None
    conn.execute("UPDATE incidents SET started = ?, resolved = ? WHERE id = ?", (started, resolved, incident_id))
    conn.commit()
    conn.close()


# --- monitors_by_type: counts active monitors per type ---
data.add_target("site1", "https://a.example", "website")
data.add_target("site2", "https://b.example", "website")
tcp_id = data.add_target("site3", "c.example", "tcp", port=443)
data.add_target("site4", "d.example", "dns")

counts = data.monitors_by_type()
assert counts == {"website": 2, "tcp": 1, "dns": 1}, counts

# soft-deleted monitors don't count (matches gap 5's active=1 filtering)
data.delete_target(tcp_id)
counts = data.monitors_by_type()
assert counts == {"website": 2, "dns": 1}, counts

# --- incidents_in_window: counts incidents STARTED within the window ---
mid = data.add_target("mon", "https://mon.example", "website")
recent_id = db.open_incident(mid, "mon", "HTTP 500")
_backdate_incident(recent_id, started_ago_sec=30 * 60)  # 30 min ago

old_id = db.open_incident(mid, "mon", "HTTP 500", problem_type="ssl")
_backdate_incident(old_id, started_ago_sec=10 * 24 * 3600)  # 10 days ago

assert data.incidents_in_window(1) == 1, data.incidents_in_window(1)
assert data.incidents_in_window(24) == 1, data.incidents_in_window(24)
assert data.incidents_in_window(24 * 14) == 2, data.incidents_in_window(24 * 14)  # both within 14 days

# --- avg_incident_duration: average resolution time across resolved
# incidents, or None (never a fabricated 0) if nothing has resolved yet ---
assert data.avg_incident_duration() is None

_backdate_incident(recent_id, started_ago_sec=3600, resolved_ago_sec=1800)  # took 1800s
_backdate_incident(old_id, started_ago_sec=7200, resolved_ago_sec=3600)     # took 3600s
avg = data.avg_incident_duration()
assert avg == 2700, avg  # (1800 + 3600) / 2

os.remove(db.DB_PATH)

# --- pure presentation helpers: MTTR display text + active-window button class ---
from app import _format_mttr, _window_button_class, _server_status_label

assert _format_mttr(None) == "—"  # no resolved incidents yet -- never a fabricated 0
assert _format_mttr(2700) == "45m"

assert _window_button_class(24, 24) == "window-btn active"
assert _window_button_class(1, 24) == "window-btn"

# --- _server_status_label: real thresholds, not a hardcoded "healthy" ---
assert _server_status_label({"cpu_pct": 10, "mem_pct": 20, "disk_pct": 30, "inodes_pct": 5}) == "healthy"
assert _server_status_label({"cpu_pct": 10, "mem_pct": 20, "disk_pct": 85, "inodes_pct": 5}) == "degraded"
assert _server_status_label({"cpu_pct": 10, "mem_pct": 20, "disk_pct": 95.1, "inodes_pct": 5}) == "critical"
assert _server_status_label({"cpu_pct": None, "mem_pct": None, "disk_pct": None, "inodes_pct": None}) == "unknown"

print("All dashboard-stats checks passed.")
