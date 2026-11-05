"""WHM site auto-discovery: parse accounts, create a monitor per domain
(idempotent, skips suspended + already-monitored, never deletes), gated and
per-server isolated. Mocked WHM responses -- no real network.

Run: ./venv/Scripts/python.exe test_whm_discovery.py
"""
import os
from unittest.mock import patch

import db
import whm_discovery
import whm_health

db.DB_PATH = "test_whm_discovery.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

LISTACCTS = {"data": {"acct": [
    {"domain": "alpha.com", "suspended": 0},
    {"domain": "beta.com", "suspended": "0"},
    {"domain": "suspended-site.com", "suspended": 1},  # skipped
]}}


# --- discover_domains: parse + skip suspended ---
with patch("whm_discovery.whm_health._api", return_value=LISTACCTS):
    assert whm_discovery.discover_domains("h", "t") == ["alpha.com", "beta.com"], \
        whm_discovery.discover_domains("h", "t")
# single-account dict (not a list) is tolerated
with patch("whm_discovery.whm_health._api", return_value={"data": {"acct": {"domain": "solo.com", "suspended": 0}}}):
    assert whm_discovery.discover_domains("h", "t") == ["solo.com"]
print("discover_domains parsing checks passed.")


# --- ensure_monitors: creates new, skips existing, idempotent ---
db.add_monitor("beta.com", "https://beta.com", "website")  # pre-existing
created = whm_discovery.ensure_monitors(["alpha.com", "beta.com"])
assert created == 1, created  # only alpha.com is new
urls = {m["url"] for m in db.list_monitors()}
assert "https://alpha.com" in urls and "https://beta.com" in urls
# running again adds nothing (idempotent)
assert whm_discovery.ensure_monitors(["alpha.com", "beta.com"]) == 0
print("ensure_monitors idempotency checks passed.")


# --- discover_all: gated, per-server, dormant without servers ---
with patch.dict(os.environ, {"WHM_SERVERS": ""}, clear=False):
    whm_discovery.discover_all()  # no servers -> no-op, no crash

with patch.dict(os.environ, {"WHM_SERVERS": "srv.example:tok"}), \
     patch("whm_discovery.whm_health._api", return_value={"data": {"acct": [{"domain": "gamma.com", "suspended": 0}]}}):
    whm_discovery.discover_all()
    assert "https://gamma.com" in {m["url"] for m in db.list_monitors()}
    # gated: an immediate second pass with a NEW domain must NOT run yet
    with patch("whm_discovery.whm_health._api",
               return_value={"data": {"acct": [{"domain": "delta.com", "suspended": 0}]}}):
        whm_discovery.discover_all()
    assert "https://delta.com" not in {m["url"] for m in db.list_monitors()}, "discovery must be interval-gated"
print("discover_all gating + wiring checks passed.")

os.remove(db.DB_PATH)
print("All whm_discovery checks passed.")
