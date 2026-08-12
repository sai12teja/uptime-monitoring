"""TDD self-check for the monitor search/filter.

Covers the two bugs this actually shipped with:
  1. build_target_grid() ignored filter_text in the normal grouped view, so
     the box was inert no matter what you typed.
  2. dcc.Input debounce is in SECONDS. It was set to 300, i.e. a five-minute
     wait after the last keystroke -- indistinguishable from "filter broken".

Run: ./venv/Scripts/python.exe test_filter.py
"""
import os

import db

db.DB_PATH = "test_filter.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data
import app

# Two sites, one grouped (3 checks) + one solo, so filtering is exercised
# against both card shapes.
web = data.add_target("medinest (Website)", "https://medinest.in", "website", group_key="g1")
tcp = data.add_target("medinest (TCP)", "medinest.in", "tcp", port=443, group_key="g1")
dns = data.add_target("medinest (DNS)", "medinest.in", "dns", group_key="g1")
other = data.add_target("m7-energybooster", "https://m7-energybooster.com", "website")

db.update_monitor_state(web, "down", 3, 0, 1.0, 10)
db.update_monitor_state(tcp, "up", 0, 2, 1.0, 10)
db.update_monitor_state(dns, "up", 0, 2, 1.0, 10)
db.update_monitor_state(other, "down", 3, 0, 1.0, 10)

groups = app._group_targets(data.get_targets())
assert len(groups) == 2, groups


def names(filtered):
    return sorted(app._strip_type_suffix(g[0]["name"]) for g in filtered)


# --- name match narrows to one card, and keeps the WHOLE group together ---
hit = app._filter_groups(groups, "medinest")
assert names(hit) == ["medinest"], names(hit)
assert len(hit[0]) == 3, "matching one check must keep all 3 checks on the card"

# --- the other site must be EXCLUDED (the original bug: nothing narrowed) ---
assert "m7-energybooster" not in names(hit)

# --- domain, type and status are searchable too, not just the name ---
assert names(app._filter_groups(groups, "medinest.in")) == ["medinest"]
assert names(app._filter_groups(groups, "dns")) == ["medinest"]
assert len(app._filter_groups(groups, "down")) == 2, "both sites have a failing check"

# --- multi-word is AND across the group ---
assert names(app._filter_groups(groups, "medinest dns")) == ["medinest"]
assert app._filter_groups(groups, "medinest nosuchthing") == []

# --- empty/whitespace shows everything, never an empty grid ---
for blank in ("", "   ", None):
    assert len(app._filter_groups(groups, blank)) == 2, repr(blank)

# --- case-insensitive ---
assert names(app._filter_groups(groups, "MEDINEST")) == ["medinest"]

# --- no match is empty, not everything ---
assert app._filter_groups(groups, "zzzzz") == []

# --- build_target_grid must APPLY the filter, not just accept the argument.
# This is the bug that made the box inert in the normal grouped view. ---
grid = app.build_target_grid("medinest")
assert len(grid.children) == 1, f"grid should render 1 card, got {len(grid.children)}"
assert len(app.build_target_grid("").children) == 2

# --- debounce must be a sub-second value. dcc.Input treats a bare number as
# SECONDS, so 300 meant a five-minute delay before anything was sent. ---
def _find_filter(node):
    if getattr(node, "id", None) == "target-filter":
        return node
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            found = _find_filter(child)
            if found is not None:
                return found
    elif children is not None:
        # Recurse into a lone child unconditionally -- gating this on
        # hasattr(children, "children") stops at any component that has no
        # children of its own, which is exactly where the Input lives.
        return _find_filter(children)
    return None


# app.layout is a FUNCTION here (rebuilt per page load), so call it to get
# the real component tree before searching it.
layout = app.app.layout
layout = layout() if callable(layout) else layout
filter_input = _find_filter(layout)
assert filter_input is not None, "filter input must exist in the layout"
debounce = filter_input.debounce
assert isinstance(debounce, (int, float)) and not isinstance(debounce, bool), debounce
assert 0 < debounce <= 1, f"debounce is in SECONDS -- {debounce} is far too long"

os.remove(db.DB_PATH)
print("All filter checks passed.")
