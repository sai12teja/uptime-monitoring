"""#13: the detail view's "30-day uptime" spans 30 days of checks, not the last
10 rows (it was computed from the last 10 and mislabelled).

Run: ./venv/Scripts/python.exe test_uptime_30d.py
"""
import os
import time

import data
import db

db.DB_PATH = "test_uptime_30d.db"
if os.path.exists(db.DB_PATH):
    os.remove(db.DB_PATH)
db.init_db()

mid = db.add_monitor("site", "https://site.example", "website")
now = time.time()

# 20 checks inside the window: 10 older fails, 10 newer oks. The last-10 view
# would read 100% up; the real 30-day window is 50%.
for i in range(10):
    db.record_check(mid, now - 1000 + i, False, 100, "fail")
for i in range(10):
    db.record_check(mid, now - 100 + i, True, 100, "ok")
# A check older than 30 days must be excluded.
db.record_check(mid, now - 40 * 86400, True, 100, "ok")

detail = data.get_target_detail(mid)
assert detail["stats"]["uptime_30d"] == "50.00%", detail["stats"]["uptime_30d"]

total, ok = db.uptime_since(mid, now - 30 * 86400)
assert (total, ok) == (20, 10), (total, ok)  # the 40-day-old check is excluded

os.remove(db.DB_PATH)
print("uptime-30d checks passed.")
