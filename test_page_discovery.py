"""TDD self-check for page_discovery.py: auto-discover a site's pages for
the Add Monitor "Discover pages" checklist (PRD-adjacent, user-requested
this session). Sitemap.xml preferred; falls back to same-domain <a href>
links on the homepage when there's no sitemap. Real local HTTP server, not
mocks -- matches this project's established testing convention.

Run: ./venv/Scripts/python.exe test_page_discovery.py
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from page_discovery import discover_pages, DEFAULT_CAP

STATE = {"sitemap": None, "homepage": b"<html><body>no links</body></html>"}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/sitemap.xml" and STATE["sitemap"] is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(STATE["sitemap"])
        elif self.path == "/sitemap.xml":
            self.send_response(404)
            self.end_headers()
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(STATE["homepage"])
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # quiet test output


server = HTTPServer(("127.0.0.1", 0), _Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{port}"

# --- sitemap present -> preferred over homepage links, same-domain only ---
STATE["sitemap"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base_url}/</loc></url>
  <url><loc>{base_url}/about</loc></url>
  <url><loc>{base_url}/pricing</loc></url>
  <url><loc>https://external.example/should-be-excluded</loc></url>
</urlset>""".encode()

paths, total, source = discover_pages(base_url)
assert source == "sitemap", source
assert total == 3, total  # external.example URL excluded
assert set(paths) == {"/", "/about", "/pricing"}, paths

# --- no sitemap -> falls back to homepage <a href> links, same-domain only,
# relative and absolute hrefs both resolved correctly ---
STATE["sitemap"] = None
STATE["homepage"] = f"""<html><body>
  <a href="/contact">Contact</a>
  <a href="/blog">Blog</a>
  <a href="{base_url}/pricing">Pricing (absolute)</a>
  <a href="https://external.example/other">External</a>
  <a href="mailto:hi@example.com">Email us</a>
</body></html>""".encode()

paths, total, source = discover_pages(base_url)
assert source == "homepage", source
assert total == 3, total
assert set(paths) == {"/contact", "/blog", "/pricing"}, paths

# --- neither sitemap nor any usable links -> "none", empty, no crash ---
STATE["homepage"] = b"<html><body>no links here</body></html>"
paths, total, source = discover_pages(base_url)
assert (paths, total, source) == ([], 0, "none"), (paths, total, source)

# --- cap at N, total_found reports the real pre-cap count ---
urls = "\n".join(f"  <url><loc>{base_url}/page-{i}</loc></url>" for i in range(30))
STATE["sitemap"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}
</urlset>""".encode()

paths, total, source = discover_pages(base_url)
assert source == "sitemap"
assert total == 30, total
assert len(paths) == DEFAULT_CAP == 25, (len(paths), DEFAULT_CAP)

# --- dedup: repeated <loc> entries collapse to one path ---
STATE["sitemap"] = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base_url}/about</loc></url>
  <url><loc>{base_url}/about</loc></url>
</urlset>""".encode()
paths, total, source = discover_pages(base_url)
assert (paths, total) == (["/about"], 1), (paths, total)

# --- SSRF guard: a private/internal target is rejected before any request
# is attempted (reuses monitor_engine's existing _is_blocked_host) ---
paths, total, source = discover_pages("http://192.168.1.1")
assert (paths, total, source) == ([], 0, "blocked"), (paths, total, source)

server.shutdown()
print("All page_discovery checks passed.")
