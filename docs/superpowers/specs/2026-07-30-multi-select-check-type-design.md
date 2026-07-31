# Multi-select check type in the Add Monitor modal

**Date:** 2026-07-30
**Status:** approved (brainstorming), not yet implemented

## Motivation

User asked for the "Check type" field in the Add Monitor modal to support selecting multiple types at once (Website/CRM/TCP Port/DNS/Push-Passive), rather than only one.

## Scope decision

Two readings of "multiple types for one entry" were considered:
1. One monitor, checked multiple ways simultaneously, no single status rollup (each check-type tracked fully independently).
2. Selecting multiple types creates one separate monitor entry per type (same target), each checked exactly as today.

Option 1 would require a new `monitor_checks` child table (moving the state-machine fields off `monitors`), a scheduler rewrite (per check-type ticking instead of per-monitor), incident model extension, and a redesigned target card/detail panel showing multiple independent statuses per monitor — a rebuild bigger than any single gap built in this project so far.

**User chose option 2** after seeing the real cost of option 1. This spec covers option 2 only.

## Design

**Modal**: `dcc.RadioItems` (id=`add-monitor-type`) becomes `dcc.Checklist`, same 5 options. The existing `toggle_type_fields` callback (which shows/hides Target, Port, Expected-value, Interval based on the selected type) keys off "is this type among the checked values" instead of "is this the single selected value" — e.g. the Port field shows if `"tcp" in selected`.

**Push exclusivity**: Push/Passive's check-in model (passively waiting to be pinged) has no sensible combination with active checks (HTTP/TCP/DNS all reach out on a schedule). Enforced at submit time via the existing error-message `Div`, not via interactive auto-uncheck logic — if `"push"` is in the selected list alongside anything else, submit fails with "Push/Passive can't be combined with other check types."

**Submit**: loops over the selected types, calling the existing `data.add_target(...)` once per type — no new backend function. Each call passes the *same* shared form values (name, url, keyword, port, interval) from the modal, varying only `target_type`; the loop doesn't invent per-type field values. Each iteration creates one independent monitor row, checked and tracked exactly as any single-type monitor is today (same state machine, same incident model, same REST API, all unchanged).

**Known limitation**: "Expected value" is one shared field, but it means different things per type (an expected keyword for Website/CRM, an expected IP for DNS). Selecting, say, Website + DNS together and filling in a value only sensible for one of them applies it to both. Not solved here — narrowing this would mean a per-type expected-value field, which is more UI complexity for a narrow combination the user is unlikely to actually hit (the common case is selecting types like TCP + DNS that don't both want text in that field meaningfully, or a single type, which is unaffected).

**Naming**: when more than one type is selected, each created monitor's name is suffixed with its type in parentheses (e.g. "Acme Server (TCP)", "Acme Server (DNS)") so the grid and incident table can tell them apart. Single-type selection (today's common case) keeps the name unsuffixed, unchanged from current behavior.

**Validation**: existing per-type rules (URL required unless push, port required for tcp, interval required for push, http(s) scheme required for website/crm) run once per selected type in the submit loop — if any selected type fails validation, nothing is created and the existing error area shows the first failure.

## What does NOT change

`db.py` schema, `monitor_engine.py` scheduler/state-machine/dispatch, `api.py` core logic, the incident model, and `data.add_target`'s signature are all untouched. This is a UI-layer-only change confined to `app.py`.

## Testing

This is Dash callback code — per this project's established convention (documented in memory), Dash UI changes are verified via live browser testing (the `browse` skill) rather than callback unit tests, since Dash callbacks aren't practically unit-testable without the whole running app. The submit loop calls into `data.add_target`, which is already covered by existing tests (`test_crm_keyword.py`, `test_api.py`, `test_monitor_types.py`) — no new backend logic needs new tests.
