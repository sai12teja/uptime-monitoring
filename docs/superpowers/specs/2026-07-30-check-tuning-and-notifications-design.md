# Per-monitor check tuning + notification overhaul (Kuma feature-parity items)

**Date:** 2026-07-30
**Status:** approved (brainstorming), not yet implemented

## Motivation

User compared Rovix against the real Uptime Kuma app and asked for a specific set of gaps to close: per-monitor Retries, per-monitor Request Timeout, HTTP method/body/encoding options, "Resend Notification if Down X times," and multi-channel notifications (Email + WhatsApp).

## Scope decision

Split into two stages (same reasoning as the DNS/TCP/push work): Stage 1 is self-contained schema/check-logic extension with no external dependencies. Stage 2 depends on a real WhatsApp Business API account (confirmed: user has a Meta Cloud API account, but not the exact approved template's parameter structure) — built against a reasonable default assumption, adjustable via env var once the real template details are known.

Two design decisions made explicitly with the user (both "recommended" options taken):
1. Recovery stays debounced at 2 consecutive successes (not matching Kuma's instant-recovery-on-1-success) — only the fail-threshold ("Retries") becomes configurable. This preserves an intentional PRD §10 decision from early in the project rather than silently reopening it to match another tool.
2. WhatsApp implemented against Meta's Cloud API (the account the user has), not Twilio.

## Stage 1: Per-monitor check tuning

**Schema** — new nullable columns on `monitors` (NULL = fall back to today's global defaults; existing monitors unaffected):
- `retries INTEGER` — matches Kuma's field name/semantics exactly: 0 = down immediately on first fail, N = N extra retries allowed first. Maps to `fail_threshold = (retries if retries is not None else 2) + 1` when calling `evaluate_status` (default `retries=2` reproduces today's global `fail_threshold=3`).
- `timeout_sec INTEGER` — overrides the global `CHECK_TIMEOUT_SEC` (10s) for this monitor only. Threaded into `do_http_check`/`do_tcp_check`'s `timeout=` params.
- `http_method TEXT DEFAULT 'GET'`, `http_body TEXT`, `http_body_encoding TEXT DEFAULT 'json'` — only meaningful for website/CRM; ignored by tcp/dns/push.

**`do_http_check`**: uses `urllib.request.Request(url, method=monitor["http_method"] or "GET", ...)` (stdlib already supports custom methods). For non-GET methods with a body set, encodes per `http_body_encoding`:
- `json` → `json.dumps(body).encode()`, `Content-Type: application/json`
- `form` → `urllib.parse.urlencode(...)` (body stored as `key=value` lines, parsed into a dict first), `Content-Type: application/x-www-form-urlencoded`
- `text` → raw `body.encode()`, `Content-Type: text/plain`

**Frontend**: Add Monitor modal gains Retries + Timeout fields for active check types; Method dropdown (GET/POST/PUT/PATCH/DELETE) + Body textarea + Body Encoding dropdown, shown only for Website/CRM.

**REST API**: `POST`/`PUT /monitors` accept `retries`, `timeout_sec`, `http_method`, `http_body`, `http_body_encoding`.

No changes to `evaluate_status` itself (already generalized for push in the prior gap) or the incident/email pipeline.

## Stage 2: Resend-if-down-X + multi-channel notifications

**Resend Notification if Down X times consecutively**: new column `monitors.resend_every INTEGER DEFAULT 0` (0 = disabled, matching Kuma's default). In `_check_one`'s "still down, not a fresh transition" branch (currently logs a `check_failure` event and returns `None`), when `resend_every > 0` and the number of fails since the incident opened is a multiple of `resend_every`, return a new `"still_down"` event instead of `None`. `handle_tick_events` gets a branch for this kind, building a `[STILL DOWN]` reminder email via a new `email_alerts.format_still_down(name, detail, downtime_so_far)`.

**Notification channels**: new column `monitors.notify_channels TEXT DEFAULT 'email'` (comma-separated: `"email"`, `"whatsapp"`, or `"email,whatsapp"`). New `notifications.py` — a thin dispatcher:
```python
def send(monitor, subject, body):
    channels = (monitor["notify_channels"] if monitor else "email").split(",")
    if "email" in channels:
        email_alerts.send(subject, body)
    if "whatsapp" in channels:
        whatsapp_alerts.send(subject, body)
```
Every existing `email_alerts.send(subject, body)` call site becomes `notifications.send(monitor, subject, body)`. Call sites that already have the `monitor` dict in scope (`ssl_check.py`'s `check_ssl_for_monitor`, `_check_one`'s branches) pass it directly. `handle_tick_events`' per-event loop doesn't carry the monitor row in its event tuple (only `incident_id`) — rather than widening that tuple (touching every existing test), it looks up `db.get_monitor(db.get_incident(incident_id)["monitor_id"])` right before sending (a cheap extra lookup, not a hot path). Server-level alerts (correlated outage, server health — no `monitors` row, `monitor_id` is NULL) always pass `monitor=None` → email-only, unchanged from today.

**`whatsapp_alerts.py`** (new, mirrors `email_alerts.py`'s exact shape — same log-and-skip-if-unconfigured pattern, same swallow-and-log-on-failure pattern): POSTs to Meta's Graph API (`https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages`) via stdlib `urllib.request` (no SDK, no new dependency — it's plain REST/JSON over HTTPS), `Authorization: Bearer {WHATSAPP_ACCESS_TOKEN}`, sending a template message:
```json
{"messaging_product": "whatsapp", "to": "<WHATSAPP_TO_NUMBER>", "type": "template",
 "template": {"name": "<WHATSAPP_TEMPLATE_NAME>", "language": {"code": "<WHATSAPP_TEMPLATE_LANG, default en_US>"},
              "components": [{"type": "body", "parameters": [{"type": "text", "text": subject}, {"type": "text", "text": body}]}]}}
```
**Known placeholder, not a finished integration**: the 2-parameter (subject, body) template shape is a guess, since the user's actual approved template's parameter count/order wasn't available. This *will* need adjusting to match the real template once known — flagged here so it isn't mistaken for a verified-correct integration. Env vars: `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_TEMPLATE_NAME`, `WHATSAPP_TEMPLATE_LANG` (default `en_US`), `WHATSAPP_TO_NUMBER`.

**Frontend**: Add Monitor modal gains a Resend-every-N-fails field (active check types) and an Email/WhatsApp checkbox pair (Email checked by default, matching today's universal behavior).

## Testing

Stage 1: real TDD, same pattern as `do_tcp_check`/`do_dns_check` — real local HTTP server (Python's own `http.server` or a quick socket-based stub) receiving a POST with a body, asserting the method/body/encoding sent were correct.

Stage 2 resend logic: TDD against the state machine, same pattern as `test_push_monitors.py`. WhatsApp: since there's no real Meta account to hit in an automated test, `whatsapp_alerts.send`'s HTTP call gets mocked in tests (same as `email_alerts.send` is mocked everywhere it's tested) — genuine live verification of an actual WhatsApp message arriving isn't possible without the user's real credentials, so this piece is verified structurally (correct payload shape, correct env-var handling, correct log-and-skip-if-unconfigured), not against the real API.

## What does NOT change

`evaluate_status`'s core mechanism (already generalized), the DNS/TCP/push check logic, the incident data model, and the correlated-outage consolidation logic (still runs before per-event emails, unaffected by the channel dispatch change).
