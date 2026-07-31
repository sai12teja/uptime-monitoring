"""TDD self-check for allowing Push/Passive to join a multi-select group
with active check types (Website/CRM/TCP/DNS), instead of being rejected
outright. Push has no target of its own, so it rides along on whatever
target the active type(s) in the same submission are checking -- it just
adds a passive check-in row to the same grouped card.

Run: ./venv/Scripts/python.exe test_push_grouping.py
"""
import os

import db

db.DB_PATH = "test_push_grouping.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data
from dash._callback_context import context_value
from dash._utils import AttributeDict

import app
from app import build_target_card, toggle_type_fields
from dash import no_update


def _fire(component_id):
    context_value.set(AttributeDict({"triggered_inputs": [{"prop_id": f"{component_id}.n_clicks", "value": 1}]}))


def _call_add_edit(edit_open=None, submit=None, **state):
    defaults = dict(name=None, url=None, types=None, keyword=None, port=None, interval=None,
                     retries=None, timeout=None, method=None, body=None, encoding=None, notify=None,
                     filter_text=None, selected_id=None, editing_id=None)
    defaults.update(state)
    result = app.add_edit_monitor(
        None, None, None, None, submit, edit_open,
        defaults["name"], defaults["url"], defaults["types"], defaults["keyword"],
        defaults["port"], defaults["interval"], defaults["retries"], defaults["timeout"],
        defaults["method"], defaults["body"], defaults["encoding"], defaults["notify"],
        defaults["filter_text"], defaults["selected_id"], defaults["editing_id"],
    )
    return dict(zip(app.ADD_EDIT_OUTPUT_IDS, result))


# ---------- toggle_type_fields: combined selection shows both field sets ----------

# Push alone: unchanged from before -- only interval, everything else hidden.
target_cls, port_cls, keyword_cls, interval_cls, retries_cls, timeout_cls, method_cls, body_cls = \
    toggle_type_fields(["push"])
assert target_cls == "form-field hidden"
assert keyword_cls == "form-field hidden"
assert retries_cls == "form-field hidden"
assert timeout_cls == "form-field hidden"
assert interval_cls == "form-field"

# Website alone: unchanged from before -- target/keyword/retries/timeout show, interval hidden.
target_cls, port_cls, keyword_cls, interval_cls, retries_cls, timeout_cls, method_cls, body_cls = \
    toggle_type_fields(["website"])
assert target_cls == "form-field"
assert keyword_cls == "form-field"
assert retries_cls == "form-field"
assert timeout_cls == "form-field"
assert interval_cls == "form-field hidden"

# Website + Push combined: BOTH field sets show -- neither hides the other.
target_cls, port_cls, keyword_cls, interval_cls, retries_cls, timeout_cls, method_cls, body_cls = \
    toggle_type_fields(["website", "push"])
assert target_cls == "form-field", "target must still show -- website in the mix needs it"
assert keyword_cls == "form-field"
assert retries_cls == "form-field"
assert timeout_cls == "form-field"
assert interval_cls == "form-field", "interval must still show -- push in the mix needs it"

print("All toggle_type_fields combined-selection checks passed.")


# ---------- submit: Website + Push no longer rejected, creates a shared group ----------

_fire("add-monitor-submit")
out = _call_add_edit(submit=1, name="Vrittispace", url="https://vrittispace.com/",
                      types=["website", "push"], interval=120, notify=["notify"])
assert out["error"] is no_update, "no error branch should have fired -- this must succeed now"
assert out["wrapper_class"] == "modal-wrapper addmonitor-hidden"

created = {m["name"]: m for m in db.list_monitors() if m["name"].startswith("Vrittispace")}
assert set(created.keys()) == {"Vrittispace (Website)", "Vrittispace (Push)"}, created.keys()

web_row = created["Vrittispace (Website)"]
push_row = created["Vrittispace (Push)"]
assert web_row["group_key"] == push_row["group_key"] is not None, "must share one group_key"
assert web_row["url"] == "https://vrittispace.com/"
assert push_row["push_token"], "push half of the combo must still get a real server-generated token"
assert push_row["interval_sec"] == 120

print("All Website+Push combined-submit checks passed.")


# ---------- grouped card renders the push row alongside the active-type row ----------

targets = {t["id"]: t for t in data.get_targets()}
group = [targets[web_row["id"]], targets[push_row["id"]]]
card = build_target_card(group)
subrows = [c for c in card.children if getattr(c, "id", None) and c.id.get("type") == "tcard"]
assert len(subrows) == 2
labels = {c.children[1].children for c in subrows}  # TYPE_LABELS text on each subrow
assert labels == {"Website", "Push"}, labels

print("All push-in-grouped-card render checks passed.")

os.remove(db.DB_PATH)
print("All push-grouping checks passed.")
