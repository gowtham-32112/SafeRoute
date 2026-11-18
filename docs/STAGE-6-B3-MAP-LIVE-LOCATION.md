# SafeRoute — Stage 6, Issue #12 (B3 · Map Page Layout and Live Location)

**Issue**: `#12` (`B3 · Map page layout and live location`)
**Branch**: `feat/frontend-12-map-live-location`
**Scope**: Authenticated `/map` workspace with desktop left sidebar, responsive mobile/tablet collapsible bottom sheet, full-height Leaflet/OpenStreetMap map, browser live geolocation with 5-second timeout, Kukatpally Y Junction fallback, `"You are here"` marker, and `"Re-centre on me"` control.

---

## 1. Files Created / Modified

| File | Purpose |
| :--- | :--- |
| [app/main.py](../app/main.py) | Adds authenticated `GET /map` route protected by Stage 5 `get_current_user`, passing `app/config.py` map defaults into `map.html` |
| [app/templates/map.html](../app/templates/map.html) | Full-height map workspace extending `base.html` with 360 px desktop left sidebar, collapsible bottom sheet below `lg`, Leaflet map region, location status card, `"Re-centre on me"` controls, and error overlay |
| [app/static/js/map.js](../app/static/js/map.js) | Encapsulated Leaflet/OpenStreetMap initialization, browser geolocation with 5-second timeout, coordinate validation, single `"You are here"` marker management, `"Re-centre on me"` logic, and responsive `invalidateSize()` handling |
| [app/static/css/saferoute.css](../app/static/css/saferoute.css) | Scoped `.sr-map-workspace` stacking isolation (`isolation: isolate`), `.sr-user-location-marker` styling, and accessible Leaflet control polish |
| [tests/test_stage6_b3_map.py](../tests/test_stage6_b3_map.py) | Automated backend access, config parity, privacy/security, and light-theme tests |

---

## 2. Authentication & Configuration

- **Auth Protection**: `GET /map` uses `Depends(get_current_user)` from [app/deps.py](../app/deps.py).
  - Unauthenticated browser HTML requests receive `303 See Other` -> `/login`.
  - Unauthenticated JSON/API requests receive `401 Unauthorized`.
  - Authenticated users missing a valid `public.profiles` record receive `403 Forbidden`.
  - Admin vs ordinary user navbar items follow B1 role-aware rendering (`Escalations` hidden from ordinary users).
- **Approved Configuration Source**: Reads directly from `Settings` in [app/config.py](../app/config.py) (zero hardcoded duplicates in `map.js`):
  - Fallback location: **Kukatpally Y Junction** (`DEFAULT_MAP_LAT = 17.4849`, `DEFAULT_MAP_LNG = 78.4138`)
  - Default zoom: `DEFAULT_MAP_ZOOM = 16`
  - Geolocation timeout: `GEOLOCATION_TIMEOUT_SECONDS = 5` (`5000 ms`)
  - Bounds debounce (prepared for B4): `MAP_BOUNDS_DEBOUNCE_MS = 400`

---

## 3. Geolocation, Fallback & Re-Centre Behavior

1. **Map Initialization**: Initializes a single Leaflet 1.9.4 map instance with OpenStreetMap tiles (`maxZoom: 19`), guarded against duplicate initialization and duplicate event listeners. Places a single `"You are here"` marker at the approved fallback immediately so the map is interactive right away.
2. **Browser Live Location & 5-Second Timeout**: Calls `navigator.geolocation.getCurrentPosition(...)` with `{ timeout: 5000, maximumAge: 60000 }` paired with a defensive 5-second JS timer and monotonic sequence counter so stale callbacks are ignored and the UI can never hang in a loading state.
3. **Graceful Fallbacks**: Permission denied (`code 1`), position unavailable (`code 2`), timeout (`code 3` or timer expiry), unsupported Geolocation API, or invalid/non-finite/out-of-range coordinates all centre cleanly on Kukatpally Y Junction (`17.4849, 78.4138`) with a clear, non-alarming status message.
4. **Location Marker Distinction (Live vs Fallback)**: Distinctive primary-blue/mint `L.divIcon` (visually distinct from hazard severity pins) updated in place via `setLatLng()`, `setIcon()`, and `setPopupContent()` so repeated updates never create duplicate markers:
   - **Live browser location confirmed**: Marker title/alt `"You are here"`, popup heading `"You are here"` noting *"Current browser location (not saved)."*
   - **Pending initial geolocation or fallback active**: Marker title/alt `"Default map location"`, popup heading `"Default map location"` noting *"Kukatpally Y Junction — default map area, not your confirmed physical location."*
5. **"Re-centre on me" Button**: Present both as an unobstructed floating control on the map canvas and inside the sidebar/bottom sheet. Re-requests browser geolocation (updating the single existing marker between `"You are here"` and `"Default map location"` if live location succeeds or falls back).

---

## 4. Privacy & Security Guarantees

- Live browser coordinates remain strictly in in-memory JavaScript state (`lastLiveLocation`) for the current tab session.
- Live coordinates are never persisted to `localStorage` or `sessionStorage`, never logged to the console, never placed in URLs, and never sent to FastAPI or Supabase.
- No database tables, columns, RLS policies, or authentication flows were altered.
- No authentication tokens or `SUPABASE_SERVICE_KEY` are exposed to browser JavaScript.

---

## 5. Responsive Layout

- **Desktop (`>= 1024px`, verified at `1280px`)**: `360px` fixed left sidebar + full remaining viewport width/height Leaflet map (`calc(100dvh - 4rem)`), zero page scrolling, Leaflet zoom controls positioned `topright`.
- **Tablet & Mobile (`< 1024px`, verified at `768px` and `375px`)**: Full-viewport map with sidebar collapsed into an accessible bottom sheet (`#sr-sheet-toggle`, `aria-expanded`, `aria-controls="sr-map-sheet-body"`, Escape closes). `ResizeObserver` and debounced `window.resize` invoke `map.invalidateSize()` cleanly.

---

## 6. Verification & Known Limitations

- **Automated Pytest Suite (`python -m pytest tests/ -v`)**: All B3 tests in [tests/test_stage6_b3_map.py](../tests/test_stage6_b3_map.py) and all existing Stage 1–6 tests pass (**108 passed, 2 skipped, 0 failed**).
- **Live Headless Chrome Verification**: Verified all 9 mocked geolocation scenarios (granted, denied, 5s timeout, unavailable, unsupported, re-centre success after denial, re-centre failure fallback, 5x repeated re-centre with single marker, and invalid coordinates) plus `1280px`, `768px`, and `375px` viewports with `0` console errors, `0` JS exceptions, and `0` horizontal overflow.
- **Known Limitations**:
  - Hazard markers, category/status filters, and live SSE hazard updates are intentionally out of scope for B3 and will be implemented in Issue `#13` (B4).
  - Browser geolocation requires HTTPS or `localhost` (`127.0.0.1`) in production browsers.
  - The 2 pre-existing local PostgreSQL integration tests in [tests/test_stage4_pg_integration.py](../tests/test_stage4_pg_integration.py) remain skipped locally because `SAFEROUTE_TEST_PG_DSN` is not configured.
