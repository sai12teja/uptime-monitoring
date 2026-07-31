"""Assert-based self-check for the server-health metric math (PRD §5.4/§7.2).

Covers the two things that are easy to get quietly wrong: reading
MemAvailable rather than MemFree, and matching `df`'s use-percentage so the
§7.2 thresholds fire at the same point ops sees them.

Run: ./venv/Scripts/python.exe test_server_health.py
"""
from server_health import _parse_meminfo, _used_pct, read_all

MEMINFO = """MemTotal:       16000000 kB
MemFree:         1000000 kB
MemAvailable:    8000000 kB
Buffers:          500000 kB
Cached:          6000000 kB
SwapTotal:       2000000 kB
"""

# 16GB total, 8GB available -> 50% used. Using MemFree instead would report
# 93.75% and page someone at 3am for a box that is actually half idle.
assert _parse_meminfo(MEMINFO) == 50.0, _parse_meminfo(MEMINFO)

# Percent used matches `df`: used/(used+free), NOT used/total. With Linux
# root-reserved blocks, total > used+free, so used/total reads low and would
# trip the §7.2 disk thresholds later than df shows them.
assert _used_pct(95, 5) == 95.0
assert _used_pct(0, 100) == 0.0
assert _used_pct(50, 50) == 50.0

# Filesystems reporting no capacity (btrfs inodes) must not divide by zero.
assert _used_pct(0, 0) is None

# Every metric is either a number or an honest None — never a fake 0 standing
# in for "couldn't read it" (that renders as a healthy 0% on the dashboard).
metrics = read_all()
assert set(metrics) == {"cpu_pct", "mem_pct", "disk_pct", "inodes_pct", "services"}
for key in ("cpu_pct", "mem_pct", "disk_pct", "inodes_pct"):
    val = metrics[key]
    assert val is None or isinstance(val, float), f"{key}={val!r}"
assert isinstance(metrics["services"], dict)

print("All server_health checks passed.")
