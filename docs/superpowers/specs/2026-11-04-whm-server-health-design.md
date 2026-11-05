# Design: WHM/cPanel Remote Server Resource Health Monitoring

**Date:** 2026-11-04
**Source PRD:** "Server Resource Health Monitoring & Alerting" (disk/CPU/memory/mail-queue on the cPanel servers).
**Status:** Approved for implementation (build against mocked WHM responses; real scoped token + live-verify is the §7 admin handoff).

## 1. Key finding (why this is small)

`server_health.py` already monitors **CPU/memory/disk/inodes of the local box Rovix runs on** (`/proc`, `statvfs`, `systemctl`) with warning/critical thresholds, 2-read hysteresis, no-repeat alerting via incidents, and the shared email channel + scheduler. The PRD's incident was a **remote cPanel server** filling up — which that local reader never sees.

So this is **not** new monitoring machinery. It is a new **data source** (the WHM JSON API over HTTPS) feeding the **same** zone state machine, incidents, emails, and scheduler. Precedent: `ssl_check.py` is already a separate module that reuses `server_health.evaluate_metric_zone` and the incident/email code — this mirrors it.

## 2. Resolved open items (read from the code, not guessed)

- **§8.3 alert destination** → `email_alerts.send()` already targets the dashboard Settings emails (`email_1/2/3`) with `ALERT_TO` fallback. New alerts call the same `send()`/`send_and_record()` — same list, nothing re-entered.
- **§8.4 schedule** → the 5s scheduler loop (`monitor_engine.start_background_scheduler`) already calls `server_health.check_server_metrics()` and `ssl_check.check_all_ssl()`. One more additive call `whm_health.check_all()`, each poll self-gated to `WHM_CHECK_INTERVAL_SEC` (default 60s) so we don't hit WHM every tick. Wrapped in its own `try/except` → cannot affect uptime checks (§FR8).

## 3. Architecture — new module `whm_health.py`

- **WHM client** `_api(host, token, function, params)`: `GET https://<host>:<WHM_PORT>/json-api/<function>?api.version=1` with header `Authorization: whm <user>:<token>`, via `urllib` (no new dependency). TLS verification **on by default**; `WHM_INSECURE_TLS=1` opt-out for the self-signed cert cPanel control ports (:2087) commonly use. Any transport/HTTP/parse error → the metric is treated as unavailable (skipped + logged), never a crash.
- **Per-metric fetchers** → percentage (or raw count for mail queue). Each is independent and degrades to `None` on failure.
- **`_evaluate(host, key, value, warn, crit, now)`**: mirrors `check_server_metrics`'s per-metric block exactly — `zone_for` → `evaluate_metric_zone` (reused, 2-read hysteresis) → `open_server_incident`/`update_incident_severity`/`resolve_server_incident` + `email_alerts.format_alert(host, detail, severity)` / `format_recovered` via `send_and_record`. State keyed `whm:<host>:<metric>` in the existing `server_metric_state` table (like `ssl:<id>`). `value is None` → return, never alert on missing data.
- **`check_all()`**: for each configured server, poll-gate then evaluate every metric.

### Alert content (§FR6)
`open_server_incident(host, detail, severity)` + subject `[WARNING] <host> — Disk 85% full`. Server, metric, value, severity all present.

## 4. Config (§6 — configurable, not inline)

Env-driven; token **never committed** (§7):
- `WHM_SERVERS="host1:token1,host2:token2"` — the owned servers to watch (chosen over site→server inference, which Rovix has no mapping for).
- `WHM_USER` (default `root`), `WHM_PORT` (2087), `WHM_CHECK_INTERVAL_SEC` (60), `WHM_INSECURE_TLS`.
- Thresholds: `WHM_DISK_WARN/CRIT` (80/90), `WHM_CPU_WARN/CRIT` (80/90), `WHM_MEM_WARN/CRIT` (80/90), `WHM_MAILQ_WARN/CRIT` (placeholder 500/1000 — §8 #2).
- `WHM_CPU_CORES` (to turn load average into a %), `WHM_MAILQ_FUNCTION`, `WHM_MEM_FUNCTION` (the uncertain WHM function names).

## 5. Confirmed vs. open (§8 — not guessed)

| Metric | Status |
|---|---|
| Disk % (`getdiskusage`, max partition) | **Confirmed** — the incident driver; alerts out of the box |
| CPU (`systemloadavg` load1 ÷ `WHM_CPU_CORES` × 100) | Needs core count; **skipped until `WHM_CPU_CORES` set** |
| Memory % | **No clean standard WHM fn** — slot wired, skipped until `WHM_MEM_FUNCTION` confirmed (flag to PM) |
| Mail queue count | §8 #1 fn + #2 thresholds unconfirmed — slot wired, skipped until `WHM_MAILQ_FUNCTION` set |

Degradation means: with only disk confirmed, only disk alerts; the rest activate once their config/function is confirmed. No workarounds substituted for the open items (§8).

## 6. Testing & verification

- Tests mock WHM responses / `fetch_metrics` (no real network, like `test_ssl_check.py`): zone transitions (warning→critical→resolve) open/escalate/resolve one incident against the right host, no-repeat alerting, alert subject carries host+metric+value+severity, missing-metric → no incident, a failing server is skipped without affecting others, `WHM_SERVERS` parsing.
- **Cannot live-verify** without the scoped token + host; that + confirming the CPU-cores/memory/mail-queue functions is the §7 admin handoff.

## 7. Out of scope (PRD §4.2/§10)
Inode monitoring, trend charts, auto-remediation, WHM UI/config changes, "all-clear" notifications.
