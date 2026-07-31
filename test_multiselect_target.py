"""Assert-based self-check for _target_for_type (multi-select scheme-stripping fix).

Run: ./venv/Scripts/python.exe test_multiselect_target.py
"""
from app import _target_for_type

# Website/crm always keep the full URL as-is.
assert _target_for_type("https://example.com", "website") == "https://example.com"
assert _target_for_type("https://example.com", "crm") == "https://example.com"

# DNS/TCP strip the scheme (and any path) down to the bare hostname.
assert _target_for_type("https://example.com", "dns") == "example.com"
assert _target_for_type("https://example.com/health", "tcp") == "example.com"
assert _target_for_type("http://google.com", "dns") == "google.com"

# Already-bare hostnames pass through unchanged.
assert _target_for_type("example.com", "dns") == "example.com"
assert _target_for_type("example.com", "tcp") == "example.com"

print("All _target_for_type checks passed.")
