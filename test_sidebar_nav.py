"""TDD self-check for the sidebar-navigation redesign (Vapi-inspired): a
persistent left sidebar switches which page section is visible (Monitors /
Incidents / Server Health) via a Store + CSS class toggle -- not a real
Dash multi-page rewrite, so every existing callback keeps working unchanged
since all page content stays mounted in the DOM, just hidden/shown.

Run: ./venv/Scripts/python.exe test_sidebar_nav.py
"""
from app import _nav_item_class, _page_wrapper_class, _page_title, PAGES

assert PAGES == ["monitors", "incidents", "server-health"], PAGES

# --- _nav_item_class: highlights the active sidebar nav item ---
assert _nav_item_class("monitors", "monitors") == "sidebar-nav-item active"
assert _nav_item_class("incidents", "monitors") == "sidebar-nav-item"
assert _nav_item_class("server-health", "server-health") == "sidebar-nav-item active"

# --- _page_wrapper_class: shows the active page's content, hides the rest ---
assert _page_wrapper_class("monitors", "monitors") == "page-section"
assert _page_wrapper_class("monitors", "incidents") == "page-section hidden"
assert _page_wrapper_class("server-health", "server-health") == "page-section"
assert _page_wrapper_class("server-health", "monitors") == "page-section hidden"

# --- _page_title: real page name shown in the content header ---
assert _page_title("monitors") == "Monitors"
assert _page_title("incidents") == "Incidents"
assert _page_title("server-health") == "Server Health"
assert _page_title("nonsense") == "Monitors"  # unknown/stale value falls back safely

print("All sidebar-nav checks passed.")
