# How Rovix Uptime Monitoring Works

A practical walkthrough of how the project runs **today** — the architecture, the
request path, the background monitoring loop, the data model, and how it's
deployed. Numbers below are the real defaults from the code, not placeholders.

> For *what* it should do (product requirements), see
> `PRD_Website_CRM_Server_Monitoring_Dashboard.md`. For *deploying* it, see
> `DEPLOY_HOSTINGER.md` and `deploy/VPS_DEPLOY_CHEATSHEET.md`. This file is the
> *how it actually works* middle layer.

---

## 1. What it is

A self-hosted uptime/health dashboard for websites, CRMs, TCP/DNS services, and
the VPS itself. It runs as **one Python process** that does two jobs at once:

1. **Serves a web dashboard** (a [Dash](https://dash.plotly.com/) app, Flask
   underneath) where you log in and see every monitored target, incidents, and
   server health.
2. **Runs a background scheduler** in a thread that actively checks every
   monitor on its own interval, opens/resolves incidents, and emails alerts.

Both live in the same process on the same port on purpose — see §8 for why it's
a *single* process with a *single* worker.

### Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11 (dev) / 3.12 (Docker image) |
| Web framework | Dash 2.18.2 on Flask (its `app.server`) |
| Charts | Plotly 5.24.1 |
| Storage | SQLite (one file, `rovix.db`) — no external DB |
| WSGI server | `waitress` on Windows, `gunicorn` on Linux |
| Deployment | Docker (`docker-compose`) or systemd on a VPS |

There is **no separate frontend build, no Node, no message queue, no Redis**.
Everything is Python + SQLite.

---

## 2. Starting up (entry points)

There are two ways the app boots, and both do the *same three setup steps*:

1. Set the Flask session secret key (persisted, so logins survive restarts).
2. Start the background check scheduler (`monitor_engine.start_background_scheduler`).
3. Start the favicon-backfill thread (`data.backfill_favicons`).

| Entry point | Used by | How |
|---|---|---|
| `app.py` → `if __name__ == "__main__"` | Windows dev, systemd deploy | `python app.py` (waitress on `:8050`), or `python app.py --debug` (Dash dev server, hot reload) |
| `wsgi.py` | Docker / gunicorn | `gunicorn wsgi:server` — replicates the three setup steps, then exposes `server` |

**Why `wsgi.py` exists:** importing `app.py` as a plain module (what
`gunicorn app:server` would do) never runs its `__main__` block — so the
scheduler and session key would never start. `wsgi.py` does that setup
explicitly. (This was the bug fixed in commit "Fix Docker startup: add wsgi.py
entrypoint".)

The app listens on **port 8050** inside the container; `docker-compose.yml`
publishes it to the VPS as **port 5000** (`http://<vps-ip>:5000`). No domain or
reverse proxy is wired up yet.

---

## 3. The web app (request path)

```
app = Dash(__name__, suppress_callback_exceptions=True)
app.layout = serve_layout            # a CALLABLE → re-run on every page load
auth.register_auth(app.server)       # login gate for the whole dashboard
api.register_api(app.server)         # REST API (same process/port)
push.register_push(app.server)       # passive check-in endpoint
```

### Authentication (`auth.py`)

- A Flask `before_request` hook gates **every** route. No session → redirect to
  `/login`. The REST API (`api`) and push (`push`) blueprints are exempt — they
  authenticate themselves (API key / secret token).
- **No self-service signup.** Accounts are created only with
  `python manage_users.py add <username>`. Passwords are hashed with werkzeug.
- Brute-force protection: **5 failed attempts → 15-minute lockout**, counted
  in-memory per username. Routes: `/login`, `/logout`, `/password` (change own
  password).

### The dashboard UI (`app.py`)

`app.py` is the whole UI (~2000 lines). It's a **single-page Dash app** with
client-side "pages" (Monitors, Incidents, Server, etc.) that are swapped by
callbacks rather than real URLs. Key pieces:

- `serve_layout()` builds the page fresh on each load (topbar, sidebar, summary,
  target grid, incident table, server panel).
- **Live refresh:** a `dcc.Interval` fires periodically and the `refresh`
  callback rebuilds the summary, the target grid (filterable), the server panel,
  and the incident table from the DB. A second one-shot 700 ms timer swaps
  skeleton loaders for real content so first paint feels instant.
- Targets are **grouped** (a site + its sub-checks render as one card), with
  favicons, letter-badge fallbacks, status tiers, and uptime/MTTR stats.
- The UI only ever **reads** through `data.py`; it never runs checks itself.

`data.py` is the read-model/formatting layer between the DB and the UI (status
labels, grouped targets, summary counts, incident dicts, favicon backfill,
and CRUD wrappers for add/update/delete target).

---

## 4. The monitoring loop (the heart of it)

Started once at boot as a daemon thread (`start_background_scheduler`). It loops
**every `TICK_SEC = 5` seconds** and runs four jobs in order:

```
while True:
    run_due_checks()               # 1. check monitors whose interval elapsed
    server_health.check_server_metrics()   # 2. CPU/mem/disk/inodes/services
    ssl_check.check_all_ssl(...)   # 3. TLS cert expiry (gated to ~once/24h)
    purge_old_checks()             # 4. delete checks > 90 days (gated to once/day)
    sleep(5)
```

### `run_due_checks()`

1. Ask the DB for monitors whose `interval_sec` has elapsed since
   `last_checked_at` (default interval **600 s**).
2. Run their checks **concurrently** in a thread pool (`MAX_WORKERS = 10`).
3. Collect the "something changed" events and hand them to
   `handle_tick_events()` (alerting — §6).

### Monitor types and how each is checked

| `type` | Check | Notes |
|---|---|---|
| `website` | HTTP GET via `do_http_check` | optional `keyword` must appear in the body |
| `crm` | HTTP GET via `do_http_check` | same as website; separate label only |
| `tcp` | socket connect to `url:port` | `do_tcp_check` |
| `dns` | DNS resolve | `do_dns_check` |
| `push` | **passive** — nothing is dialed out | the target pings `/push/<token>`; a *missed* check-in within its interval is the failure |

HTTP checks have a **10 s timeout**, a `RovixMonitor/1.0` user agent, support
non-GET methods with a JSON/form body, handle gzip when matching a keyword, and
run behind an **SSRF guard** (`_is_blocked_host`) that refuses targets resolving
to private/internal addresses.

### The up/down state machine (`evaluate_status`)

A pure function decides transitions from each check result:

- **Default:** `3` consecutive failures → **down**; `2` consecutive successes →
  back **up**. (So a single blip never pages anyone.)
- Per-monitor override: `retries` column → fail threshold is `retries + 1`
  (e.g. `retries = 0` = down on the first failure, Uptime-Kuma style).
- **Push** monitors use `1` fail / `1` ok — a missed or received check-in is
  unambiguous.

On a real transition it opens or resolves an **incident** and logs an
`incident_event`. Every check (pass or fail) is written to the `checks` table so
uptime % and history stay accurate regardless of alerting.

---

## 5. Data model (SQLite)

One file, created/migrated on startup by `db.init_db()`. New columns are added
with `ALTER TABLE ... IF missing` so upgrades don't lose data. Every call opens
its own short-lived connection (no shared global connection).

| Table | Holds |
|---|---|
| `monitors` | one row per target: name, url, type, keyword, interval, thresholds, status, last check, group key, notify flag, push token, favicon |
| `checks` | every individual check result (ts, ok, response_ms, detail) — the raw history, purged after 90 days |
| `incidents` | opened/resolved outages, deduped by `(monitor, problem_type)`; severity, started/resolved, acknowledged_by |
| `incident_events` | timeline per incident (check_failure, email_sent, …) |
| `server_metric_state` | hysteresis state for each server metric; also reused as a generic "last-run" timestamp for SSL/purge cadence |
| `users` | login accounts (username + werkzeug password hash) |
| `settings` | single row (id = 1): up to 3 alert recipient emails + a global notify toggle |

`ROVIX_DB_PATH` env var points the app at the DB file; unset, it defaults to
`rovix.db` in the working directory. In Docker it's a mounted volume
(`/app/data/rovix.db`) so data survives rebuilds.

---

## 6. Alerting (`email_alerts.py` + `handle_tick_events`)

After a tick's checks commit, `handle_tick_events()` decides what to email:

1. **Correlated outage first.** If there are ≥ 3 monitors and enough are down at
   once (`compute_is_correlated_outage`), it opens **one** "correlated outage"
   server incident and sends **one** alert — and suppresses the individual
   per-site alerts that tick (they share a root cause). When it clears, it sends
   one recovery.
2. **Otherwise, batching.** Individual "down" and "recovered" events are
   buffered and sent as **one consolidated email per 60-second window**
   (`ALERT_BATCH_WINDOW_SEC`), so a burst of failures is one email, not ten. A
   lone event still goes out after the window even if no second event arrives
   (the batch is flushed every tick).
3. The per-monitor `notify` flag and the global toggle can suppress alerts.

Emails are rich HTML ("premium" card templates with a recent-check timeline),
sent by `email_alerts.send()` over SMTP. It supports **STARTTLS and implicit-TLS
(port 465)**. Recipients come from the Settings modal (`settings` table), with
an optional `ALERT_TO` env fallback. **If `SMTP_HOST` is unset, alerts are
silently skipped** — a deliberate no-op, not a crash. Every sent alert is logged
as an `email_sent` incident event.

---

## 7. The other background checks

- **Server health (`server_health.py`):** reads CPU, memory, disk, and inode
  usage (Linux `/proc` etc.) plus systemd service states for
  `nginx, postgresql, postfix, named`. Thresholds: warn/crit at **80/95%**
  (CPU, memory) and **90/95%** (disk, inodes). A metric must stay in a zone for
  **2 consecutive reads** before it flips (hysteresis, so a momentary spike
  doesn't alert). Readings cached for 60 s.
- **SSL (`ssl_check.py`):** for each website monitor, checks the TLS cert expiry
  and warns when **≤ 14 days** remain. Runs about **once every 24 hours**.
- **Favicon / page discovery (`page_discovery.py`, `data.backfill_favicons`):**
  a startup thread resolves a site icon for any monitor missing one; cards show
  a letter badge until it lands.
- **Retention:** `checks` rows older than **90 days** (`CHECKS_RETENTION_DAYS`)
  are purged once a day.

---

## 8. Why single process / single worker

The Docker `CMD` runs gunicorn with **1 worker, 4 threads**; the systemd unit
matches it. This is intentional, because two pieces of state live **in memory**:

- the **background scheduler** — a second worker would double-check every site;
- the **login rate-limiter** — a second worker would keep its own counter and
  halve the brute-force protection.

So the app scales by threads within one process, not by multiple workers. (If it
ever needs more, the rate-limiter and scheduler would have to move to the DB
first — noted in `auth.py`.)

---

## 9. The REST API and push endpoint

- **`api.py`** registers `/monitors` routes on the same Flask server. It's gated
  by an `API_KEY` env var — **every route returns 503 until the key is set** —
  and is meant for scripts, so it bypasses the browser login.
- **`push.py`** registers `/push/<token>`. A passive ("heartbeat") monitor's
  target calls this URL on its own schedule; the server looks the monitor up by
  its server-generated secret token and records a successful check-in. Missing
  the check-in window is what marks it down.

---

## 10. Configuration (`.env`)

`env_file.load()` reads a `.env` from the working directory at startup. **Real
environment variables always win** over the file, so a container/systemd unit
can inject the same keys. `.env` is gitignored; `.env.example` is the template.

| Key | Purpose |
|---|---|
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `ALERT_FROM` | outbound alert email (port 465 = implicit TLS, else STARTTLS) |
| `DASHBOARD_URL` | public URL put in the "view dashboard" link in alerts |
| `ALERT_TO` | fallback recipients (ignored when set in the Settings modal) |
| `API_KEY` | enables `api.py`'s REST routes |
| `SESSION_SECRET_KEY` | pins login sessions across restarts |
| `ROVIX_DB_PATH` / `ROVIX_SESSION_KEY_PATH` | relocate DB / session key (used by the Docker volumes) |
| `TZ` | local time for alert timestamps (e.g. `Asia/Kolkata`) |
| `CHECKS_RETENTION_DAYS` / `ALERT_BATCH_WINDOW_SEC` | tune retention / alert batching |

---

## 11. Running it

**Local (dev):**
```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
python app.py            # waitress on http://localhost:8050
python manage_users.py add admin   # create a login (no default account exists)
```

**Docker (production shape):**
```bash
cp .env.example .env     # fill in real SMTP etc.
docker compose up -d --build        # gunicorn, reachable at :5000
```

Data (DB, session key, uploaded logos) persists in named Docker volumes across
rebuilds. The systemd path is in `DEPLOY_HOSTINGER.md`; `deploy/` has the nginx
config and the service unit.

---

## 12. Module map (quick reference)

| File | Responsibility |
|---|---|
| `app.py` | the entire Dash dashboard UI + callbacks; dev/systemd entry point |
| `wsgi.py` | gunicorn/Docker entry point (starts scheduler + backfill, exposes `server`) |
| `data.py` | read-model/formatting layer between DB and UI; target CRUD; favicon backfill |
| `db.py` | SQLite schema, migrations, and all queries |
| `auth.py` | session login gate, rate limiting, password change |
| `monitor_engine.py` | the background scheduler, all check types, state machine, incident + alert orchestration |
| `email_alerts.py` | SMTP sending + HTML alert/digest templates |
| `server_health.py` | host CPU/mem/disk/inode + service-state checks |
| `ssl_check.py` | TLS certificate expiry checks |
| `page_discovery.py` | favicon + page discovery for a site |
| `push.py` | `/push/<token>` passive check-in endpoint |
| `api.py` | API-key-gated REST endpoints |
| `env_file.py` | minimal `.env` loader |
| `manage_users.py` | CLI to create/list login accounts |
| `test_*.py` | pytest suite (~40 files) covering engine, alerts, API, auth, UI builders |
