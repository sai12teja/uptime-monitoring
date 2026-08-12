"""TDD self-check for the notification settings feature: a per-monitor
"notify" toggle, plus a Settings modal (gear icon) storing up to 3 alert
emails and a global mute in db.settings.

Run: ./venv/Scripts/python.exe test_notify_settings.py
"""
import os
from unittest.mock import MagicMock, patch

import db

db.DB_PATH = "test_notify_settings.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data
import email_alerts
from dash import no_update
from dash._callback_context import context_value
from dash._utils import AttributeDict

import app
import monitor_engine
from monitor_engine import handle_tick_events

monitor_engine.ALERT_BATCH_WINDOW_SEC = 0  # flush every batch immediately, matching this file's per-event assertions


def _fire(component_id):
    context_value.set(AttributeDict({"triggered_inputs": [{"prop_id": f"{component_id}.n_clicks", "value": 1}]}))


# ---------- db.get_settings()/update_settings() ----------

# No row saved yet -> None, and reading it must not write a default row
# (a plain SELECT mutating rovix.db on every unrelated test run would be a
# nasty surprise).
assert db.get_settings() is None

db.update_settings("a@example.com", "b@example.com", None, True)
row = db.get_settings()
assert row["email_1"] == "a@example.com"
assert row["email_2"] == "b@example.com"
assert row["email_3"] is None
assert row["notify_enabled"] == 1

db.update_settings("a@example.com", None, None, False)
row = db.get_settings()
assert row["notify_enabled"] == 0
assert row["email_2"] is None, "update_settings must overwrite, not merge"

print("All db settings checks passed.")


# ---------- email_alerts.send() honors settings emails + global mute ----------

env = {"SMTP_HOST": "smtp.example.com", "ALERT_TO": "fallback@example.com"}

# Settings row unset in a fresh db -> falls back to ALERT_TO, same as before
# this feature existed.
db.update_settings(None, None, None, True)
db.get_settings()  # sanity: row now exists but all emails NULL
with patch.dict("os.environ", env, clear=True), patch("smtplib.SMTP") as mock_smtp:
    server = MagicMock()
    mock_smtp.return_value.__enter__.return_value = server
    assert email_alerts.send("subj", "body") is True
    assert server.send_message.call_args[0][0]["To"] == "fallback@example.com"

# Settings emails configured -> override ALERT_TO, joined with ", ".
db.update_settings("ops1@example.com", "ops2@example.com", None, True)
with patch.dict("os.environ", env, clear=True), patch("smtplib.SMTP") as mock_smtp:
    server = MagicMock()
    mock_smtp.return_value.__enter__.return_value = server
    assert email_alerts.send("subj", "body") is True
    assert server.send_message.call_args[0][0]["To"] == "ops1@example.com, ops2@example.com"

# Global mute -> no SMTP connection attempted at all.
db.update_settings("ops1@example.com", None, None, False)
with patch.dict("os.environ", env, clear=True), patch("smtplib.SMTP") as mock_smtp:
    assert email_alerts.send("subj", "body") is False
    mock_smtp.assert_not_called()

db.update_settings(None, None, None, True)  # reset for the rest of this file
print("All email_alerts settings-integration checks passed.")


# ---------- per-monitor notify toggle suppresses handle_tick_events' email ----------

muted_id = data.add_target("muted site", "https://muted.example", "website", notify=False)
loud_id = data.add_target("loud site", "https://loud.example", "website", notify=True)

muted_incident = db.open_incident(muted_id, "muted site", "HTTP 500")
loud_incident = db.open_incident(loud_id, "loud site", "HTTP 500")

sent = []
with patch.object(email_alerts, "send", side_effect=lambda s, b, h=None: sent.append(s)):
    handle_tick_events([
        ("opened", "muted site", "HTTP 500", muted_incident),
        ("opened", "loud site", "HTTP 500", loud_incident),
    ])

assert len(sent) == 1 and "loud site" in sent[0], sent
print("All per-monitor notify checks passed.")


# ---------- Add/Edit modal: notify checkbox wiring ----------

def _call_add_edit(edit_open=None, submit=None, **state):
    defaults = dict(name=None, url=None, types=None, keyword=None, port=None, interval=None,
                     retries=None, timeout=None, method=None, body=None, encoding=None, notify=None,
                     paths_text=None, discovered_checked=None, filter_text=None, selected_id=None,
                     editing_id=None)
    defaults.update(state)
    result = app.add_edit_monitor(
        None, None, None, None, submit, edit_open,
        defaults["name"], defaults["url"], defaults["types"], defaults["keyword"],
        defaults["port"], defaults["interval"], defaults["retries"], defaults["timeout"],
        defaults["method"], defaults["body"], defaults["encoding"], defaults["notify"],
        defaults["paths_text"], defaults["discovered_checked"],
        defaults["filter_text"], defaults["selected_id"], defaults["editing_id"],
    )
    return dict(zip(app.ADD_EDIT_OUTPUT_IDS, result))


# Opening "Add Monitor" defaults notify to checked.
_fire("add-monitor-open")
out = app.add_edit_monitor(1, None, None, None, None, None,
                            None, None, None, None, None, None, None, None, None, None, None, None,
                            None, None, None, None, None)
out = dict(zip(app.ADD_EDIT_OUTPUT_IDS, out))
assert out["notify"] == ["notify"]

# Editing the muted monitor pre-fills notify unchecked.
_fire("detail-edit-btn")
out = _call_add_edit(edit_open=1, selected_id=muted_id)
assert out["notify"] == [], out["notify"]

# Submitting edit with notify unchecked persists notify=0.
_fire("add-monitor-submit")
out2 = _call_add_edit(submit=1, name="loud site renamed", url="https://loud.example",
                       types=["website"], notify=[], selected_id=loud_id, editing_id=loud_id)
assert out2["wrapper_class"] == "modal-wrapper addmonitor-hidden"
assert db.get_monitor(loud_id)["notify"] == 0

# A brand-new monitor with the checkbox unchecked is created muted.
_fire("add-monitor-submit")
out3 = _call_add_edit(submit=1, name="new muted", url="https://new-muted.example",
                       types=["website"], notify=[])
new_id = [m["id"] for m in db.list_monitors() if m["name"] == "new muted"][0]
assert db.get_monitor(new_id)["notify"] == 0

print("All Add/Edit notify-checkbox checks passed.")


# ---------- Settings modal callback ----------

def _call_settings(open_=None, close=None, cancel=None, backdrop=None, submit=None, **state):
    defaults = dict(email_1=None, email_2=None, email_3=None, enabled=None)
    defaults.update(state)
    result = app.open_edit_settings(open_, close, cancel, backdrop, submit,
                                     defaults["email_1"], defaults["email_2"],
                                     defaults["email_3"], defaults["enabled"])
    return result


# Opening loads the saved settings into the form.
db.update_settings("saved1@example.com", "saved2@example.com", None, True)
_fire("settings-open")
out = _call_settings(open_=1)
assert out[0] == "modal-wrapper addmonitor-open"
assert out[2] == "saved1@example.com"
assert out[3] == "saved2@example.com"
assert out[5] == ["enabled"]
assert out[6] == "modal-wrapper addmonitor-hidden"  # forces Add/Edit modal closed
assert out[7] == "modal-wrapper addmonitor-hidden"  # forces Delete-confirm closed

# Saving persists the form values.
_fire("settings-submit")
out = _call_settings(submit=1, email_1="new@example.com", email_2="", email_3="",
                      enabled=[])
assert out[0] == "modal-wrapper addmonitor-hidden"
row = db.get_settings()
assert row["email_1"] == "new@example.com"
assert row["email_2"] is None
assert row["notify_enabled"] == 0

# Invalid email is rejected, nothing persisted.
_fire("settings-submit")
out = _call_settings(submit=1, email_1="not-an-email", enabled=["enabled"])
assert out[1] == "Enter a valid email address."
assert db.get_settings()["email_1"] == "new@example.com", "rejected save must not persist"

# A CRLF-smuggled address must also be rejected, not just "no @" -- the
# email module raises ValueError on a header value containing a newline,
# and if that reached db.update_settings it would brick every future send().
_fire("settings-submit")
out = _call_settings(submit=1, email_1="a@example.com\r\nBcc: evil@evil.com", enabled=["enabled"])
assert out[1] == "Enter a valid email address."
assert db.get_settings()["email_1"] == "new@example.com", "CRLF-smuggled address must not persist"

# The gear button lives in summary-slot, rebuilt every refresh tick -- a
# remount with n_clicks=0 must not spuriously reopen the modal (same
# phantom-trigger shape already guarded for detail-edit-btn/detail-delete-btn).
_fire("settings-open")
out = _call_settings(open_=0)
assert out[0] is no_update, "mount noise must not open the Settings modal"

print("All Settings modal checks passed.")


# ---------- Modal-stacking guard is bidirectional (Settings <-> Add/Edit/Delete) ----------

mid3 = data.add_target("stack test", "https://stack.example", "website")

# Opening Add Monitor forces Settings closed (already covered above via
# settings-open forcing Add/Edit closed; this is the reverse direction).
_fire("add-monitor-open")
out = app.add_edit_monitor(1, None, None, None, None, None,
                            None, None, None, None, None, None, None, None, None, None, None, None,
                            None, None, None, None, None)
out = dict(zip(app.ADD_EDIT_OUTPUT_IDS, out))
assert out["settings_class"] == "modal-wrapper addmonitor-hidden"

# Opening Edit forces Settings closed too.
_fire("detail-edit-btn")
out = _call_add_edit(edit_open=1, selected_id=mid3)
assert out["settings_class"] == "modal-wrapper addmonitor-hidden"

# Opening Delete-confirm forces Settings closed.
_fire("detail-delete-btn")
d = app.delete_monitor(1, None, None, None, mid3, None)
assert d[5] == "modal-wrapper addmonitor-hidden"

print("All bidirectional modal-stacking checks passed.")


# ---------- email_alerts.send() survives a malformed To: header defensively ----------

# Belt-and-suspenders on top of the UI-level validation above: even if a
# bad address somehow ends up in the settings table, send() must not raise
# out of the scheduler loop -- it should log-and-skip like any other send failure.
db.update_settings("a@example.com\r\nBcc: evil@evil.com", None, None, True)
with patch.dict("os.environ", {"SMTP_HOST": "smtp.example.com"}, clear=True), patch("smtplib.SMTP"):
    assert email_alerts.send("subj", "body") is False

db.update_settings(None, None, None, True)  # reset
print("All email_alerts defensive-send checks passed.")

os.remove(db.DB_PATH)
print("All notify/settings checks passed.")
