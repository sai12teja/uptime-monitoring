"""A displayed timestamp must be wall-clock, never time.monotonic().

Real bug this locks down: server_health cached its reading time with
time.monotonic() (right for the TTL, wrong for display) and passed that
same value to the alert email. On Linux monotonic counts from boot, so a
48-day-uptime VPS mailed "TIME 1970-02-18" instead of the actual time.
"""
import time

import email_alerts
import server_health


def test_server_alert_ts_is_wall_clock():
    server_health._cache.update(ts=0.0, wall=0.0, value=None)
    server_health.read_all()

    wall = server_health._cache["wall"]
    mono = server_health._cache["ts"]

    # The cache still tracks monotonic for the TTL...
    assert abs(mono - time.monotonic()) < 5, "TTL clock must stay monotonic"
    # ...and separately a real wall-clock time for display.
    assert abs(wall - time.time()) < 5, f"wall={wall} is not a real timestamp"

    # The value that reaches the email renders as today, not 1970.
    year = time.strftime("%Y", time.localtime(wall))
    assert year == time.strftime("%Y"), f"alert would show year {year}"


def test_formatter_renders_given_ts():
    now = time.time()
    subject, body, html = email_alerts.format_server_alert("CPU 99% load", "critical", ts=now)
    stamp = time.strftime("%Y-%m-%d", time.localtime(now))
    assert stamp in body, f"{stamp!r} missing from {body!r}"
    assert "1970" not in body


if __name__ == "__main__":
    test_server_alert_ts_is_wall_clock()
    test_formatter_renders_given_ts()
    print("PASS")
