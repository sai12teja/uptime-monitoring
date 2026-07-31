# Product Requirements Document
## Website, CRM & Server Health Monitoring Dashboard with Email Alerts

| Field | Value |
|---|---|
| **Document version** | 1.1 |
| **Status** | Draft for build — design review complete (2026-07-28) |
| **Hosting** | Single dedicated server (GoDaddy), root access confirmed |
| **Dashboard tech** | Plotly (Dash) |
| **Alerting** | Email only (recipients to be configured by dev team) |
| **Audience** | Product Managers, Developers, UI/UX Designers, QA Engineers, Stakeholders |

> **v1.1 change note:** Section 13 (Dashboard UI/UX Behavior) was expanded from a high-level element list into concrete, buildable design decisions following a design review — layout hierarchy, full interaction-state coverage, a minimal visual token set, calm-vs-incident theming, responsive/accessibility behavior, target detail panel contents, scale behavior, and an incident acknowledge action. Section 14 (Email Alert Behavior) gained a structured subject-line format. Sections 9, 11, and 12 were updated to support the acknowledge action.

> **How to read this document:** Assumes no prior knowledge of the project. Sections 1–4 explain what and why in plain language. Section 5 onward covers features, flows, data, and screen behavior in implementation detail.

---

## Table of Contents
1. Plain-English Overview
2. The Problem
3. Goals & Success Metrics
4. Key Terms (Glossary)
5. What Gets Monitored
6. System Architecture
7. Feature Specifications
8. End-to-End User Flows
9. Business Logic & Rules
10. Check Intervals & Thresholds
11. API Requirements
12. Database Design
13. Dashboard UI/UX Behavior
14. Email Alert Behavior
15. Edge Cases & Failure Handling
16. Non-Functional Requirements
17. MVP Scope & Future Roadmap
18. QA & Acceptance Criteria
19. Open Questions
20. Assumptions & Constraints

---

## 1. Plain-English Overview

We host multiple client websites and a CRM (customer relationship management system) on one **dedicated server** at GoDaddy. Right now, if a website or the CRM goes down, we find out only when someone happens to check it or a client complains — there's no automated, real-time way to know.

This project builds a simple, self-hosted **monitoring system** that:
1. Continuously checks every website, the CRM, and the server's own health (CPU, RAM, disk).
2. Shows everything on a **single live dashboard** (built with Plotly) — one page, real-time status of every domain and the server.
3. Sends an **email alert** the moment something goes down, and another when it recovers.

There is **no AI, no automated fixing, no WhatsApp** in this project — that was explored in a separate, unrelated effort. This is intentionally simple: **detect → show on dashboard → email**. A human always does the actual fixing.

---

## 2. The Problem

- We don't currently have any paid or free tool giving real-time notice when a client website or the CRM goes down.
- Outages aren't frequent, but when they happen, we learn about them late — sometimes from the client, not from our own systems.
- There's no single place to see "is everything up right now" across all hosted domains and the CRM.
- The CRM is login-gated, so it's not obvious (without deciding on an approach) how to check its health automatically — this document answers that.

---

## 3. Goals & Success Metrics

| Goal | Metric | Target |
|---|---|---|
| Detect outages automatically | Mean time to detect (MTTD) | ≤ 1 check interval (≤ 60s for websites/CRM login) |
| Alert quickly | Email delivered after confirmed failure | ≤ 2 minutes |
| Avoid false alarms | False-positive alert rate | < 5% |
| Single source of truth | One dashboard shows all domains + CRM + server health | 100% of hosted targets visible |
| Reliable monitoring | Monitoring system's own uptime | ≥ 99.9% |

---

## 4. Key Terms (Glossary)

| Term | Meaning |
|---|---|
| **Dedicated server** | A physical server fully owned by us — no sharing with other tenants, full root access. This is where everything is hosted, including this monitoring system. |
| **Monitor** | One configured check (e.g. "check crm.ourcompany.com every 60 seconds"). |
| **Check** | A single execution of a monitor at a point in time. |
| **Incident** | A recorded problem — opens when checks fail, closes when they recover. |
| **Tier 1 CRM check** | Checking that the CRM's login page loads correctly (reachability only). |
| **Tier 2 CRM check** | A synthetic test that actually logs into the CRM and clicks through specific workflows (future scope). |
| **Synthetic monitoring** | Industry term for scripted, robot-driven checks that simulate real user actions (e.g. via Playwright), as opposed to simple ping/HTTP checks. |
| **Dash (Plotly Dash)** | A Python framework for building interactive web dashboards using Plotly charts — this is what the team will use to build the dashboard UI. |
| **Heartbeat / watchdog** | A secondary, independent check confirming the monitoring system itself is still alive (see §16). |

---

## 5. What Gets Monitored

### 5.1 Websites
Every client website hosted on the server. Standard outside-in check: does the site load, does it return a healthy HTTP status, is the SSL certificate valid.

### 5.2 CRM — Tier 1 (in scope now)
The CRM is login-gated, so a full "click around and use it" check isn't simple — but checking that it's **reachable** is exactly as easy as a website check:
- Hit the CRM's login page URL.
- Confirm it returns a healthy HTTP status (e.g. 200) within a normal response time.
- Confirm the page contains expected content (e.g. the login form itself) — this catches cases where the server responds but shows an error page instead of the login screen.

This does **not** confirm login actually works or that any feature inside the CRM works — it confirms the CRM application is up and serving its login page. That is exactly the same signal being tracked for websites, applied to the CRM's front door.

### 5.3 CRM — Tier 2 (future scope, not built now)
A **synthetic transaction** check: a scheduled headless-browser script (e.g. Playwright) that:
1. Opens the CRM login page.
2. Logs in using a **dedicated, monitoring-only CRM account** (never a real user's credentials).
3. Confirms login succeeded (e.g. the main dashboard element appears).
4. Optionally navigates into specific modules/tabs to confirm they load without errors.
5. Logs out and reports success/failure and timing for each step.

This is a well-established pattern (used by tools like Checkly, Datadog Synthetics, New Relic) for testing login-gated applications. Since the CRM has **no 2FA**, a dedicated monitoring account can log in without any extra manual-approval step — this removes the one common blocker for this kind of check. Flagged as future scope per your instruction; not part of this build.

### 5.4 Server Health
Because we have **root access** on a **dedicated** server, full server-level monitoring is possible with no restrictions:
- CPU load / load average
- Memory (RAM) and swap usage
- Disk usage per partition (disk-full is one of the most common causes of "everything is down" outages)
- Inode usage (running out of file slots even with free disk space)
- Status of core services (web server, database, mail server, DNS resolver — whichever your stack runs)

---

## 6. System Architecture

```
┌───────────────────────── DEDICATED SERVER (GoDaddy, root access) ─────────────────────────┐
│                                                                                              │
│   Hosted content:                          Monitoring stack (this project):                │
│   • Client websites                        • Monitor Engine (scheduled checks)               │
│   • CRM application                        • Backend (check logic, incident state)           │
│                                             • Database (stores checks/incidents/history)      │
│                                             • Dashboard (Plotly Dash) — single live page      │
│                                             • Email alert sender                              │
│                                                                                                │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                        Recipients receive email alerts
                        (list managed by dev team — see §14)
```

**Key architecture decision:** since the server is dedicated with root access, the entire monitoring stack **can** run on the same box being monitored — no separate VPS is strictly required. One trade-off worth knowing about (see §16) is that if the whole server crashes hard, the monitor crashes with it, and nothing is left to send the alert. The common, low-cost industry fix is a small independent **heartbeat check** running elsewhere (even a $5/month box) that simply confirms "is the monitor still alive" and alerts if it goes silent. This is optional and can be added later — flagged here so the decision is made on purpose, not by accident.

**Components**
| Component | Purpose |
|---|---|
| Monitor Engine | Runs scheduled checks: website HTTP checks, CRM login-page checks, server resource reads |
| Backend | Evaluates check results, applies thresholds, creates/updates/resolves incidents |
| Database | Stores monitor configs, check history, incidents |
| Dashboard (Plotly Dash) | Single-page live view of every domain + CRM + server health |
| Email Sender | Sends alert on DOWN, alert on recovery (UP) |

---

## 7. Feature Specifications

### 7.1 Website & CRM (Tier 1) Monitoring
- Configurable monitor per target: URL, interval, timeout, expected HTTP status, optional expected keyword.
- Detects: unreachable, wrong/error HTTP status, slow response (above a configurable threshold), SSL certificate invalid or expiring soon.
- **Flap protection:** requires **consecutive** failures before declaring DOWN, and consecutive successes before declaring UP (see §10) — prevents one network blip from triggering a false alarm.

### 7.2 Server Health Monitoring
- Reads CPU load, memory usage, disk usage (per partition), inode usage, and core service status directly from the server (root access allows this with no restrictions).
- Two-level thresholds per metric: **warning** and **critical** (e.g. disk > 90% = warning, > 95% = critical).

### 7.3 Incident Management
- On confirmed failure, create an incident: target, problem type, severity, time detected, status.
- **Deduplication:** one open incident per (target + problem type) — repeated failing checks update the existing incident, they don't create duplicates.
- **Auto-resolve:** when checks recover, close the incident, record total downtime.

### 7.4 Dashboard
- Single Plotly Dash page showing, for every monitored target: current status (up/down), last-checked time, response time, and (for the server) live resource gauges.
- Auto-refreshing — no manual reload needed.
- Incident history view (what went down, when, for how long).

### 7.5 Email Alerts
- Sent on: new incident (DOWN), and recovery (UP).
- Recipient list: to be configured directly by the dev team (per your instruction — not specified in this document).

---

## 8. End-to-End User Flows

### Flow A — A client website goes down
1. `clientsite.com` starts returning HTTP 500 (or times out).
2. Monitor's next check fails. Failure count = 1 → below threshold, no alert yet.
3. Two more consecutive checks fail → threshold reached → **confirmed DOWN**.
4. Backend creates an incident (deduped — only one open incident for this target/problem).
5. Dashboard updates the target's card to "down" immediately.
6. Email alert sent: *"DOWN — clientsite.com — [problem] — detected at [time]"*.
7. Team fixes the issue manually.
8. Next checks succeed; after the required consecutive successes → **confirmed UP**.
9. Backend auto-resolves the incident, records downtime duration.
10. Email alert sent: *"UP — clientsite.com recovered — total downtime: [X] minutes"*.
11. Dashboard updates the target's card back to "up."

### Flow B — CRM login page fails
Same as Flow A, but the check target is the CRM login page URL instead of a website homepage — reachability + expected content check, same threshold and alert logic.

### Flow C — Server disk fills up
1. Server-health check reads disk usage on `/` at 96%.
2. Threshold: >95% = critical → incident created (type: disk-full, target: the server itself, not a specific website).
3. Dashboard shows the server's disk gauge in critical/red state.
4. Email alert sent describing the server-level issue.
5. Team clears space manually; next reading below threshold → incident auto-resolves; recovery email sent.

---

## 9. Business Logic & Rules

- **Down confirmation:** DOWN only after N consecutive failed checks (recommended default: 3). UP only after M consecutive successes (recommended default: 2).
- **One open incident per problem:** dedup key = (target, problem type). No duplicate incidents while one is already open for the same issue.
- **Auto-resolve:** only the same check that opened the incident can close it — a website check recovering doesn't resolve a server-disk incident, and vice versa.
- **Severity:** website/CRM down → critical. Server resource warning-level → warning; critical-level → critical. SSL expiring soon → warning, escalating to critical on/after the expiry date.
- **Correlated outages:** if the entire server goes down, every website + the CRM will fail simultaneously — the dashboard should make it visually obvious this is one root cause (e.g. a prominent "server unreachable" banner) rather than just showing 20 unrelated red cards, so the team isn't confused about scope.
- **Incident acknowledgment:** any team member can acknowledge an open incident from the dashboard (see §13.8). Acknowledging is a **coordination signal only** — it records who/when and displays a "seen by [name]" marker, but it does **not** stop alert emails, does not pause checks, and does not auto-resolve the incident. Only the underlying check recovering can close it (unchanged from the auto-resolve rule above). This keeps §20's "no automated remediation" constraint intact — acknowledgment is a human telling other humans "I'm on it," not the system taking any action.

---

## 10. Check Intervals & Thresholds

Recommended defaults, based on common industry practice (used by tools like UptimeRobot, Pingdom, Uptime Kuma):

| Check type | Interval | Failure threshold | Notes |
|---|---|---|---|
| Website / CRM login page (Tier 1) | 60 seconds | 3 consecutive fails → DOWN; 2 consecutive successes → UP | Fast enough to catch outages quickly without being noisy |
| Server resources (CPU/RAM/disk) | 60–300 seconds | 2 consecutive over-threshold reads → alert | These change slowly; no benefit to checking every few seconds |
| SSL certificate expiry | Once every 12–24 hours | N/A (date-based, not consecutive-failure based) | Alert at configurable lead times, e.g. 14 / 7 / 1 days before expiry |
| CRM synthetic workflow (Tier 2, future) | 5–15 minutes | Same consecutive-failure logic as above | Heavier check (headless browser); runs less frequently |

With a 60-second interval and a 3-failure threshold, worst-case detection time is about 3 minutes; typical detection is faster since the clock starts at the first failed check, not the third.

---

## 11. API Requirements

Internal APIs for the backend serving the dashboard and handling checks. (Exact framework choice left to dev team; described here at the requirement level.)

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/monitors` | List all configured monitors (websites, CRM, server) |
| POST | `/monitors` | Add a new monitor |
| PUT | `/monitors/{id}` | Edit a monitor (interval, threshold, target) |
| DELETE | `/monitors/{id}` | Remove a monitor |
| GET | `/status` | Current live status of every monitor — powers the dashboard |
| GET | `/incidents` | List incidents (filter by status, target, date range) |
| GET | `/incidents/{id}` | Full detail + timeline for one incident |
| POST | `/incidents/{id}/acknowledge` | Record acknowledgment (user, timestamp) on an open incident — see §13.8. Coordination only; does not alter alerting or resolution logic |
| GET | `/targets/{id}/detail` | Powers the detail panel (§13.6): target header/current status, 30-day uptime %, avg response time, merged check+incident timeline, last ~20 raw checks |
| GET | `/server/metrics` | Latest server resource readings (CPU/RAM/disk/services) |

**Notes**
- `/status` should be fast and lightweight — this is what the dashboard polls/subscribes to for live updates.
- Incident creation/resolution is internal backend logic (triggered by the monitor engine), not a user-facing endpoint. Acknowledgment is the one user-facing incident endpoint, and it is additive metadata only — it never triggers resolution.
- `/targets/{id}/detail` should return the merged timeline pre-sorted (checks + incident events interleaved chronologically) so the dashboard doesn't have to merge two separate feeds client-side.

---

## 12. Database Design

Stores monitor configuration, check history, and incidents. (DB engine choice left to dev team — SQLite is sufficient at this scale; MySQL/PostgreSQL work equally well if preferred.)

**`monitors`** — one row per configured check
`id, name, type (website/crm/server), target_url, interval_sec, timeout_sec, expected_status, keyword, active, created_at`

**`checks`** — one row per executed check (high volume; apply a retention window, e.g. 90 days)
`id, monitor_id (FK), timestamp, status (up/down), response_time_ms, http_code, message`

**`server_metrics`** — time-series of server resource readings
`id, timestamp, cpu_load, mem_used_pct, disk_used_pct, inodes_used_pct, services_json`

**`incidents`** — one row per problem
`id, target_type, target_id, problem_type, severity, status (open/resolved), first_seen, resolved_at, downtime_sec, dedup_key, acknowledged_by, acknowledged_at`

`acknowledged_by` / `acknowledged_at` are nullable — set by `POST /incidents/{id}/acknowledge` (§11, §13.8) and never touched by the resolution logic. Acknowledged state is independent of open/resolved status: an incident can be open+acknowledged, open+unacknowledged, or resolved (acknowledged or not, doesn't matter once closed).

**`incident_events`** — timeline entries for an incident (check failures, email sent, resolved)
`id, incident_id (FK), timestamp, event_type, detail`

**Design notes**
- Index `checks` on `(monitor_id, timestamp)` for fast dashboard queries.
- Index `incidents` on `(status, dedup_key)` to enforce "one open incident per problem."
- `checks` and `server_metrics` are the high-volume tables — plan for retention/rollup as data grows.

---

## 13. Dashboard UI/UX Behavior

Single-page Plotly Dash application. This section reflects a design review pass (v1.1) that turned the original element list into concrete, buildable decisions — genre reference points are Grafana, Datadog, Better Uptime, UptimeRobot, Uptime Kuma. Classification: **App UI** (data-dense operations tool, not a marketing surface) — calm surface hierarchy, minimal chrome, utility language throughout.

### 13.1 Layout & Hierarchy

Top-to-bottom structure, in reading-priority order:

```
┌─────────────────────────────────────────────────────────┐
│ [Normal]:  Summary bar — "18 up · 1 down · server: OK"   │
│ [Outage]:  ⚠ SERVER UNREACHABLE — ALL CHECKS FAILING     │  ← replaces summary bar entirely, not additive
├─────────────────────────────────────────────────────────┤
│ Target card grid (down cards sort to top)                │
├─────────────────────────────────────────────────────────┤
│ Server health panel (CPU / RAM / disk / inode gauges)    │
├─────────────────────────────────────────────────────────┤
│ Incident history table                                    │
└─────────────────────────────────────────────────────────┘
```

- **Summary bar / correlated-outage banner:** during a correlated server-wide outage (see §9), the banner **replaces** the summary bar's content entirely rather than stacking above or beside it. Once the root cause is known ("server unreachable"), a live "0 up / 20 down" count is noise competing with the one fact that matters.
- **Main grid:** one card per monitored target (each website + the CRM). See §13.2 for full status/state spec. Down and overdue targets always sort to the top.
- **Server health panel:** live gauges/charts for CPU, RAM, disk, inode usage, plus a list of core services with up/down status.
- **Incident history table:** target, problem type, when it started, when it resolved (or "ongoing"), duration, acknowledgment marker (see §13.8).
- **Navigation model:** login is a separate, unauthenticated route. Once authenticated, the dashboard is genuinely one page — clicking a target card expands an **in-place slide-over or expanding row** (§13.6) rather than navigating to a new route. This preserves scroll position and live-refresh context and avoids back-button state management, matching the "single live page" intent.

### 13.2 Interaction States

The original spec (up/down/grey) collapsed several distinct real states into ambiguous buckets. Full state coverage:

| Feature | Loading | Empty | Error | Success | Partial/Stale |
|---|---|---|---|---|---|
| **Target card** | Skeleton card, no status color yet | N/A (card only exists once monitor is configured) | N/A — a target "error" IS the down state, already specified | Green, icon + "up" label, response time shown | **Overdue** (amber, "last checked Xm ago" emphasized — distinct from confirmed down) |
| **New monitor (no checks yet)** | — | Neutral grey card: "Awaiting first check" | — | Transitions to up/down on first check | — |
| **Dashboard `/status` feed** | Initial page load: skeleton grid, never a blank white page | — | **Stale-data banner**: keep last-known card states + timestamps visible, add a persistent "Data may be stale — dashboard connection lost, last updated Xm ago" strip | Live-refreshing grid, updates silently in place (no flash/reload jank) | Same stale-data banner covers partial feed failures |
| **Monitor grid (zero monitors)** | — | "No monitors configured yet" + a visible "Add monitor" action — not a blank grid | — | — | — |
| **Incident history table** | Skeleton rows | "No incidents recorded — all systems have been healthy since [date]" (framed as good news, not missing data) | Table-specific load failure: inline "Couldn't load incident history" + retry, doesn't take down the whole page | Populated rows, most recent first | — |
| **Server health gauges** | Skeleton gauge outlines | N/A (server health always exists once server is configured) | Same stale-data banner pattern as dashboard feed | Live gauge with numeric readout + color band | Gauge shows last value + a "stale" tick mark if the reading is overdue |

**Grey-state disambiguation:** "unknown/grey" from the original spec splits into two distinct visual treatments:
- **Neutral grey — "Awaiting first check":** a brand-new monitor, no judgment implied.
- **Amber — "Overdue":** an existing monitor that's gone quiet past its expected check interval. This is itself a signal that monitoring may be broken, not that the target is down — it must never look identical to a healthy or brand-new card.
- Confirmed down stays red, unchanged from the original spec.

**Dashboard-level error handling:** if `/status` (§11) itself fails or times out (a monitoring-system problem, distinct from a monitored target being down), the dashboard keeps showing last-known card states rather than going blank, but adds the persistent staleness banner above. Never let old green cards silently pass as current truth — but also never remove the team's last picture of the world during an active incident.

**Empty states are features:** the zero-monitors and zero-incidents states get purposeful copy (and, for zero-monitors, a visible primary action) rather than a generic "No data found" placeholder that reads as broken rather than empty.

Status indicator still uses both color and an icon/label together, never color alone (accessibility — unchanged from original spec, see also §13.5).

### 13.3 Visual Design System (minimal token set)

No DESIGN.md exists for this project yet — this is the minimal starter token set an implementer should treat as the seed of one, chosen specifically to avoid the generic default-Dash/Bootstrap look:

- **Typeface:** IBM Plex Sans for UI text, IBM Plex Mono for all numeric readouts (response times, percentages, uptime figures) — real typefaces built for data-dense enterprise tooling, not a default font stack (no Inter/Roboto/Arial/system-ui as primary).
- **Color tokens (dark-mode-first — this tool is checked during incidents, often at night):** `--color-bg`, `--color-surface`, `--color-text-primary`, `--color-text-secondary`, `--color-status-up` (muted, see §13.4), `--color-status-down`, `--color-status-warning`, `--color-status-unknown`, one `--color-accent` for interactive elements and focus rings. Named tokens, not ad hoc "red/green/grey."
- **Card treatment:** flat surface + 1px border, no drop shadows, no colored left-border accent, small border radius (4-6px). Status communicated via icon + label + a thin top-edge status strip, not decorative shadow/glow.
- **Spacing scale:** 4px base unit (4/8/12/16/24/32) applied consistently across the card grid, gauges, and table.
- **Accessibility floor:** body text ≥16px, contrast ratio ≥4.5:1 minimum on all text — including the muted "calm state" green in §13.4, which must stay above 4.5:1 against its background even while visually de-emphasized.

Recommend `/design-consultation` (a fuller design-system pass) only if this dashboard grows into a broader product surface beyond this one page.

### 13.4 Calm State vs. Incident State

This tool serves two opposite emotional contexts: a calm daily glance (should cost near-zero attention) and an active-incident lookup (often stressed, often on a phone, needs instant orientation). The dashboard should look and feel different between them, not just swap card colors:

- **Healthy/calm state:** deliberately quiet styling — muted green, minimal motion, gauges visually recede. The goal is that checking on a routine day feels like nothing.
- **Incident state:** higher contrast, the correlated-outage banner (§13.1), and motion/attention cues activate only when something is actually wrong. The page's overall visual energy is itself a severity signal, not just per-card color — this also guards against alert fatigue from a dashboard that always looks equally alarming.

### 13.5 Responsive & Accessibility

**Mobile layout (below ~640px width) — priority-reordered, not just reflowed:**
- Down/overdue targets always float to the top of the single column, regardless of add-order — the phone-glance use case is "what's broken right now," not "browse everything alphabetically."
- Summary bar / correlated-outage banner stays pinned at the top on scroll.
- Server gauges collapse to compact inline numeric readouts (e.g. "CPU 42% · RAM 61% · Disk 78%") until tapped to expand into the full chart widget.
- Incident history table becomes stacked record cards (target / problem / duration per card) instead of a horizontally-scrolling wide table.
- Touch targets (card tap-to-expand, gauge tap-to-expand) are minimum 44×44px.

**Screen reader / live-region behavior:** a single `aria-live="polite"` region announces only actual status transitions — "clientsite.com is now down," "Server unreachable — all checks failing," "clientsite.com recovered" — never a full-page re-announcement on every 10-30 second refresh poll. This gives screen reader users the same signal sighted users get from a card flashing red, without announcement noise every refresh cycle.

**Also required:** keyboard navigation for the card grid and detail panel (tab order follows visual priority — down/overdue targets first), visible focus states using `--color-accent`, and the color+icon/label pairing from §13.2 (never color alone).

### 13.6 Target Detail Panel

Clicking any card expands an in-place slide-over or expanding row (§13.1) containing:
1. Header — target name/URL and current status.
2. Compact stat row — 30-day uptime %, average response time.
3. One merged chronological timeline — check failures/recoveries and incidents interleaved together, not two disconnected lists.
4. The last ~20 raw checks in a small table.

Powered by `GET /targets/{id}/detail` (§11), which returns the merged timeline pre-sorted server-side.

### 13.7 Scale Behavior

The grid stays a flat, undifferentiated layout below roughly 20-25 monitored targets — grouping chrome isn't worth adding at small scale. Above that threshold (this system is expected to handle "dozens" of targets per §16):
- Cards group under collapsible section headers: **Websites / CRM / Server**.
- A lightweight text filter lets the user jump to a specific target by name.

Without this, a flat grid of 60-80 near-identical cards becomes unscannable even when everything is healthy, let alone during a partial outage.

### 13.8 Incident Acknowledgment

A single **Acknowledge** action is available on any open incident, from both the card and the incident history table:
- Records who acknowledged it and when (`acknowledged_by` / `acknowledged_at`, §12).
- Displays a subtle "seen by [name]" marker on the card and in the incident table.
- Does **not** stop alert emails, does not pause checks, and does not auto-resolve the incident (see §9). It is a coordination signal for a small team glancing at the same dashboard mid-incident — nothing more.

---

## 14. Email Alert Behavior

- **Trigger:** new incident opens (DOWN) → send email. Incident resolves (UP) → send email.
- **Content:** target name, problem type, severity, time detected (for DOWN); target name, "recovered," total downtime (for UP).
- **Subject line format:** these alerts are frequently read as a phone lock-screen notification, often off-hours — the subject line is structured to front-load severity + target so triage is possible without opening the email:
  - `[DOWN] clientsite.com — HTTP 500`
  - `[RECOVERED] clientsite.com — 12m downtime`
  - `[CRITICAL] Server — disk 96% full`
  - `[WARNING] Server — disk 92% full` (for warning-level server thresholds, per §7.2/§9)
- **Recipients:** managed directly by the dev team during implementation (per your instruction — not specified here).
- **Delivery:** standard SMTP send from the backend; log delivery success/failure for troubleshooting.

---

## 15. Edge Cases & Failure Handling

| Edge case | Behavior |
|---|---|
| **Flapping** (site goes up/down repeatedly in quick succession) | Consecutive-failure/-success thresholds absorb this; avoid re-alerting on every flap. |
| **Whole server goes down** | All website + CRM checks fail simultaneously — dashboard groups this as one correlated event (see §9, §13), not many separate incidents. |
| **Monitoring system itself crashes** | Run under a process manager with auto-restart. Optional: a small external heartbeat check (see §6) to detect if the monitor itself has gone silent. |
| **Duplicate/retried checks** | Deduplicated by (target, problem type) — a retried check doesn't create a second incident. |
| **SSL clock-skew false alarm** | Use a lead-time window (e.g. 14/7/1 days) rather than a single hard cutoff. |
| **Email delivery fails** | Log the failure; consider a simple fallback (e.g. retry once) — no secondary channel required for this scope. |
| **CRM Tier 1 check "passes" but CRM is actually broken inside** | Expected limitation of Tier 1 — it only confirms the login page is reachable, not that the CRM works internally. This gap is exactly what Tier 2 (future scope) is designed to close. |
| **A monitor goes quiet without technically failing** (checker crashed, scheduling issue) | Rendered as a distinct amber "overdue" card state (§13.2), not the same grey as a brand-new monitor and not the same red as a confirmed down target — surfaces monitoring-pipeline problems without misreporting the target's actual health. |
| **Dashboard's own `/status` feed fails or times out** | Last-known card states stay visible (never a blank page) with a persistent staleness banner (§13.2) — distinguishes "the monitored things are fine" from "we've lost sight of everything," which is itself an incident-worthy state. |
| **An acknowledged incident keeps failing / reopens** | Acknowledgment (§13.8, §9) never stops alerting or checks — the incident continues to email and update normally. The "seen by [name]" marker persists alongside continued alerts; only recovery (per the existing auto-resolve rule) closes it. |

---

## 16. Non-Functional Requirements

- **Reliability:** monitoring system uptime ≥ 99.9%; auto-restart on crash.
- **Security:** dashboard access behind authentication; monitoring-only CRM account (future Tier 2) has minimal permissions, credentials stored securely, never in plain code.
- **Performance:** should comfortably handle dozens of monitored targets without missing check intervals.
- **Auditability:** every incident and its timeline is stored and reviewable later.
- **Data retention:** configurable history window for checks (e.g. 90 days); incidents retained longer.
- **Optional resilience:** a small independent heartbeat/watchdog (see §6) is recommended so a full server crash doesn't also silence the alarm — flagged as a decision to make consciously, not a hard requirement for MVP.

---

## 17. MVP Scope & Future Roadmap

### MVP (build now)
1. Website monitors for every hosted domain (HTTP status, response time, SSL expiry).
2. CRM Tier 1 monitor (login page reachability).
3. Server health monitor (CPU, RAM, disk, inodes, core services) — enabled by root access.
4. Backend: check scheduling, threshold logic, incident creation/dedup/auto-resolve.
5. Database to store monitors, checks, incidents.
6. Plotly Dash dashboard: live status grid + server health panel + incident history.
7. Email alerts on DOWN and UP.

### Future roadmap (not built now)
| Phase | Focus |
|---|---|
| **Next** | CRM Tier 2 — synthetic login + workflow testing via headless browser (e.g. Playwright) |
| **Later** | Independent heartbeat/watchdog on a separate small box, for resilience if the main server crashes entirely |
| **Later** | Escalation rules if the team grows (e.g. notify a second person if unacknowledged) |
| **Later** | Public/client-facing status page, SLA-style uptime reporting |

---

## 18. QA & Acceptance Criteria

- [ ] A website returning an error for the configured consecutive-failure count opens exactly one incident and triggers an email within 2 minutes.
- [ ] Recovery auto-resolves the incident and records the correct downtime, with a recovery email sent.
- [ ] A single isolated failed check (not reaching threshold) does **not** open an incident or send an email.
- [ ] CRM login-page check behaves identically to a website check (same thresholds, same alerting).
- [ ] Server disk usage crossing the critical threshold opens an incident and sends an email.
- [ ] If the entire server is unreachable, the dashboard shows one correlated "server down" indication rather than many disconnected red cards.
- [ ] Dashboard auto-refreshes without manual reload and correctly reflects current status for every target.
- [ ] Incident history is accurate and queryable (start time, resolve time, duration).
- [ ] Correlated-outage banner replaces (not stacks with) the summary bar during a server-wide outage (§13.1).
- [ ] A monitor with no checks yet renders as neutral "Awaiting first check," visually distinct from both an overdue and a confirmed-down monitor (§13.2).
- [ ] A monitor that misses its expected check interval renders as amber "Overdue," distinct from confirmed down (§13.2).
- [ ] If `/status` fails or times out, the dashboard shows a staleness banner and retains last-known card states rather than going blank (§13.2).
- [ ] Zero-monitors and zero-incidents states show purposeful empty-state copy (and an "Add monitor" action for the former), not a generic "No data found" (§13.2).
- [ ] Email subject lines match the structured format, e.g. `[DOWN] target — problem` / `[RECOVERED] target — Xm downtime` (§14).
- [ ] Below ~640px width, down/overdue targets reorder to the top, gauges collapse to inline numeric readouts, and the incident table becomes stacked cards (§13.5).
- [ ] Screen reader users receive an `aria-live` announcement only on actual status transitions, never on every refresh poll (§13.5).
- [ ] Clicking a target card opens an in-place detail panel with 30-day uptime %, avg response time, a merged timeline, and the last ~20 raw checks (§13.6).
- [ ] Acknowledging an incident records who/when, displays a "seen by" marker, and provably does not affect alerting or auto-resolve behavior (§13.8, §9).
- [ ] At roughly 25+ configured targets, cards group under Websites/CRM/Server section headers and a text filter is available (§13.7).

---

## 19. Open Questions

1. Which specific core services should be checked on the server (web server, database, mail, DNS — confirm the exact stack running on this box)?
2. Confirm the full list of domains/websites to be monitored, and the CRM's exact login URL.
3. SMTP provider/credentials for sending email alerts — to be handled by dev team, flagging here so it's not missed.
4. Desired dashboard refresh rate (suggested 10–30 seconds — confirm any preference).
5. Timing for CRM Tier 2 (synthetic workflow testing) — confirm if/when this becomes a priority.

---

## 20. Assumptions & Constraints

- Everything runs on the existing **dedicated GoDaddy server** with root access; no separate VPS is required for MVP.
- Dashboard is built with **Plotly (Dash)**, implemented by the dev team directly in code (no drag-and-drop builder).
- Alerting is **email only** for this scope — no WhatsApp, SMS, or Slack integration included.
- **No AI, classification, or automated remediation** is part of this scope — every fix is manual, done by a human after being alerted.
- CRM has **no 2FA**, which simplifies any future Tier 2 automated-login testing.
- Email recipient list and SMTP setup will be configured directly by the dev team during implementation.
- Recommended check intervals and thresholds (§10) are defaults based on common industry practice and can be tuned after the system is live.
