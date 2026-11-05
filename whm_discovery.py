"""Auto-discover the websites hosted on each configured WHM server and ensure
an uptime monitor exists for each -- so "all my WHM sites" are watched without
adding them by hand (the core need: many sites, monitored automatically).

Reuses whm_health's WHM API client and the existing monitor engine
(db.add_monitor); every discovered site then flows through the SAME uptime +
SSL + alerting machinery as a hand-added one. Idempotent: never duplicates an
existing monitor, and never deletes one (a removed account's monitor is left
for a human to retire). Dormant until WHM_SERVERS is configured.
"""
import os
import time

import db
import whm_health

DISCOVERY_INTERVAL_SEC = whm_health._int_env("WHM_DISCOVERY_INTERVAL_SEC", 3600)
# WHM function listing accounts/domains. listaccts = primary domains (clean v1);
# addon/parked/sub domains are a fast-follow via a different function.
DISCOVERY_FUNCTION = os.environ.get("WHM_DISCOVERY_FUNCTION", "listaccts")


def discover_domains(host, token):
    """Primary domains on a WHM server, skipping suspended accounts (don't
    monitor a site that's intentionally offline). Tolerates the API returning
    one account as a dict or many as a list."""
    accts = whm_health._api(host, token, DISCOVERY_FUNCTION).get("data", {}).get("acct", [])
    if isinstance(accts, dict):
        accts = [accts]
    domains = []
    for a in accts if isinstance(accts, list) else []:
        if not isinstance(a, dict):
            continue
        domain = (a.get("domain") or "").strip()
        suspended = str(a.get("suspended", 0)).lower() in ("1", "true")
        if domain and not suspended:
            domains.append(domain)
    return domains


def _existing_urls():
    """Every monitored URL (active or not), normalised, so discovery never
    re-adds a site a human deactivated or that already exists."""
    with db._conn() as conn:
        rows = conn.execute("SELECT url FROM monitors").fetchall()
    return {(r["url"] or "").rstrip("/").lower() for r in rows}


def ensure_monitors(domains):
    """Create a website monitor (https://<domain>) for any domain not already
    monitored. Returns the count created. Idempotent by URL."""
    existing = _existing_urls()
    created = 0
    for domain in domains:
        url = f"https://{domain}"
        if url.rstrip("/").lower() in existing:
            continue
        db.add_monitor(domain, url, "website")
        existing.add(url.rstrip("/").lower())
        created += 1
    return created


def discover_all():
    """One discovery pass across every configured WHM server, gated to
    DISCOVERY_INTERVAL_SEC. Guarded per server so one failure can't stop the
    others or the scheduler. Dormant (no-op) when no servers are configured."""
    now = time.time()
    if not whm_health.servers():
        return
    gate = db.get_metric_state("whm:_discovery")
    if gate is not None and now - gate["last_read_ts"] < DISCOVERY_INTERVAL_SEC:
        return
    for host, token in whm_health.servers():
        try:
            created = ensure_monitors(discover_domains(host, token))
            if created:
                print(f"[whm_discovery] {host}: added {created} new site monitor(s)")
        except Exception as e:
            print(f"[whm_discovery] {host} discovery failed: {e}")
    db.set_metric_state("whm:_discovery", "ok", None, 0, None, now)
