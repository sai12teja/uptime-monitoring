#!/bin/bash
# Rovix security push agent — runs ON the WHM/cPanel server (needs root, which
# you already have). Reads Imunify360's malware count locally and POSTs it to
# Rovix's /whm-security endpoint. Rovix never gets any WHM credential — the
# server reports its own security status.
#
# Install:
#   1) copy this file to the server, e.g. /root/rovix_security_push.sh
#   2) edit ROVIX_URL and API_KEY below
#   3) chmod +x /root/rovix_security_push.sh
#   4) add to root's crontab (every 5 min):
#        */5 * * * * /root/rovix_security_push.sh >/dev/null 2>&1
#
# Rovix side already understands this payload (POST /whm-security):
#   {"host": "<server>", "counts": {"imunify": <n>, "cphulk": <n>, "modsec": <n>}}
# Send whichever sources you can read; omitted ones are left untouched.

set -euo pipefail

ROVIX_URL="https://YOUR_ROVIX_HOST/whm-security"   # <-- set to your Rovix URL + /whm-security
API_KEY="YOUR_API_KEY"                              # <-- must equal Rovix's API_KEY env var
HOST="$(hostname)"

# --- Imunify360 malware: number of malicious files currently detected ---
# The agent prints JSON; count the items. Any error -> 0 (fail safe, not a
# false alarm). ImunifyAV (not 360) uses `imunify-antivirus` instead.
imunify_count=0
if command -v imunify360-agent >/dev/null 2>&1; then
  imunify_count="$(imunify360-agent malware malicious list --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    items = d.get("items") if isinstance(d, dict) else d
    print(len(items) if isinstance(items, list) else int((d or {}).get("count", 0)))
except Exception:
    print(0)
' 2>/dev/null || echo 0)"
fi

payload="$(python3 -c '
import json, sys
print(json.dumps({"host": sys.argv[1], "counts": {"imunify": int(sys.argv[2])}}))
' "$HOST" "$imunify_count")"

curl -fsS -m 20 -X POST "$ROVIX_URL" \
  -H "X-API-Key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "$payload" >/dev/null && echo "pushed imunify=${imunify_count} for ${HOST}"
