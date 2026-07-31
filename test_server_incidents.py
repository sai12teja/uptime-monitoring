"""TDD self-check for server-metric threshold -> incident logic (PRD §7.2/§9).

Run: ./venv/Scripts/python.exe test_server_incidents.py
"""
from server_health import zone_for, evaluate_metric_zone

WARN, CRIT = 80, 95

# zone_for: classifies a reading against the two-level thresholds (§7.2).
assert zone_for(50, WARN, CRIT) == "ok"
assert zone_for(80, WARN, CRIT) == "warning"
assert zone_for(95, WARN, CRIT) == "critical"
assert zone_for(None, WARN, CRIT) is None  # unreadable metric — never alert on missing data

# evaluate_metric_zone: pure state machine, mirrors monitor_engine's
# evaluate_status but 3-level (ok/warning/critical) instead of 2 (up/down),
# because §14 alerts on warning AND critical separately.

# Stable ok, stays ok, no transition.
assert evaluate_metric_zone("ok", None, 0, "ok") == ("ok", None, 0, None)

# First over-warning read: not yet 2 consecutive, no transition.
assert evaluate_metric_zone("ok", None, 0, "warning") == ("ok", "warning", 1, None)

# Second consecutive warning read -> commits: opens at warning.
assert evaluate_metric_zone("ok", "warning", 1, "warning") == ("warning", None, 0, "open")

# A single critical read after one warning read resets the counter (must
# AGREE on the same target zone for 2 reads, not just "any breach twice").
assert evaluate_metric_zone("ok", "warning", 1, "critical") == ("ok", "critical", 1, None)

# Already warning, 2 consecutive critical reads -> escalate (not "open").
assert evaluate_metric_zone("warning", None, 0, "critical") == ("warning", "critical", 1, None)
assert evaluate_metric_zone("warning", "critical", 1, "critical") == ("critical", None, 0, "escalate")

# Already critical, 2 consecutive reads drop to warning -> deescalate (still open).
assert evaluate_metric_zone("critical", None, 0, "warning") == ("critical", "warning", 1, None)
assert evaluate_metric_zone("critical", "warning", 1, "warning") == ("warning", None, 0, "deescalate")

# Straight critical -> ok in 2 reads (e.g. disk cleanup frees space fast) ->
# resolve directly, no need to pass back through warning first.
assert evaluate_metric_zone("critical", None, 0, "ok") == ("critical", "ok", 1, None)
assert evaluate_metric_zone("critical", "ok", 1, "ok") == ("ok", None, 0, "resolve")

# Straight ok -> critical in 2 reads (metric spikes hard) -> "open" at
# critical directly, never having been in "warning".
assert evaluate_metric_zone("ok", None, 0, "critical") == ("ok", "critical", 1, None)
assert evaluate_metric_zone("ok", "critical", 1, "critical") == ("critical", None, 0, "open")

# Unreadable metric mid-accumulation: no-op, doesn't reset or crash.
assert evaluate_metric_zone("ok", "warning", 1, None) == ("ok", "warning", 1, None)

print("All server-incident zone/transition checks passed.")
