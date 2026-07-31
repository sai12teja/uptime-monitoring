"""TDD self-check for db.py's server-metric incident storage (PRD §7.2/§9).

Isolated throwaway DB, same pattern as test_dashboard.py — never touches
the live rovix.db.

Run: ./venv/Scripts/python.exe test_db_server_incidents.py
"""
import os

import db

db.DB_PATH = "test_db_server_incidents.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

# No state yet for a metric -> None, not a crash.
assert db.get_metric_state("disk_pct") is None

# Round-trip: set then get.
db.set_metric_state("disk_pct", status="warning", pending_zone=None, pending_count=0,
                     incident_id=None, last_read_ts=123.0)
row = db.get_metric_state("disk_pct")
assert row["status"] == "warning"
assert row["pending_zone"] is None
assert row["pending_count"] == 0
assert row["incident_id"] is None
assert row["last_read_ts"] == 123.0

# Second set on the same metric updates in place (no duplicate rows).
db.set_metric_state("disk_pct", status="critical", pending_zone="warning", pending_count=1,
                     incident_id=None, last_read_ts=124.0)
row = db.get_metric_state("disk_pct")
assert row["status"] == "critical" and row["pending_count"] == 1

# A different metric is independent state.
assert db.get_metric_state("cpu_pct") is None

# open_server_incident: no monitor row exists for "disk" (it's not a
# website/CRM target) -> incidents.monitor_id must accept NULL.
incident_id = db.open_server_incident("Server", "Disk 96% full", "critical")
assert incident_id is not None
incidents = db.list_incidents()
assert len(incidents) == 1
assert incidents[0]["monitor_id"] is None
assert incidents[0]["target_name"] == "Server"
assert incidents[0]["problem"] == "Disk 96% full"
assert incidents[0]["severity"] == "critical"
assert incidents[0]["resolved"] is None

# update_incident_severity: escalate/deescalate without resolving.
db.update_incident_severity(incident_id, "warning")
assert db.list_incidents()[0]["severity"] == "warning"

# resolve_server_incident: returns downtime, marks resolved.
downtime_sec = db.resolve_server_incident(incident_id)
assert downtime_sec is not None and downtime_sec >= 0
assert db.list_incidents()[0]["resolved"] is not None

os.remove(db.DB_PATH)
print("All db.py server-incident storage checks passed.")
