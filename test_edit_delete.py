"""TDD self-check for the Edit/Delete monitor UI: the detail panel's Edit
button reuses the Add Monitor modal (pre-filled, type locked) and calls
data.update_target; Delete asks for confirmation, then soft-deletes.

Run: ./venv/Scripts/python.exe test_edit_delete.py
"""
import os

import db

db.DB_PATH = "test_edit_delete.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data
from dash import no_update
from dash._callback_context import context_value
from dash._utils import AttributeDict

import app


def _fire(component_id):
    context_value.set(AttributeDict({"triggered_inputs": [{"prop_id": f"{component_id}.n_clicks", "value": 1}]}))


def _all_ids(component):
    # Dash components: .id (may be absent) + .children (component, list of
    # components, or a plain string/None) -- walk both.
    found = set()
    comp_id = getattr(component, "id", None)
    if isinstance(comp_id, str):
        found.add(comp_id)
    children = getattr(component, "children", None)
    if isinstance(children, (list, tuple)):
        for c in children:
            if hasattr(c, "children") or hasattr(c, "id"):
                found |= _all_ids(c)
    elif hasattr(children, "children") or hasattr(children, "id"):
        found |= _all_ids(children)
    return found


# --- detail-edit-btn/detail-delete-btn must exist in the INITIAL layout,
# not just after build_detail_content() runs from a card click. They're
# registered as callback Inputs; if a user's very first interaction is
# "+ Add Monitor" before ever opening a detail panel, a browser that has
# never seen these ids throws "nonexistent object used in an Input" and
# that first click silently does nothing -- a real bug server-side
# Python tests can't catch, since they invoke the callback function
# directly rather than checking what the browser actually receives.
initial_ids = _all_ids(app.serve_layout())
assert "detail-edit-btn" in initial_ids, "detail-edit-btn missing from initial layout"
assert "detail-delete-btn" in initial_ids, "detail-delete-btn missing from initial layout"
print("All initial-layout Edit/Delete button presence checks passed.")


def _call_add_edit(edit_open=None, submit=None, **state):
    defaults = dict(name=None, url=None, types=None, keyword=None, port=None, interval=None,
                     retries=None, timeout=None, method=None, body=None, encoding=None, notify=None,
                     paths_text=None, filter_text=None, selected_id=None, editing_id=None)
    defaults.update(state)
    result = app.add_edit_monitor(
        None, None, None, None, submit, edit_open,
        defaults["name"], defaults["url"], defaults["types"], defaults["keyword"],
        defaults["port"], defaults["interval"], defaults["retries"], defaults["timeout"],
        defaults["method"], defaults["body"], defaults["encoding"], defaults["notify"],
        defaults["paths_text"], defaults["filter_text"], defaults["selected_id"], defaults["editing_id"],
    )
    return dict(zip(app.ADD_EDIT_OUTPUT_IDS, result))


# custom interval_sec (45, not the 60 default) so the edit path's fallback
# logic is actually exercised rather than accidentally matching by luck
mid = data.add_target("acme site", "https://acme.example", "website",
                       interval_sec=45, retries=1, timeout_sec=5)

# --- opening Edit pre-fills every field from the real row, locks the type ---
_fire("detail-edit-btn")
out = _call_add_edit(edit_open=1, selected_id=mid)
assert out["wrapper_class"] == "modal-wrapper addmonitor-open"
assert out["name"] == "acme site"
assert out["url"] == "https://acme.example"
assert out["title"] == "Edit Monitor"
assert out["submit_label"] == "Save Changes"
assert out["type_value"] == ["website"]
assert out["type_wrapper_class"] == "form-field hidden"  # can't change type after creation
assert out["interval"] == 45, out["interval"]  # real value, not the 60 default
assert out["retries"] == 1
assert out["timeout"] == 5
assert out["editing_id"] == mid

# --- submitting in edit mode updates the existing row, creates nothing new ---
before_count = len(db.list_monitors())
_fire("add-monitor-submit")
out2 = _call_add_edit(
    submit=1, name="acme site renamed", url="https://acme.example/health",
    types=["website"], keyword="Welcome", port=out["port"], interval=out["interval"],
    retries="3", timeout="8", method="GET", body="", encoding="json",
    selected_id=mid, editing_id=mid,
)
assert out2["wrapper_class"] == "modal-wrapper addmonitor-hidden"
assert out2["editing_id"] is None
assert len(db.list_monitors()) == before_count, "edit must not create a new monitor row"

row = db.get_monitor(mid)
assert row["name"] == "acme site renamed"
assert row["url"] == "https://acme.example/health"
assert row["keyword"] == "Welcome"
assert row["retries"] == 3
assert row["timeout_sec"] == 8
assert row["interval_sec"] == 45, "interval_sec must be preserved, not reset to 60"

# --- validation still runs in edit mode (bad URL rejected, nothing changed) ---
_fire("add-monitor-submit")
out3 = _call_add_edit(submit=1, name="acme site renamed", url="not-a-url",
                       types=["website"], selected_id=mid, editing_id=mid)
assert out3["error"] == "URL must start with http:// or https://"
assert db.get_monitor(mid)["url"] == "https://acme.example/health", "rejected edit must not persist"

# --- delete flow: open confirm, cancel leaves it active, confirm soft-deletes ---
_fire("detail-delete-btn")
d = app.delete_monitor(1, None, None, None, mid, None)
assert d[0] == "modal-wrapper addmonitor-open"
assert d[4] == "modal-wrapper addmonitor-hidden"  # forces Edit modal closed too
assert db.get_monitor(mid)["active"] == 1

_fire("delete-confirm-cancel")
d = app.delete_monitor(None, 1, None, None, mid, None)
assert d[0] == "modal-wrapper addmonitor-hidden"
assert db.get_monitor(mid)["active"] == 1

_fire("delete-confirm-submit")
d = app.delete_monitor(None, None, None, 1, mid, None)
assert d[0] == "modal-wrapper addmonitor-hidden"
assert d[1] is None  # selected-target cleared -> closes the detail panel
assert db.get_monitor(mid)["active"] == 0  # soft-deleted, history preserved not destroyed

# --- the race that caused both modals to render stacked: Edit-open must
# force Delete-confirm closed, and Delete-open must force Edit closed ---
mid2 = data.add_target("race test", "https://race.example", "website")
_fire("detail-edit-btn")
out_edit = _call_add_edit(edit_open=1, selected_id=mid2)
assert out_edit["wrapper_class"] == "modal-wrapper addmonitor-open"
assert out_edit["delete_confirm_class"] == "modal-wrapper addmonitor-hidden"

_fire("detail-delete-btn")
d = app.delete_monitor(1, None, None, None, mid2, None)
assert d[0] == "modal-wrapper addmonitor-open"
assert d[4] == "modal-wrapper addmonitor-hidden"

# --- opening the detail panel (any card click) recreates the Edit/Delete
# buttons from scratch, which Dash treats as a trigger even though nobody
# clicked them (n_clicks resets to 0/None on (re)mount) -- must be a no-op ---
_fire("detail-edit-btn")
out_phantom = _call_add_edit(edit_open=0, selected_id=mid2)
assert out_phantom["wrapper_class"] is no_update, "mount noise must not open the Edit modal"

_fire("detail-delete-btn")
d = app.delete_monitor(0, None, None, None, mid2, None)
assert d[0] is no_update, "mount noise must not open the Delete confirm"
assert d[4] is no_update

os.remove(db.DB_PATH)
print("All Edit/Delete UI checks passed.")
