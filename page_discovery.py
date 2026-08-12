"""Auto-discover a site's pages for the Add Monitor "Discover pages" button
(user-requested this session, modeled after UptimeRobot's onboarding scan).

Sitemap.xml first -- most sites have one and it's usually a clean, complete
list of pages, no crawling needed. Falls back to scanning same-domain
<a href> links on the homepage when there's no sitemap. This is NOT a
recursive crawler (it never follows links found on non-homepage pages) --
that's a meaningfully bigger feature, scoped out deliberately to keep this
fast, simple, and stdlib-only (urllib + xml.etree + html.parser), matching
this project's zero-new-dependency convention.
"""
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from monitor_engine import _is_blocked_host

FETCH_TIMEOUT_SEC = 10
DEFAULT_CAP = 25
USER_AGENT = "RovixMonitor/1.0"
MAX_FETCH_BYTES = 1_000_000  # a sitemap/homepage past 1MB is pathological for this purpose


class _IconExtractor(HTMLParser):
    """Pulls the first <link rel="...icon...">'s href out of a page head.

    A plain /favicon.ico probe is not enough in practice: every one of this
    deployment's real monitored sites 404s there while declaring its actual
    icon at a custom path (e.g. href="assets/img/favicon.png")."""

    ICON_RELS = {"icon", "shortcut", "apple-touch-icon", "apple-touch-icon-precomposed",
                 "fluid-icon", "mask-icon"}

    def __init__(self):
        super().__init__()
        self.href = None

    def handle_starttag(self, tag, attrs):
        if tag != "link" or self.href:
            return  # first icon wins -- stable, predictable pick
        attrs = dict(attrs)
        rel = (attrs.get("rel") or "").lower()
        # Split on whitespace and match whole tokens, so rel="iconoclast"
        # doesn't register as an icon the way a substring check would.
        if not self.ICON_RELS & set(rel.split()):
            return
        if attrs.get("href"):
            self.href = attrs["href"].strip()


class _LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value)


def _fetch(url):
    """Returns bytes, or None on any failure (unreachable/timeout/blocked/
    non-2xx). Re-checks the SSRF guard here too since callers may build
    derived URLs (e.g. /sitemap.xml) from a base that already passed it,
    but this is the actual network call site."""
    hostname = urllib.parse.urlparse(url).hostname
    if hostname and _is_blocked_host(hostname):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            return resp.read(MAX_FETCH_BYTES)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _paths_from_sitemap(base_url, base_host):
    body = _fetch(base_url.rstrip("/") + "/sitemap.xml")
    if not body:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    paths = []
    for el in root.iter():
        if not el.tag.endswith("loc") or not el.text:
            continue
        parsed = urllib.parse.urlparse(el.text.strip())
        if parsed.hostname == base_host:
            paths.append(parsed.path or "/")
    return paths


def _paths_from_homepage(base_url, base_host):
    body = _fetch(base_url)
    if not body:
        return []
    parser = _LinkExtractor()
    parser.feed(body.decode("utf-8", errors="ignore"))
    paths = []
    for href in parser.hrefs:
        resolved = urllib.parse.urljoin(base_url + "/", href)
        parsed = urllib.parse.urlparse(resolved)
        if parsed.scheme not in ("http", "https") or parsed.hostname != base_host:
            continue
        paths.append(parsed.path or "/")
    return paths


def discover_favicon(url):
    """Absolute URL of the site's declared icon, or None if it doesn't
    declare one (caller then shows a letter badge). Fails soft on every
    error -- an unreachable site must never break rendering a card.

    Always probes the site ROOT, never whatever specific path is in `url`
    -- a monitor's stored url is often a specific page (e.g.
    https://site.com/contact-us/), but the icon is a site-wide asset
    declared on the homepage, which may not exist or lack the tag on an
    arbitrary sub-page. Caught live: a real monitored site resolved fine
    at its root but returned nothing when probed at a sub-page url.

    The returned URL is handed to the browser as an <img src>; it is not
    fetched here, so an icon hosted on a CDN/other host is fine to keep.
    The homepage fetch itself goes through _fetch, which applies the same
    SSRF guard as the real monitor checks."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    if not parsed.hostname:
        return None
    root = f"{parsed.scheme}://{parsed.netloc}"
    body = _fetch(root)
    if not body:
        return None
    parser = _IconExtractor()
    try:
        parser.feed(body.decode("utf-8", errors="ignore"))
    except Exception:
        return None  # malformed markup must not propagate
    if not parser.href:
        return None
    # urljoin needs a trailing slash on the base, or a relative href like
    # "assets/i.png" would replace the last path segment instead of
    # appending under it.
    return urllib.parse.urljoin(root + "/", parser.href)


def discover_pages(base_url, cap=DEFAULT_CAP):
    """Returns (paths: list[str], total_found: int, source: str) where
    source is "sitemap", "homepage", "none", or "blocked". `paths` is
    deduped (order preserved) and capped at `cap`; `total_found` is the
    real pre-cap unique count, so callers can say "N more not shown"."""
    base_host = urllib.parse.urlparse(base_url).hostname
    if base_host and _is_blocked_host(base_host):
        return [], 0, "blocked"

    raw = _paths_from_sitemap(base_url, base_host)
    source = "sitemap"
    if not raw:
        raw = _paths_from_homepage(base_url, base_host)
        source = "homepage"
    if not raw:
        return [], 0, "none"

    seen = set()
    deduped = []
    for path in raw:
        if path not in seen:
            seen.add(path)
            deduped.append(path)

    return deduped[:cap], len(deduped), source
