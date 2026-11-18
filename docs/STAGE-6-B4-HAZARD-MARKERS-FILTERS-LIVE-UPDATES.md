# Stage 6 – B4: Hazard Markers, Filters, and Live Updates (Issue #13)

## Overview

This document summarizes the implementation and verification of **Issue #13 (`B4 · Hazard markers, filters and live updates`)** on top of the approved B1 design system, B2 landing/auth pages, B3 interactive map/location workspace, and A8 hazard reporting/streaming backend.

All changes strictly follow the locked SafeRoute stack (FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind CSS CDN + Leaflet + `leaflet.markercluster@1.5.3` + OpenStreetMap) and introduce **zero database schema changes**, **zero backend contract breaking changes**, and **zero modifications** to B3's location-privacy module (`app/static/js/map.js`).

---

## Files Added and Updated

- [app/static/js/map_hazards.js](../app/static/js/map_hazards.js) — Dedicated B4 map hazard controller implementing debounced bounds fetching, severity-coded markers and clustering, safe DOM popups with lazy signed photo thumbnails, multi-select filters, Option R1 SSE live updates with reconnect backoff, and synchronized side-list state (`srHazardsPanel` Alpine component).
- [app/templates/map.html](../app/templates/map.html) — Loads `leaflet.markercluster@1.5.3` CSS/JS and `/static/js/map_hazards.js`; replaces the B3 placeholder card with the `#sr-hazards-panel` UI (live updates badge, hazard count, category filter chips, status chips + `Show fixed` toggle, Reset action, loading/error-with-Retry/empty states, and synchronized interactive report list); auto-collapses the mobile bottom sheet when a user taps a list item.
- [app/static/css/saferoute.css](../app/static/css/saferoute.css) — Adds scoped B4 marker, cluster badge, popup card, thumbnail/placeholder, and high-contrast `.leaflet-container a.sr-popup-cta` styles while preserving `color-scheme: only light;` and the mint contrast rule.
- [tests/test_stage6_b4_map_hazards.py](../tests/test_stage6_b4_map_hazards.py) — Automated tests for B4 template/asset wiring, auth/bounds rejection, Option R1 SSE response headers and payload redaction, bounds API filter calls, and static JS security/privacy/design guardrails.
- [docs/STAGE-6-B4-HAZARD-MARKERS-FILTERS-LIVE-UPDATES.md](./STAGE-6-B4-HAZARD-MARKERS-FILTERS-LIVE-UPDATES.md) — This documentation file.

---

## Implemented Capabilities

### 1. Debounced Bounds Loading & Stale-Response Protection
- Loads visible hazards automatically on initial map mount using `GET /api/reports?bounds=min_lat,min_lng,max_lat,max_lng`.
- Re-queries on Leaflet `moveend` with a **400 ms debounce** (`boundsDebounceMs` sourced from `#sr-map-config`).
- Deduplicates identical in-flight/repeated query keys and protects against out-of-order responses using both `AbortController` and a monotonic request sequence counter (`fetchSeq`).
- Handles network/server errors gracefully with an accessible `role="alert"` panel and a working **Retry** button (never exposing raw stack traces).

### 2. Severity-Coded Markers & Marker Clustering
- Uses `L.markerClusterGroup` (`leaflet.markercluster@1.5.3`) with graceful fallback to `L.layerGroup` if the clustering plugin is unavailable.
- Individual markers render color-coded pins using the exact Spec Section 5 palette paired with visible text labels (`L`, `M`, `H` on pins plus full `Low`, `Medium`, `High` text badges in popups and the list):
  - **Low**: `#2E9E5B`
  - **Medium**: `#F5A524`
  - **High**: `#D92D20`
- Cluster badges summarize the count and reflect the highest severity band contained inside the cluster.
- Tracks markers in a single `markersById` map keyed by report UUID so repeated loads or SSE events never produce duplicate markers.

### 3. Safe DOM Popups & Lazy Signed Thumbnails
- Popups are constructed exclusively with safe DOM APIs (`document.createElement` + `.textContent`; zero `innerHTML`) to prevent XSS from untrusted report descriptions.
- Displays:
  - Hazard photo thumbnail (validated `http://`, `https://`, or relative `/` URL only; lazy-loaded via `GET /reports/{id}` on `popupopen` and cached in memory; never exposes raw `storage_path`), falling back cleanly to a `"No photo available"` placeholder when missing or broken.
  - Category, severity band + numeric score, confidence badge, escalation/fixed status badge, freshness text (e.g., `"2 days ago"`), and description snippet.
  - Stale warning (`"Stale – may no longer be accurate"`) when an unfixed report has been inactive for **30+ days**.
  - High-contrast `"View details"` link pointing to `/reports/{id}`.

### 4. Multi-Select Filters & Synchronized Side List
- **Category filters** (multi-select): `Pothole`, `Road break`, `Streetlight`, `Footpath`, `Other`.
- **Status filters**: `Open`, `Escalated` (both active by default), plus a **`Show fixed`** toggle (default OFF so fixed hazards are excluded from the default map and list).
- **Reset** button restores defaults cleanly.
- Single-value filters are forwarded to `GET /api/reports?bounds=...` (`category`, `status`); multi-select combinations are filtered consistently across both map markers and the side list without requiring changes to A8's API contract.
- Filter selections persist across map panning and zooming (`moveend`).
- Selecting a side-list item pans/zooms via `zoomToShowLayer` (unclustering if needed), highlights the active marker, opens its popup, and automatically collapses the mobile bottom sheet on small viewports.

### 5. Option R1 Server-Side SSE Live Updates
- Connects exclusively to FastAPI's authenticated SSE bridge via `new window.EventSource("/api/reports/stream")`.
- Handles:
  - `connected`: marks live badge `Live` and refreshes current bounds after a reconnect.
  - `hazard_change`: processes `INSERT`, `UPDATE`, and `DELETE` events in place, deduplicating by report ID, ignoring out-of-bounds insertions, updating status/severity live, and removing reports that transition to `fixed` when `Show fixed` is OFF.
  - `reconnect` and stream errors: marks live badge `Reconnecting` and retries with bounded exponential backoff (`3s` up to `30s`).
- Closes the `EventSource`, cancels pending timers, and aborts in-flight fetches on `pagehide` and `beforeunload`.

---

## Security & Privacy Verification

- **No Direct Browser Supabase Realtime**: `map_hazards.js` contains zero references to `/realtime/v1`, `wss://`, `@supabase/supabase-js`, `SUPABASE_SERVICE_KEY`, `sr_access_token`, or `sr_refresh_token`.
- **Physical Privacy Isolation**: B3's [app/static/js/map.js](../app/static/js/map.js) remains untouched (zero `fetch`/`EventSource`); `map_hazards.js` never accesses browser geolocation APIs or client storage (`localStorage`/`sessionStorage`).
- **XSS Safety**: All untrusted text in popups and list items is rendered with `.textContent` / Alpine `x-text` only (verified live with an `<img src=x onerror=...>` payload).
- **Access Control**: Unauthenticated requests to `/api/reports` and `/api/reports/stream` remain rejected (`401 Unauthorized`).

---

## Verification Summary

### Automated Pytest Suite
- Command: `python -m pytest tests/ -v`
- Result: **114 passed, 2 skipped, 0 failed**.
- The 2 skipped tests are pre-existing PostgreSQL integration tests in [tests/test_stage4_pg_integration.py](../tests/test_stage4_pg_integration.py) that require `SAFEROUTE_TEST_PG_DSN`.

### Live Headless Chrome CDP Verification (Mocked Backend Endpoints)
Executed against the real FastAPI app served locally via Uvicorn with mocked auth, `/api/reports`, `/reports/{id}`, and a live async SSE queue on `/api/reports/stream`:
- **Initial bounds load**: 3 default hazards loaded (`fixed` excluded), `isClustered: true`, live badge `connected`, `bounds=` query parameter verified.
- **Severity & clustering**: Cluster badge rendered at zoom 10; individual `H`/`M`/`L` severity pins rendered at zoom 16.
- **Multi-select category & status filters**: Category multi-select, status filtering, `Show fixed` toggle, and `Reset` synchronized map markers and side list.
- **Debounce & stale-response protection**: 3 rapid pans triggered 1 debounced `/api/reports` request; delayed slow response did not overwrite a newer response.
- **Popups, XSS, thumbnails & stale warnings**: XSS payload escaped (`xssTriggered: false`), lazy thumbnail loaded (`naturalWidth > 0`), missing-photo placeholder rendered, stale warning displayed for 42-day-old report, and `"View details"` linked to `/reports/{id}`.
- **Live SSE updates & reconnect**: Live `INSERT`, duplicate `INSERT` deduplication, out-of-bounds ignore, `UPDATE` to `escalated`, `UPDATE` to `fixed` removal/re-display, `DELETE`, and `reconnect` recovery verified with `0` console errors and `0` uncaught JS exceptions.
- **Error state & Retry**: Simulated `503` surfaced user-friendly error alert and recovered cleanly on clicking `Retry`.
- **Responsive visual checks (`1280px`, `768px`, `375px`)**: Zero horizontal overflow across all three viewports; mobile bottom sheet auto-collapsed when selecting a list item. Visual screenshot review caught and fixed two real layout/contrast issues before commit:
  1. Leaflet's `.leaflet-container a` rule overrode Tailwind's white text on the popup `"View details"` CTA — fixed via `.leaflet-container a.sr-popup-cta` in `saferoute.css`.
  2. `flex-1 min-h-0` on `#sr-hazards-panel` caused the sidebar privacy footer to overlap the hazard list — fixed by switching both blocks to `shrink-0`.

### What Was Mocked vs. Live Integration Still Required
- **Mocked locally**: Authentication session cookies, `/api/reports`, `/reports/{id}` signed URLs, and `/api/reports/stream` SSE event delivery were verified using mocked FastAPI/Uvicorn handlers.
- **Live Supabase verification recommended after merge/deployment**:
  1. Confirm live `reports_in_bounds` PostGIS RPC performance and RLS enforcement with real Supabase credentials.
  2. Confirm real Supabase Storage signed URLs render inside marker popups and respect expiry/fallback.
  3. Confirm end-to-end live Supabase Realtime PostgreSQL changes flow through Option R1 `/api/reports/stream` between two real browser sessions.
