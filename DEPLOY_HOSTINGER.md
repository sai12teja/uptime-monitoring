# Deploying to a Hostinger VPS (Ubuntu/Debian)

Adapted to this actual codebase — a generic audit report
(`deployment_readiness_report.md`) got the shape right but had two
inaccuracies fixed here: it aliases `/assets/` past the login gate (a real
security regression, see `deploy/nginx-rovixdashboard.conf`), and its `.env`
uses `ALERT_FROM_EMAIL`/`ALERT_TO_EMAIL`, which `email_alerts.py` does not
read (the real names are `ALERT_FROM`/`ALERT_TO`).

## Step 0 — DNS + firewall (do this first, both take time to propagate)

1. **Point your domain at the VPS.** In your domain registrar (or
   Hostinger's Domains panel if it's registered there), create an A record:
   ```
   yourdomain.com     A     187.127.139.175
   www.yourdomain.com A     187.127.139.175
   ```
   DNS can take anywhere from minutes to a few hours to propagate. Certbot
   in Step 6 will fail its HTTP challenge if the domain doesn't resolve to
   this VPS yet — check with `dig yourdomain.com` before running it.

2. **Open 80/443 on the VPS.** Hostinger's KVM plans ship with the OS
   firewall (`ufw`) typically inactive, but confirm nothing in Hostinger's
   own VPS panel firewall (Manage → Firewall, if present on your plan) is
   blocking inbound 80/443 — that's a separate layer from `ufw` and Nginx
   never sees traffic Hostinger's panel already dropped.
   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow 'Nginx Full'   # opens both 80 and 443
   sudo ufw enable
   sudo ufw status
   ```

## Step 1 — System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv nginx git curl certbot python3-certbot-nginx
```

## Step 2 — Project + virtualenv

```bash
sudo mkdir -p /var/www/rovixdashboard
sudo chown -R $USER:$USER /var/www/rovixdashboard
# upload the project here (scp / git clone / sftp)
cd /var/www/rovixdashboard

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt   # picks up gunicorn on Linux automatically
                                   # (requirements.txt uses a platform marker,
                                   # so this is the same file used on Windows)
```

Do **not** upload `rovix.db`, `session_secret.key`, or `.env` from a dev
machine — generate/create those fresh on the server (next step). All three
are already gitignored.

## Step 3 — `.env`

Copy the template and fill in real values:

```bash
cp .env.example .env
nano .env
```

```ini
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASS=your-gmail-app-password
ALERT_FROM=you@example.com

DASHBOARD_URL=https://yourdomain.com

# Only needed if you use the REST API (api.py) -- every /monitors route
# 503s until this is set:
#API_KEY=

# Optional: pins login sessions across restarts. If unset, app.py generates
# one and persists it to session_secret.key in the project directory (same
# effect, no action needed).
#SESSION_SECRET_KEY=
```

`env_file.py` loads this at startup (top of `app.py`) regardless of how the
process is launched — confirmed by killing and restarting the app from a
shell with none of these variables set and watching a real alert email still
send. Real environment variables (e.g. injected by systemd) always take
precedence over the file.

Generate a `SESSION_SECRET_KEY` / `API_KEY` if you want them systemd-managed
instead of file-based:
```bash
openssl rand -hex 32
```

## Step 4 — First-run: create the admin account

`manage_users.py` has `add` and `list` only — no delete, so pick the
username deliberately:

```bash
source venv/bin/activate
python manage_users.py add admin
# prompts for a password, never echoed or logged
```

This also runs `db.init_db()` as a side effect, creating `rovix.db` fresh
on the server.

## Step 5 — systemd service

```bash
sudo cp deploy/rovixdashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rovixdashboard
sudo systemctl start rovixdashboard
sudo systemctl status rovixdashboard
```

**`--workers 1` in the unit file is required, not a tuning suggestion.**
Two things in this codebase are process-local state:
- `auth.py`'s login rate limiter (`_ATTEMPTS`, an in-memory dict) — a second
  worker keeps its own independent counter, halving the real lockout
  protection for an attacker who spreads guesses across both.
- `monitor_engine.start_background_scheduler()`, started once in `app.py`'s
  `__main__` — a second worker would run a second copy of the check loop,
  hitting every monitored site twice as often and sending duplicate alert
  emails per incident.

If you ever need more than one worker (heavy REST API traffic, say), the
lockout store and scheduler both need to move to the database first — flagged
in `auth.py` with a `ponytail:` comment for exactly this.

Check logs if it doesn't start:
```bash
sudo journalctl -u rovixdashboard -f
```

## Step 6 — Nginx + TLS

```bash
sudo cp deploy/nginx-rovixdashboard.conf /etc/nginx/sites-available/rovixdashboard
sudo nano /etc/nginx/sites-available/rovixdashboard   # replace yourdomain.com
sudo ln -s /etc/nginx/sites-available/rovixdashboard /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```

`deploy/nginx-rovixdashboard.conf` proxies **everything** through gunicorn,
including `/assets/`. Do not add a static `alias` for `/assets/` — that
route is gated by `auth.py`'s session check same as the rest of the
dashboard (confirmed: `require_login` excludes only `/login`, `/logout`,
and the `api`/`push` blueprints), and a static alias would serve every
uploaded client logo and the CSS to anyone, logged in or not.

## Step 7 — Update DASHBOARD_URL, verify

Once the domain resolves and TLS is live, confirm `.env`'s `DASHBOARD_URL`
matches it (used to build the "view dashboard" link inside alert emails —
`email_alerts.py`), then restart:

```bash
sudo systemctl restart rovixdashboard
```

Smoke test:
```bash
curl -I https://yourdomain.com/login   # expect 200
```

Log in, open **Settings → and the /password page** to set a real password
if you deployed with a placeholder one.

## What's already handled in the code (no VPS-specific action needed)

- **Server health metrics** (`server_health.py`) read `/proc/meminfo` and
  detect `os.name` — CPU/RAM will actually report real numbers on this
  Linux VPS, unlike Windows dev where those collectors return `null`.
- **Rate limiting** (5 attempts / 15 min lockout, per username) is already
  implemented and tested (`test_password_change.py`).
- **Secrets** are never hardcoded; `db.py`, `rovix.db`, `.env`, and
  `session_secret.key` are all gitignored.
- **SSRF guard** (`monitor_engine._is_blocked_host`) blocks link-local /
  RFC1918 / metadata-endpoint targets for every check, so a monitor can't be
  used to probe the VPS's own internal network.

## After this goes live: retire the Windows/ngrok copy

The dev instance on your Windows machine (`https://wilbur-rheumatoid-
comparatively.ngrok-free.dev`, `admin`/`123456`) is a **separate app with
its own `rovix.db`** — it does not sync with the VPS. Once this deployment
is confirmed working:

- Decide whether the 42 monitors already configured on the Windows copy
  need to be recreated here, or whether this is meant to be a clean start.
- Stop the Windows app + ngrok tunnel, or at least change that `admin`
  password — it's still live and reachable at that URL until you do.
- Don't run both long-term pointed at the same monitored sites: two
  independent schedulers means duplicate checks and duplicate alert emails
  for every incident.
