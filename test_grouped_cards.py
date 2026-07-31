"""TDD self-check for grouped-card rendering: monitors created from one
multi-select Add Monitor submission share a group_key and render as one
dashboard card instead of N separate cards.

Run: ./venv/Scripts/python.exe test_grouped_cards.py
"""
import os
import sqlite3

import db

db.DB_PATH = "test_grouped_cards.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

# --- migration adds group_key without dropping existing data ---
check_conn = sqlite3.connect(db.DB_PATH)
cols = {row[1] for row in check_conn.execute("PRAGMA table_info(monitors)").fetchall()}
check_conn.close()
assert "group_key" in cols, f"group_key missing: {cols}"

# --- db.add_monitor / data.add_target thread group_key through ---
import data

solo_id = data.add_target("solo site", "https://solo.example", "website")
row = db.get_monitor(solo_id)
assert row["group_key"] is None

web_id = data.add_target("brand (Website)", "https://brand.example", "website", group_key="grp1")
tcp_id = data.add_target("brand (TCP)", "brand.example", "tcp", port=443, group_key="grp1")
assert db.get_monitor(web_id)["group_key"] == "grp1"
assert db.get_monitor(tcp_id)["group_key"] == "grp1"

targets = {t["id"]: t for t in data.get_targets()}
assert targets[solo_id]["group_key"] is None
assert targets[web_id]["group_key"] == "grp1"
assert targets[tcp_id]["group_key"] == "grp1"

# --- app._group_targets partitions solo vs shared-group_key targets ---
from app import _group_targets, _strip_type_suffix, build_target_card

all_targets = list(targets.values())
groups = _group_targets(all_targets)
assert len(groups) == 2, groups
group_ids = {frozenset(t["id"] for t in g) for g in groups}
assert frozenset([solo_id]) in group_ids
assert frozenset([web_id, tcp_id]) in group_ids

# --- _strip_type_suffix strips known suffixes, leaves plain names alone ---
assert _strip_type_suffix("brand (Website)") == "brand"
assert _strip_type_suffix("brand (TCP)") == "brand"
assert _strip_type_suffix("plain name") == "plain name"

# --- build_target_card: single target renders unchanged (clickable button, tcard id) ---
card = build_target_card([targets[solo_id]])
assert card.id == {"type": "tcard", "index": solo_id}
assert "target-card" in card.className and "clickable" in card.className

# --- build_target_card: grouped targets render one card with a subrow per type ---
db.update_monitor_state(web_id, "up", 0, 2, 1000.0, 50)
db.update_monitor_state(tcp_id, "down", 3, 0, 1000.0, None)
refreshed = {t["id"]: t for t in data.get_targets()}
group_targets = [refreshed[web_id], refreshed[tcp_id]]

grouped_card = build_target_card(group_targets)
assert "status-down" in grouped_card.className, grouped_card.className  # worst-of-group status
subrows = [c for c in grouped_card.children if getattr(c, "id", None) and c.id.get("type") == "tcard"]
assert len(subrows) == 2
subrow_ids = {c.id["index"] for c in subrows}
assert subrow_ids == {web_id, tcp_id}

os.remove(db.DB_PATH)
print("All grouped-card checks passed.")
