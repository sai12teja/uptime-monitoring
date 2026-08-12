"""TDD self-check for the grouped-card redesign: collapsed-by-default
<details>/<summary> card (front shows only the site name, sub-rows reveal
on click/expand) and the new 3-tier health color (green only if every
sub-monitor is up, red only if every sub-monitor is down, grey for any
real mix -- replacing the old "any one down paints the whole card red"
worst-status-wins rule).

Run: ./venv/Scripts/python.exe test_card_redesign.py
"""
import os
import time

import db

db.DB_PATH = "test_card_redesign.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data
from app import _group_status_tier, _group_summary_text, build_target_card
from dash import html

# --- _group_status_tier: pure 3-tier classification ---
assert _group_status_tier([{"status": "up"}, {"status": "up"}]) == "up"
assert _group_status_tier([{"status": "down"}, {"status": "down"}]) == "down"
assert _group_status_tier([{"status": "up"}, {"status": "down"}]) == "mixed"
assert _group_status_tier([{"status": "up"}, {"status": "awaiting"}]) == "mixed"
assert _group_status_tier([{"status": "down"}, {"status": "overdue"}]) == "mixed"
assert _group_status_tier([{"status": "up"}]) == "up"  # trivial single-item case

# --- _group_summary_text: the collapsed card's at-a-glance meta line. Gives
# a grouped card real body/height (it used to render name-only, which made
# it a squat bar next to 3-line solo cards) AND says something useful
# without dumping the whole sub-row list. Down count always wins the
# summary -- that's the number you care about first. ---
assert _group_summary_text([{"status": "up"}, {"status": "up"}]) == "2 checks · all up"
assert _group_summary_text([{"status": "down"}, {"status": "down"}]) == "2 checks · all down"
assert _group_summary_text([{"status": "up"}, {"status": "down"}]) == "2 checks · 1 down"
assert _group_summary_text([{"status": "down"}, {"status": "down"}, {"status": "up"}]) == "3 checks · 2 down"
# no downs but not all up (awaiting/overdue) -- report what IS up, never
# imply "all up" when something hasn't reported yet
assert _group_summary_text([{"status": "up"}, {"status": "awaiting"}]) == "2 checks · 1 up"

# --- build_target_card: solo card wraps a real <button> in a <div>.
# The wrapper exists because the site link is an <a>, which cannot legally
# nest inside a <button> -- so the clickable hit area is the inner button and
# the link is its sibling. Asserted by BEHAVIOUR (a real <button> carrying the
# tcard id is present) rather than by the card's outermost tag, so the next
# layout change doesn't break this test for no reason. ---
solo_id = data.add_target("solo site", "https://solo.example", "website")
db.update_monitor_state(solo_id, "up", 0, 2, time.time(), 50)
solo_target = {t["id"]: t for t in data.get_targets()}[solo_id]
solo_card = build_target_card([solo_target])


def _find(node, predicate, out=None):
    out = [] if out is None else out
    if predicate(node):
        out.append(node)
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            _find(child, predicate, out)
    elif children is not None and hasattr(children, "children"):
        _find(children, predicate, out)
    return out


buttons = _find(solo_card, lambda n: isinstance(n, html.Button))
assert len(buttons) == 1, buttons
assert buttons[0].id == {"type": "tcard", "index": solo_id}, buttons[0].id
# The <a> must never end up inside the <button> -- invalid HTML, and browsers
# disagree on which element a click activates.
assert not _find(buttons[0], lambda n: isinstance(n, html.A)), "link must not nest inside the button"

# --- build_target_card: grouped card is now a <details>, closed by default,
# with the site name as the <summary> (front-of-card, per the redesign ask) ---
web_id = data.add_target("brand (Website)", "https://brand.example", "website", group_key="grp1")
tcp_id = data.add_target("brand (TCP)", "brand.example", "tcp", port=443, group_key="grp1")
db.update_monitor_state(web_id, "up", 0, 2, time.time(), 50)
db.update_monitor_state(tcp_id, "up", 0, 2, time.time(), 20)
targets = {t["id"]: t for t in data.get_targets()}
group = [targets[web_id], targets[tcp_id]]

card = build_target_card(group)
assert isinstance(card, html.Details), type(card)
assert card.open is False, "must be collapsed by default -- sub-rows stay hidden"

# Summary is the ENTIRE collapsed face -- it holds both the title row and the
# meta/link row, because a collapsed <details> renders only its first child.
# Anything left outside the summary is invisible until the card is expanded.
summary = card.children[0]
assert isinstance(summary, html.Summary), type(summary)

title = _find(summary, lambda n: getattr(n, "className", None) == "target-card-title")
assert len(title) == 1, title
assert title[0].children == "brand", title[0].children

# Meta line lives inside the summary too, so the collapsed card shows its
# check summary rather than just a bare name.
meta = _find(summary, lambda n: getattr(n, "className", None) == "target-card-meta")
assert len(meta) == 1, meta
assert meta[0].children == "2 checks · all up", meta[0].children

# --- all up -> green tier ---
assert "status-up" in card.className, card.className

# --- sub-rows live in their own scrollable container, a sibling of the
# summary (found by class, not index -- the meta row moved inside the summary
# and shifted every positional index by one) ---
wrappers = _find(card, lambda n: getattr(n, "className", None) == "target-card-subrows")
assert len(wrappers) == 1, wrappers
subrows_wrapper = wrappers[0]
subrow_ids = {c.id["index"] for c in subrows_wrapper.children if getattr(c, "id", None)}
assert subrow_ids == {web_id, tcp_id}

# --- all down -> red tier ---
db.update_monitor_state(web_id, "down", 3, 0, 1000.0, None)
db.update_monitor_state(tcp_id, "down", 3, 0, 1000.0, None)
targets = {t["id"]: t for t in data.get_targets()}
card_all_down = build_target_card([targets[web_id], targets[tcp_id]])
assert "status-down" in card_all_down.className, card_all_down.className

# --- one up, one down -> grey (mixed) tier, NOT red -- this is the actual
# behavior change: previously worst-status-wins painted this red ---
db.update_monitor_state(web_id, "up", 0, 2, time.time(), 50)
targets = {t["id"]: t for t in data.get_targets()}
card_mixed = build_target_card([targets[web_id], targets[tcp_id]])
assert "status-mixed" in card_mixed.className, card_mixed.className
assert "status-down" not in card_mixed.className
assert "status-up" not in card_mixed.className

os.remove(db.DB_PATH)
print("All card-redesign checks passed.")
