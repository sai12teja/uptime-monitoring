# Rovix UI Redesign — "Calm Observability"

**Date:** 2026-11-05
**Status:** Approved, implementing
**Scope:** Full in-app UI pass — all screens, modals, target detail, and login — restyle + targeted layout/UX tweaks. Dark stays default; add a polished light mode + toggle.

## Goal

Elevate the Rovix Uptime Monitoring dashboard to a premium, user-friendly UI matching the category leaders (OpenAI Platform, Supabase, Better Stack, Neon, incident.io — pulled from Mobbin). The defining move: **premium in this category = calmer and cleaner**. Retire the heavy aurora-glow + grain background for flat, crisp surfaces with clear hierarchy and generous whitespace.

## Key references (Mobbin)

- Status hero lead-in: Better Stack, OpenAI Platform service-health.
- Tidy stat-card grids + restrained charts: Supabase, Neon.
- Incident timeline/table: incident.io, Better Stack.
- Login centered card + split panel: Hotjar, Peec AI, Airtable.

## Architecture (unchanged)

Plotly Dash app. UI = Python `html`/`dcc` components styled by `assets/style.css` (Dash auto-loads `assets/`). Login/password are standalone inline-HTML Flask routes in `auth.py`. Small clientside JS in `assets/interactions.js`.

**Leverage:** component CSS already uses `var(--color-*)` tokens, so swapping the token values and adding a light set recolors nearly every component at once. This keeps the diff small — most work is tokens + background + a handful of component refinements, not a rewrite.

## 1. Dual-theme token system (`style.css`)

Replace the single dark `:root` with a token set keyed by `data-theme` on `<html>`:
- `:root` and `:root[data-theme="dark"]` → dark (default).
- `:root[data-theme="light"]` → light overrides.

| Token | Dark | Light |
|---|---|---|
| `--color-bg` | `#0b0d12` (flat) | `#f7f8fa` |
| `--color-surface` / `-2` | `#151821` / `#1c202b` | `#ffffff` / `#ffffff` |
| `--color-border` | `#262b36` | `#e5e7ec` |
| `--color-text-primary` / `-secondary` | `#e6e8eb` / `#9aa3ad` | `#1a1d24` / `#5c6470` |
| `--color-accent` | `#4d8fe0` | `#2f6fd0` |
| status up / warn / down | `#3fb27f` / `#d9a441` / `#e5484d` | `#1f9d6b` / `#b7791f` / `#d92d20` |
| shadows | tighter (less muddy) | soft `rgba(16,24,40,.06)` |

Keep IBM Plex Sans/Mono, the 4px spacing scale, and radii. Status background tints (`*-bg`) get a light-mode variant. All colors meet WCAG AA in both themes (light status colors deepened for white surfaces).

**Background:** replace the `body` triple-radial aurora + dot-grid and the `body::before` SVG grain with a flat canvas (optionally one very faint top radial at low opacity). Removes the main "busy" signal.

## 2. Theme toggle (only new mechanism)

- No-flash init: set `app.index_string` (Dash) with a blocking `<head>` script that reads `localStorage['rovix-theme']` and sets `document.documentElement.dataset.theme` before first paint. Default `dark` when unset.
- Toggle button (sun/moon) added to the topbar `.header-actions` (`id="theme-toggle"`).
- `app.clientside_callback` on the button's `n_clicks`: flip `data-theme`, persist to `localStorage`, return the opposite-mode icon label. Pure clientside, no server round-trip.

## 3. Screen-by-screen (additive markup — no existing class renames)

- **Sidebar** (`build_sidebar`): add a small inline-SVG Rovix mark by `.sidebar-brand`; each nav item gets an inline-SVG icon; refine `.sidebar-nav-item.active` to a soft accent-tinted pill + left rule. No icon-font dependency.
- **Topbar** (`build_topbar`, `build_skeleton_topbar`): add theme toggle; `.updated-at` becomes a subtle live pill with a pulsing dot.
- **Monitors / status hero** (`build_summary_or_banner`): promote `.all-clear-hero` into a persistent status hero band — big operational-state line (`● All systems operational` / `N of M up`) + supporting stat strip (down/degraded/up, incidents in window, avg fix time, this-server status) built from existing `data.py` values. The 3 summary cards restyle into a tidier stat-card grid.
  - `ponytail:` 30-day uptime sparkline **deferred** — no cheap fleet-wide daily series exists (`db.uptime_since` is per-target/per-window). Add when a roll-up query or cached daily series lands; do not fabricate a series.
- **Monitor cards** (`.target-card*`): calmer spacing, crisper status pill, gentle hover-lift.
- **Incidents** (`build_incident_table_inner`): restyle `.incident-table` — severity status dots, cleaner rows, better empty/error states. Keep the table (columns suit the data; Better Stack uses one too). Markup unchanged; CSS only.
- **Server Health**: stat tiles + restrained thin-line charts (muted gridlines, single accent) via CSS on existing panel markup.
- **Modals** (settings, add/edit) + **target detail** slide-over: unified card surface, consistent inputs/buttons, tighter field grouping; polish existing `.timeline-item` in the detail view.
- **Login + password** (`auth.py`): rework `LOGIN_PAGE` / `PASSWORD_PAGE` into a centered card + subtle split panel (brand mark + one product line), using the same token palette. **Preserve** every `name=`, `id=`, `action=`, button text, and `{placeholder}` so `auth.py`/`test_auth.py`/`test_password_change.py` flows are untouched.

## 4. Motion & accessibility

Subtle only: 120–160ms ease on hover/active, pulsing live dot, gentle card hover-lift. Keep the existing `prefers-reduced-motion` guard. `:focus-visible` rings on all interactive elements. AA contrast in both themes.

## 5. Implementation footprint

- ~90% `assets/style.css` (token sets + background + component refinements).
- Targeted additive edits in `app.py` (hero, sidebar icons, topbar toggle, `index_string`, clientside callback).
- `auth.py` (two inline `<style>` blocks + markup).
- `assets/interactions.js` only if needed beyond the clientside callback.
- **No new dependencies.** No layout framework.

## 6. Testing / risk

- Markup changes are additive (new classes), so existing class/text assertions stay green.
- Run the full `test_*.py` suite after; fix any regressions.
- Risk: low. Biggest is the theme attribute wiring — verified by loading both modes. Login markup risk mitigated by preserving all form contracts.

## Out of scope (YAGNI)

Collapsible sidebar, per-user theme persistence server-side, the deferred sparkline, chart library swaps, any backend/monitoring logic changes.
