"""TDD self-check for favicon discovery: parse <link rel="icon"> out of a
site's homepage HTML and resolve it to an absolute URL.

Needed because a plain /favicon.ico probe finds nothing on real sites --
verified against the live monitored domains, every one of which 404s at
/favicon.ico while declaring its real icon at a custom path in HTML.

Real local HTTP server, not mocks -- matches this project's convention.

Run: ./venv/Scripts/python.exe test_favicon_discovery.py
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from page_discovery import discover_favicon

STATE = {"html": b"<html><head></head><body></body></html>"}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(STATE["html"])
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # quiet test output


server = HTTPServer(("127.0.0.1", 0), _Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"

# --- relative href resolves against the site root (the real-world case:
# every monitored site declares something like href="assets/img/fav.png") ---
STATE["html"] = b'<html><head><link rel="shortcut icon" href="assets/img/favicon.png"></head></html>'
assert discover_favicon(base) == f"{base}/assets/img/favicon.png", discover_favicon(base)

# --- root-relative href ---
STATE["html"] = b'<html><head><link rel="icon" href="/static/fav.png"></head></html>'
assert discover_favicon(base) == f"{base}/static/fav.png", discover_favicon(base)

# --- absolute href on another host is kept as-is (CDN-hosted icons are
# legitimate -- this is an <img src> for the browser, not a fetch we make) ---
STATE["html"] = b'<html><head><link rel="icon" href="https://cdn.example/i.png"></head></html>'
assert discover_favicon(base) == "https://cdn.example/i.png", discover_favicon(base)

# --- rel is matched case-insensitively and within multi-value rel lists ---
STATE["html"] = b'<html><head><link REL="SHORTCUT ICON" HREF="/a.ico"></head></html>'
assert discover_favicon(base) == f"{base}/a.ico", discover_favicon(base)

# --- apple-touch-icon counts too (many sites ship only that) ---
STATE["html"] = b'<html><head><link rel="apple-touch-icon" href="/touch.png"></head></html>'
assert discover_favicon(base) == f"{base}/touch.png", discover_favicon(base)

# --- a rel that merely CONTAINS "icon" as a substring of another word must
# not match (guards a naive `"icon" in rel` check) ---
STATE["html"] = b'<html><head><link rel="iconoclast" href="/nope.png"></head></html>'
assert discover_favicon(base) is None, discover_favicon(base)

# --- no icon declared -> None, so the caller falls back to a letter badge ---
STATE["html"] = b"<html><head><title>no icon</title></head></html>"
assert discover_favicon(base) is None

# --- first declared icon wins (stable, predictable pick) ---
STATE["html"] = b'<html><head><link rel="icon" href="/one.png"><link rel="icon" href="/two.png"></head></html>'
assert discover_favicon(base) == f"{base}/one.png", discover_favicon(base)

# --- REGRESSION: a real monitor's stored url is often a specific PAGE, not
# the site root (e.g. https://site.com/contact-us/), but the icon is a
# site-wide asset declared on the homepage. Passing a page URL must still
# probe the root, not that page (which may not exist or lack the tag) --
# a real bug caught live: synergix-group.com resolved fine at its root but
# the backfill was passing per-page URLs and silently getting nothing. ---
STATE["html"] = b'<html><head><link rel="icon" href="/site-icon.png"></head></html>'
assert discover_favicon(f"{base}/some/deep/page") == f"{base}/site-icon.png", \
    discover_favicon(f"{base}/some/deep/page")

# --- unreachable host fails soft, never raises ---
assert discover_favicon("http://127.0.0.1:1") is None

# --- SSRF guard still applies (reuses monitor_engine's _is_blocked_host) ---
assert discover_favicon("http://192.168.1.1") is None

server.shutdown()
print("All favicon-discovery checks passed.")
