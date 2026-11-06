# Rovix Monitoring — Setup & Operations

A short operational summary of how this deployment monitors your sites and
servers, what's currently configured, and how to manage it.

Production dashboard: **http://187.127.139.175:5000** (login `admin`).

---

## What Rovix watches

| Layer | What it checks | How |
|---|---|---|
| **Uptime** | Is each site/page responding? | HTTP check per monitor (status code) |
| **Content** | Did the *real* page render? | Optional **keyword** — page must contain it, else DOWN |
| **Latency** | Is the site slow? | Response-time; a separate WARNING incident when it's slow for 2 checks |
| **SSL** | Is the certificate valid / expiring? | `ssl_check.py` |
| **Server health (WHM)** | CPU / disk of the cPanel box | `whm_health.py` (pull) + the **push agent** for root-only metrics |
| **Security** | Imunify360 malware, cPHulk, ModSecurity | **push agent** → `POST /whm-security` (Rovix never holds root) |
| **DNS / domain** | A/AAAA-record changes, domain expiry | DNS-change detection + domain checks |

Alerts go out over **email (SMTP)** and any configured **webhook channels**
(Slack / Discord / generic). A global notify toggle and maintenance windows mute
every channel at once.

---

## How down-detection works (and the best-practice settings applied)

1. **Interval** — how often a monitor is checked. Your key/healthy homepages are
   set to **60 seconds** for fast "sudden down" detection.
2. **Confirmation** — a monitor must fail **3 checks in a row** before it's
   declared DOWN (this prevents a one-second blip from alarming you). So at a 60s
   interval, a real outage alerts in ~1–3 minutes.
3. **Content keyword (the important one)** — a basic check only asks "did it
   respond?" A site can return **HTTP 200 while being completely broken** (blank
   page, "Index of /" directory listing, PHP error). A keyword fixes this: Rovix
   verifies the page contains a known phrase from the healthy page; if it's
   missing, the site is DOWN even on a 200. Keywords are applied **selectively**
   to verified sites, because they're brittle — a site redesign that changes the
   phrase would cause a false alarm.
4. **Recovery** — when the site responds correctly again, the incident resolves
   and you get a RECOVERED email.

---

## Current state (as configured)

- **~29 healthy homepages**: 60s interval **+ a verified content keyword** — fully
  protected (catches real downs *and* broken-but-200 pages).
- **All WHM/security/DNS/latency/SSL layers**: active.
- **Alert emails**: mobile-responsive; delivered from the VPS.

### ⚠️ Sites that return 200 but are actually broken (fix these)
These show "up" because they respond, but serve no real site:

| Site | Problem |
|---|---|
| `palmrekha.co.in`, `rovixai.co`, `demo.thebrandchimp.com` | "Index of /" directory listing (not deployed) |
| `nawabsbarrie.ca` | blank page (~54 bytes) |
| `sridharswamiji.com`, `ravixai.com` | near-empty page (~114 bytes) |

Once the real site is deployed to each, they can be keyword-protected like the rest.

---

## Managing monitors

**In the dashboard** (Monitors → Add / Edit Monitor):
- **Interval** — how often to check (min 30s).
- **Keyword** — a phrase from the healthy page (leave blank for status-only).
- **Expected status** — e.g. a health endpoint that should return `401`/`204`.
- **Discover pages** — auto-find a site's sub-pages to monitor.

**Via the REST API** (API-key protected, header `X-API-Key`):
- `GET /status`, `GET /monitors`, `POST /monitors`, `PUT /monitors/<id>`, `DELETE …`
- `POST /whm-security` — the server-side security push agent's endpoint.

---

## Security push agent (WHM)

Root-only server metrics (Imunify360 malware, disk, cPHulk) can't be read by a
non-root WHM token, so a small cron on the WHM server pushes them to Rovix
instead — see `deploy/whm_security_push.sh`. Rovix evaluates thresholds and
alerts; it never holds any WHM credential.

---

## Deploying code changes

Code lives on GitHub (`sai12teja/uptime-monitoring`, mirrored to
`RovixAI/rovix-wacthover`). The app runs as a Docker container on the VPS.

On the VPS (Hostinger → Docker Manager → Web console):
```bash
cd ~/rovix-app && git pull && docker compose up -d --build
# if "container name already in use":
docker rm -f rovix-uptime && docker compose up -d
```
Secrets live in `~/rovix-app/.env` on the VPS (never in git). The database,
session key, and uploaded logos persist across rebuilds via Docker volumes.

> **Note:** monitor config changes (interval, keyword, add/remove) are made live
> via the dashboard/API and take effect immediately — no redeploy. Only **code**
> changes need the `git pull` + rebuild above.
