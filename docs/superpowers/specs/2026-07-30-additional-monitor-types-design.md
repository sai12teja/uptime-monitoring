# Additional monitor types: DNS, TCP port, push/passive

**Date:** 2026-07-30
**Status:** approved (brainstorming), not yet implemented

## Motivation

The PRD's original scope only covers HTTP-based checks (websites, the CRM login page) and server host health. The user wants Uptime-Kuma-style breadth added: DNS resolution checks, TCP port reachability checks, and push/passive ("dead man's switch") monitors. gRPC and MQTT were explicitly scoped out — no real infrastructure to monitor with them right now.

## Scope

**In scope:** DNS checks, TCP port checks, push/passive monitors — schema, backend check logic, scheduler integration, REST API, and the Add Monitor frontend.

**Out of scope:** gRPC monitors, MQTT monitors, public status pages, per-monitor notification channel selection (still one global email pipeline).

**Suggested implementation staging:** DNS and TCP are straightforward extensions of the existing active-check pipeline (new check function + type dispatch, nothing else changes) and can land together first. Push is architecturally distinct — new Blueprint, new route, state-machine threshold changes, the awaiting-monitor edge case — and is more naturally a second stage.

## Data model

Extend the existing `monitors` table (no new tables) with two nullable columns:

- `port INTEGER` — used by TCP checks only.
- `push_token TEXT UNIQUE` — used by push checks only; server-generated, never client-supplied.

(No `dns_record_type` column — see DNS checks below for why it isn't needed.)

Existing columns are reused rather than duplicated:
- `url` — hostname for DNS, host for TCP. Unused (NULL) for push.
- `keyword` — DNS's optional "expected resolved value" check. Exactly the same "optional expected content" semantics website/CRM already use it for.
- `interval_sec` — for push monitors, this becomes "expected check-in frequency" instead of "active poll frequency." Same column, different meaning depending on `type`.

New `type` values: `"tcp"`, `"dns"`, `"push"` (alongside existing `"website"`, `"crm"`).

## DNS checks

`do_dns_check(monitor)` (new, `monitor_engine.py`): resolves `monitor["url"]` via `socket.getaddrinfo(hostname, None)` (stdlib — no new dependency), which returns whatever A/AAAA addresses the system resolves without needing to request a specific record type. Self-review caught that an earlier draft of this spec added a `dns_record_type` column to let a record type be selected — dropped, since nothing in this design exposes a record-type picker anywhere; storing a value nobody sets is exactly the kind of speculative column this project avoids elsewhere. MX/TXT/CNAME checks are out of scope entirely (stdlib doesn't expose them without extra work, and nothing today needs them).

If `monitor["keyword"]` is set, at least one resolved address must match it exactly, else the check fails. No match required = plain resolvability check. Returns `(ok, response_ms, detail)` — same contract as `do_http_check`, so it plugs into the existing pipeline unchanged.

## TCP port checks

`do_tcp_check(monitor)` (new): opens a raw `socket` connection to `(monitor["url"], monitor["port"])` with the existing `CHECK_TIMEOUT_SEC` timeout. Success = connects. Same `(ok, response_ms, detail)` return contract.

## Dispatch

`_check_one` gains a dispatch by `monitor["type"]` to select `do_http_check` / `do_dns_check` / `do_tcp_check` (push is handled separately, see below). Everything downstream — `evaluate_status`, incident open/resolve, email alerts, `incident_events` logging — is unchanged, shared code across all check types.

## Push/passive monitors

Architecturally inverted: Rovix waits to be pinged instead of reaching out.

**Token & endpoint.** Each push monitor gets `push_token = secrets.token_hex(16)` (same stdlib approach as `API_KEY`/`SESSION_SECRET_KEY` generation) at creation. A new route, `GET/POST /push/<token>`, receives check-ins. The token in the URL is the auth — no `X-API-Key` required, matching how push monitoring works elsewhere (Healthchecks.io, Uptime Kuma) and how a plain `curl` from a cron job is expected to work.

These routes live in their own Flask Blueprint (`"push"`), separate from the existing `"api"` Blueprint. `auth.py`'s session-gate exemption (`request.blueprint == "api"`) gets a sibling exemption for `request.blueprint == "push"`. The `api` Blueprint's `X-API-Key` check is untouched and does not apply to these routes.

**Down/up detection, reusing existing machinery.** A ping = an "ok" event: `db.record_check`, reset fail counter. A *missed* ping is detected by the existing `db.get_due_monitors()` query — "hasn't been checked/pinged within its interval" already means exactly "missed its check-in" for a push monitor. `_check_one`'s `"push"` branch feeds `ok=False` into the same `evaluate_status` state machine instead of running an active check — same incident/email/timeline code path as every other type.

**Threshold generalization.** Per approved decision, push monitors alert after **1** missed check-in (not 3) and recover after **1** successful ping (not 2) — symmetric fast-alerting, since a missed/ran cron job is an unambiguous signal, unlike a network blip. `evaluate_status(status, fails, oks, ok, fail_threshold=3, ok_threshold=2)` gains two optional parameters, defaulting to the existing 3/2 (no behavior change for http/tcp/dns). Push checks call it with `fail_threshold=1, ok_threshold=1`.

**New-monitor edge case.** A push monitor with `last_checked_at IS NULL` (never pinged) must not be immediately flagged as missed on the very next scheduler tick. The `"push"` branch treats `last_checked_at IS NULL` as "still awaiting first check-in" (stays in the existing `"awaiting"` status) and skips failure evaluation entirely until the first real ping arrives.

## Frontend

**Add Monitor modal** — type dropdown gains TCP Port / DNS / Push/Passive. Fields shown are conditional on type, toggled via one new callback using the same show/hide CSS-class pattern already used for `target-filter-wrapper`:

| Type | Fields shown |
|---|---|
| Website / CRM | Target URL, Expected keyword (optional) — unchanged |
| TCP Port | Target host, Port (new) |
| DNS | Target hostname, Expected value (optional) — same input as keyword, relabeled generically |
| Push/Passive | Name, Interval only |

**Push URL discovery** — shown in the existing target detail slide-over (click the card), not a new toast/dialog. Reuses existing UI and stays available to look up again later, not just at creation.

**Target grid** — no structural change; existing status coloring (up/down/overdue/awaiting) already works generically. Card subtext becomes type-appropriate (`host:port` for TCP, hostname for DNS, "last check-in Ns ago" for push).

## REST API

`POST /monitors` accepts an optional `port` field (TCP only). `push_token` is always server-generated. `PUT /monitors/{id}` can edit `port` the same way it edits `keyword` today. Consistent with gap 5's precedent, **`type` is not editable after creation** — only its type-appropriate fields are.

## Dependencies

None added. DNS via `socket.getaddrinfo`, TCP via `socket` — both stdlib, matching this backend's existing zero-new-dependencies property.

## Testing plan

Following this project's established TDD + live-verification convention:
- `do_dns_check` / `do_tcp_check`: pure-logic tests against real local sockets / real DNS (e.g. `localhost`, a known-good and known-bad hostname), mirroring how `do_http_check` is tested.
- `evaluate_status`'s new threshold params: unit tests confirming default behavior is unchanged, plus `fail_threshold=1`/`ok_threshold=1` behavior for push.
- Push endpoint: Flask test-client tests against a bare Flask app (same pattern as `test_api.py`), covering valid token, invalid token (404), first-ping-ever, missed-ping-then-recovered.
- Live verification: a real TCP port (e.g. local port), a real resolvable/unresolvable hostname, and a real push monitor pinged via curl against the running dashboard.

## Rollout notes

Self-review caught an inaccuracy in an earlier draft here: it claimed this project has an established in-place migration pattern from "prior gaps like gap 5's `active` column." Checked the live `rovix.db` — those columns exist, but `db.py` has never contained an `ALTER TABLE` statement. They only got there because the live db was deleted and freshly recreated during each gap's live-verification cleanup step (the documented "restart clean" convention), not because `CREATE TABLE IF NOT EXISTS` retroactively adds columns — it doesn't; it's a no-op once the table exists.

That's no longer an acceptable path for this gap: the live `rovix.db` now holds real data worth keeping (an admin login account, real incident history). This gap needs its own explicit migration: `init_db()` gains guarded `ALTER TABLE monitors ADD COLUMN port INTEGER` / `ADD COLUMN push_token TEXT` statements (SQLite has no `ADD COLUMN IF NOT EXISTS`, so guard each with a `PRAGMA table_info` check or a caught `OperationalError`) so the new columns land on the existing database without dropping it. This is the first schema change in this project that actually needs a real migration step, rather than delete-and-recreate.
