# WHM API Access Request — Rovix Server Monitoring

**For:** the WHM/cPanel administrator
**From:** the team running Rovix (website + server monitoring)
**Why:** Rovix is adding server-resource and security monitoring for our cPanel
servers (a server running low on disk recently broke a client CRM while the
site still returned HTTP 200, so uptime checks missed it). Rovix needs
**read-only** access to WHM to pull resource/security stats and alert us.

> **Security note:** Rovix only ever *reads* stats. The token below must be
> **scoped read-only, never root / full access** — Rovix's code has no
> permission to change, delete, or configure anything on the server.

---

## 1. Create one WHM API token per server

WHM → **Development → Manage API Tokens → Create**

- Scope it **read-only** via ACLs — **not** a root-equivalent / all-access token.
- One token per server we host sites on.

## 2. The token must be allowed to call these read functions

Grant the minimal read-only ACLs that cover the functions below.

### Standard / confirmed — needed for the core features

| WHM function | Rovix uses it for |
|---|---|
| `listaccts` | auto-discover and monitor **every** site, no manual entry |
| `getdiskusage` | **disk-space** monitoring (the incident driver) |
| `systemloadavg` | **CPU load** monitoring |

### Please confirm the exact function name — and that the token's ACL covers it

We will **not** guess these; each stays switched off until you confirm it.

| Signal | We need the WHM / plugin function that returns… |
|---|---|
| **Memory** | current **used / total** memory |
| **Mail queue** | number of **queued mail messages** |
| **ImunifyAV / 360** | current **malware detections** |
| **ModSecurity** | recent **blocked / attack requests** |
| **cPHulk** | **failed-login / brute-force** events |

## 3. Please send back (securely — secrets manager, not email/chat)

- [ ] WHM **hostname** of each server
- [ ] WHM **port** (default `2087` — confirm)
- [ ] The **API token** value for each server
- [ ] WHM **user** (default `root` — confirm)
- [ ] **CPU core count** per server (needed to express load average as a %)
- [ ] **Typical mail-queue size** per server (so we set sensible warning/critical counts — a raw count has no universal baseline)
- [ ] The **confirmed function names** from section 2
- [ ] Does WHM on `:2087` use a **valid TLS certificate or a self-signed one**?

## 4. Optional, non-blocking — interim safety net

While the above is being set up, you can enable WHM's own native warning as a
stopgap (pure WHM config, no code):

- WHM → Server Configuration → **Tweak Settings** → enable **"System disk space usage warnings."**
- WHM → **Contact Manager** → point those warnings at our existing alert email.

---

## What each item unlocks

The **one** thing that turns monitoring on is a **read-only token per server**
(section 1 + the three standard functions in section 2). With just that:

- **every site is auto-discovered and monitored**, and
- **disk + CPU alerting goes live.**

**Memory**, **mail queue**, and the **security signals** (Imunify / ModSecurity /
cPHulk) each switch on the moment you confirm their function name — no code
change on our side, just a config value.

## Reference (WHM API docs)

- Show Current Disk Usage: https://docs.cpanel.net/whm/system-health/show-current-disk-usage/
- `getdiskusage`: https://api.docs.cpanel.net/specifications/whm.openapi/bandwidth-and-disk-quotas/diskusage-get_disk_usage
- API Token Management (scoped ACL tokens): https://api.docs.cpanel.net/specifications/whm.openapi/api-token-management
