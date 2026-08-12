"""TDD self-check for site logos (favicons) on monitor cards.

The browser loads each site's own /favicon.ico directly -- no third-party
favicon service (which would leak every monitored domain to it) and no
backend fetching. A deterministic colored letter badge sits behind the
image as the fallback for sites that have no favicon.

Run: ./venv/Scripts/python.exe test_card_favicon.py
"""
import os

import db

db.DB_PATH = "test_card_favicon.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

import data
from app import _favicon_url, _badge_letter, _badge_hue, _group_favicon_target

# --- _favicon_url: always the host's own /favicon.ico, path/query stripped ---
assert _favicon_url("https://example.com") == "https://example.com/favicon.ico"
assert _favicon_url("https://example.com/about.php") == "https://example.com/favicon.ico"
assert _favicon_url("https://example.com/a/b?c=d#e") == "https://example.com/favicon.ico"
assert _favicon_url("http://example.com") == "http://example.com/favicon.ico"  # scheme preserved

# TCP/DNS monitors store a BARE hostname (no scheme) -- still resolvable to a
# favicon, just assume https since that's what a browser would try.
assert _favicon_url("example.com") == "https://example.com/favicon.ico"

# Push monitors have no target at all -> no favicon, caller shows the badge.
assert _favicon_url("") is None
assert _favicon_url(None) is None

# --- _badge_letter: first alphanumeric char, uppercased; never crashes ---
assert _badge_letter("vrittispace") == "V"
assert _badge_letter("the brand chimp") == "T"
assert _badge_letter("  spaced") == "S"
assert _badge_letter("123abc") == "1"
assert _badge_letter("/leading-slash") == "L"  # skips punctuation to the first real char
assert _badge_letter("") == "?"
assert _badge_letter(None) == "?"

# --- _badge_hue: deterministic per name, so a site's badge colour never
# changes between renders, and stays in range for hsl() ---
assert _badge_hue("vrittispace") == _badge_hue("vrittispace")
assert _badge_hue("a") != _badge_hue("b")  # different names -> different hues
for name in ["", "a", "vrittispace", "the brand chimp", "zzz"]:
    assert 0 <= _badge_hue(name) < 360, name

# --- _group_favicon_target: picks the member whose url can actually yield a
# favicon, so a group led by a Push monitor (url="") still shows the site's
# logo from its Website sibling instead of falling back to a letter ---
push = {"type": "push", "url": ""}
website = {"type": "website", "url": "https://example.com"}
assert _group_favicon_target([push, website]) is website
assert _group_favicon_target([website, push]) is website
assert _group_favicon_target([push]) is None

# --- end to end against a real row: url must reach the UI layer ---
mid = data.add_target("brand site", "https://brand.example", "website")
target = {t["id"]: t for t in data.get_targets()}[mid]
assert target["url"] == "https://brand.example", target
assert _favicon_url(target["url"]) == "https://brand.example/favicon.ico"

os.remove(db.DB_PATH)
print("All card-favicon checks passed.")
