# Rovix Dashboard: Production Readiness & Hostinger VPS Deployment Audit

**Prepared by:** Manus AI  
**Date:** August 12, 2026  
**Target Platform:** Hostinger Virtual Private Server (VPS) running Ubuntu / Debian Linux  

---

## Executive Summary

The **Rovix Dashboard** (`rovixdashboard.zip`) is a robust, self-hosted uptime monitoring and server metrics application built on **Python**, **Dash**, and **Flask**, backed by an embedded **SQLite** database. It features real-time website and CRM checks, automated background monitor engines, server health diagnostics, incident management, email alerts, REST API integrations, and session-based operator authentication.

This audit evaluates the application's user interface, backend architecture, security controls, and readiness for production deployment on a Hostinger VPS. Overall, the codebase is remarkably well-structured, modular, and equipped with thorough unit tests. However, deploying it on a live production VPS requires proper process management (Gunicorn), a reverse proxy (Nginx), TLS/SSL encryption, and environment variable hardening.

---

## 1. Frontend UI & Experience Evaluation

The dashboard interface is built using **Dash** component trees styled with custom CSS (`assets/style.css`) and Plotly integrations. 

### Key Strengths
- **Responsive Layout & Design:** The UI includes clean cards, status badges (`up`, `down`, `overdue`, `awaiting`), sidebar navigation, and interactive filtering.
- **Accessibility & Diffing:** The frontend incorporates `aria-live` status diffing (`diff_status_messages`) to announce state changes efficiently without spamming screen readers or polling loops on every no-op interval.
- **Visual Feedback:** Stat cards support warning and critical thresholds (e.g., CPU, memory, disk, and inode utilization) with visual color-coding.

### Recommended Improvements
- **Static Asset Caching:** Ensure Nginx is configured to aggressively cache static files under `/assets/` (CSS, JS, logos) to reduce latency and server load.
- **Client-Side Error Boundaries:** Add fallback error states in Dash callbacks to gracefully handle intermittent network drops or API timeouts during polling cycles (defaulted to 15-second intervals).

---

## 2. Backend & Security Architecture Audit

The backend consists of Flask (`app.server`), a REST API (`api.py`), push monitoring (`push.py`), authentication (`auth.py`), and background check routines (`monitor_engine.py`, `server_health.py`).

### Key Strengths
- **Secure Authentication & Lockout:** Operator login is protected by session cookies, password hashing (`werkzeug.security`), and brute-force protection with account lockout windows (`MAX_LOGIN_ATTEMPTS = 5`, 15-minute cooldown).
- **Constant-Time API Key Validation:** The REST API uses `hmac.compare_digest` against `X-API-Key` headers, preventing timing attacks on API authentication.
- **Robust Database Layer:** SQLite access is properly parameterized and structured via `db.py`, ensuring data integrity across checks and incidents.
- **SSRF & Security Safeguards:** Includes targeted URL validation and SSRF defenses (`ssl_check.py`) for external monitoring probes.

### Identified Vulnerabilities & Mitigation
- **In-Memory Lockout Store:** The brute-force tracker (`_ATTEMPTS` in `auth.py`) is stored in process memory. If deployed with multiple Gunicorn worker processes, each worker maintains isolated counters. **Recommendation:** For single-worker deployments (recommended for this scale), run Gunicorn with `--workers 1` or migrate attempt counters to the SQLite database if multi-worker scaling is required.
- **Flask 3 Blueprint Registration:** Ensure that blueprint routes are registered at module load time rather than inside function wrappers called repeatedly across test runners or reloads, maintaining strict compatibility with Flask 3.x sans-io rules.

---

## 3. Hostinger VPS Production Deployment Guide

To deploy Rovix Dashboard securely and reliably on a Hostinger VPS (Ubuntu 22.04 / 24.04), follow the step-by-step production setup outlined below.

### Step 1: System Preparation & Dependencies
Log into your Hostinger VPS via SSH, update system packages, and install Python 3, pip, and Nginx:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv nginx git curl certbot python3-certbot-nginx
```

### Step 2: Project Setup & Virtual Environment
Upload or extract your project files to `/var/www/rovixdashboard` (or `/home/ubuntu/rovixdashboard`), create a virtual environment, and install dependencies:

```bash
sudo mkdir -p /var/www/rovixdashboard
sudo chown -R $USER:$USER /var/www/rovixdashboard
# Copy or extract project files into /var/www/rovixdashboard
cd /var/www/rovixdashboard

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt gunicorn
```

### Step 3: Configure Environment Variables
Create a `.env` file in the project root to store sensitive production secrets. **Never hardcode secrets in source code.**

```ini
SESSION_SECRET_KEY=generate_a_secure_random_hex_here_using_openssl_rand_hex_32
API_KEY=generate_another_secure_api_key_here
SMTP_HOST=smtp.yourprovider.com
SMTP_PORT=587
SMTP_USER=alerts@yourdomain.com
SMTP_PASS=your_smtp_password
ALERT_FROM_EMAIL=alerts@yourdomain.com
ALERT_TO_EMAIL=admin@yourdomain.com
```

Generate secure secrets on your server using:
```bash
openssl rand -hex 32
```

### Step 4: Configure Gunicorn WSGI Service
Create a systemd service file to run Gunicorn in the background and ensure automatic startup on system reboots.

Create file `/etc/systemd/system/rovixdashboard.service`:

```ini
[Unit]
Description=Rovix Dashboard Uptime Monitor
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/rovixdashboard
Environment="PATH=/var/www/rovixdashboard/venv/bin"
ExecStart=/var/www/rovixdashboard/venv/bin/gunicorn --workers 1 --bind 127.0.0.1:8000 app:server

[Install]
WantedBy=multi-user.target
```

*Note: `--workers 1` is recommended to maintain in-memory state consistency for monitoring schedules and login attempt tracking.*

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable rovixdashboard
sudo systemctl start rovixdashboard
sudo systemctl status rovixdashboard
```

### Step 5: Configure Nginx Reverse Proxy
Create an Nginx server block configuration for your domain at `/etc/nginx/sites-available/rovixdashboard`:

```nginx
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support for Dash hot-reload / live updates if needed
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /assets/ {
        alias /var/www/rovixdashboard/assets/;
        expires 30d;
        add_header Cache-Control "public, no-transform";
    }
}
```

Enable the site and test Nginx configuration:
```bash
sudo ln -s /etc/nginx/sites-available/rovixdashboard /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### Step 6: Secure with Free SSL / TLS (Let's Encrypt)
Run Certbot to automatically issue and configure an SSL certificate for your domain:

```bash
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com
```
Certbot will automatically configure HTTPS redirects and SSL parameters in your Nginx configuration.

---

## 4. Production Readiness Scorecard

| Evaluation Category | Status | Remarks |
| :--- | :---: | :--- |
| **UI & Responsiveness** | **Pass** | Clean Dash layout, CSS styling, responsive grid, accessible status diffing. |
| **Backend Architecture** | **Pass** | Robust modular architecture (`db.py`, `monitor_engine.py`, `server_health.py`). |
| **Security Controls** | **Pass** | HMAC API keys, password hashing, lockout logic, SSRF protections. |
| **Process Management** | **Action Required** | Must use Gunicorn + systemd service (outlined above) instead of `python app.py`. |
| **Reverse Proxy & SSL** | **Action Required** | Must configure Nginx and Let's Encrypt SSL on the Hostinger VPS. |
| **Environment Hardening** | **Action Required** | Must populate `.env` with secure secrets before production launch. |

---

## Conclusion & Next Steps

The Rovix Dashboard is **production-ready in code quality and feature completeness**, provided it is deployed following the hardening steps outlined in this guide. 

To complete your deployment on your Hostinger VPS:
1. Transfer project files to `/var/www/rovixdashboard`.
2. Set up the Python virtual environment and install dependencies.
3. Populate the `.env` file with secure production secrets.
4. Configure the **systemd** service and **Gunicorn**.
5. Set up **Nginx** and enable **SSL** via Certbot.

Once these steps are completed, your uptime monitoring and server metrics dashboard will run securely and reliably 24/7 on your Hostinger VPS.
