"""TDD self-check for page-level monitoring: one Add Monitor submission can
fan out into several monitors for different paths of the same site (and/or
several check types), sharing a group_key and a distinct subrow_label so the
grouped card can tell pages apart instead of repeating the same type label.

Run: ./venv/Scripts/python.exe test_page_monitoring.py
"""
import os
import sqlite3

import db

db.DB_PATH = "test_page_monitoring.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

# --- migration adds subrow_label without dropping existing data ---
check_conn = sqlite3.connect(db.DB_PATH)
cols = {row[1] for row in check_conn.execute("PRAGMA table_info(monitors)").fetchall()}
check_conn.close()
assert "subrow_label" in cols, f"subrow_label missing: {cols}"

# --- db.add_monitor / data.add_target thread subrow_label through ---
import data

plain_id = data.add_target("plain site", "https://plain.example", "website")
assert db.get_monitor(plain_id)["subrow_label"] is None

about_id = data.add_target("brand — /about", "https://brand.example/about", "website",
                            group_key="grp1", subrow_label="/about")
assert db.get_monitor(about_id)["subrow_label"] == "/about"

targets = {t["id"]: t for t in data.get_targets()}
assert targets[plain_id]["subrow_label"] is None
assert targets[about_id]["subrow_label"] == "/about"

# --- _parse_paths: blank input means "no fan-out" ---
from app import (_parse_paths, _url_with_path, _default_interval, _build_entries,
                  _strip_type_suffix, build_target_card)

assert _parse_paths("") == [None]
assert _parse_paths("   \n  \n") == [None]
assert _parse_paths("/about") == ["/about"]
assert _parse_paths("/\n/about\n/contact") == ["/", "/about", "/contact"]
assert _parse_paths("/about\n\n/contact") == ["/about", "/contact"]  # blank lines skipped

# --- _url_with_path joins cleanly regardless of slashes ---
assert _url_with_path("https://example.com", None) == "https://example.com"
assert _url_with_path("https://example.com", "/about") == "https://example.com/about"
assert _url_with_path("https://example.com/", "/about") == "https://example.com/about"
assert _url_with_path("https://example.com", "about") == "https://example.com/about"
assert _url_with_path("https://example.com", "/") == "https://example.com/"

# --- _default_interval: root path keeps 60s, sub-pages default slower, explicit always wins ---
assert _default_interval(None, None) == 60
assert _default_interval("/", None) == 60
assert _default_interval("/about", None) == 300
assert _default_interval("/about", 45) == 45
assert _default_interval(None, 120) == 120

# --- _build_entries: single type, no paths -- untouched (the common case) ---
entries = _build_entries("Homepage", ["website"], [None])
assert entries == [{"type": "website", "path": None, "name": "Homepage", "subrow_label": None}]

# --- _build_entries: multi-type only (existing behavior, unchanged) ---
entries = _build_entries("brand", ["website", "tcp"], [None])
assert entries == [
    {"type": "website", "path": None, "name": "brand (Website)", "subrow_label": None},
    {"type": "tcp", "path": None, "name": "brand (TCP)", "subrow_label": None},
]

# --- _build_entries: multi-path, single website type -- subrow_label is the path ---
entries = _build_entries("Vrittispace", ["website"], ["/", "/about"])
assert entries == [
    {"type": "website", "path": "/", "name": "Vrittispace — /", "subrow_label": "/"},
    {"type": "website", "path": "/about", "name": "Vrittispace — /about", "subrow_label": "/about"},
]

# --- _build_entries: paths ignored for non-http types (tcp/dns/push) ---
entries = _build_entries("brand", ["website", "tcp"], ["/", "/about"])
assert entries == [
    {"type": "website", "path": "/", "name": "brand (Website) — /", "subrow_label": "Website /"},
    {"type": "website", "path": "/about", "name": "brand (Website) — /about", "subrow_label": "Website /about"},
    {"type": "tcp", "path": None, "name": "brand (TCP)", "subrow_label": None},
]

# --- _strip_type_suffix strips a path suffix, a type suffix, both, or neither ---
assert _strip_type_suffix("Vrittispace — /about") == "Vrittispace"
assert _strip_type_suffix("brand (Website) — /about") == "brand"
assert _strip_type_suffix("brand (Website)") == "brand"
assert _strip_type_suffix("plain name") == "plain name"

# --- build_target_card: grouped subrows show the distinguishing path, not a
# repeated identical "Website" label ---
contact_id = data.add_target("Vrittispace — /contact", "https://vrittispace.example/contact", "website",
                              group_key="grp2", subrow_label="/contact")
about2_id = data.add_target("Vrittispace — /about", "https://vrittispace.example/about", "website",
                             group_key="grp2", subrow_label="/about")
db.update_monitor_state(contact_id, "down", 3, 0, 1000.0, None)
db.update_monitor_state(about2_id, "up", 0, 2, 1000.0, 40)
refreshed = {t["id"]: t for t in data.get_targets()}
group = [refreshed[contact_id], refreshed[about2_id]]
card = build_target_card(group)
assert "status-down" in card.className, card.className  # worst-of-group status, still free

subrow_labels = []
for child in card.children:
    if getattr(child, "id", None) and child.id.get("type") == "tcard":
        subrow_labels.append(child.children[1].children)  # dot, label, status[, time]
assert "/contact" in subrow_labels, subrow_labels
assert "/about" in subrow_labels, subrow_labels

os.remove(db.DB_PATH)
print("All page-monitoring checks passed.")
