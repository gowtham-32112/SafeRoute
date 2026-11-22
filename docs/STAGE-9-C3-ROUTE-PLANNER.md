# Stage 9: Hazard-Aware Route Planner (C3 / C4)

## 1. Overview & Mission

In Stage 9 Part 1, SafeRoute implements the **Hazard-Aware Route Planner** (`/routes` and `/api/routes/plan`). Drivers and citizens can plan routes between origin and destination waypoints (via coordinates, landmark quick-picks, or map clicks), evaluate alternative driving paths provided by OSRM (`router.project-osrm.org`), intersect known road hazards within a 25-metre corridor using PostGIS (`hazards_near_route`), and compare routes ranked by duration and road hazard risk.

**Assigned Issues**:
- Issue #30: `C3 · Hazard-aware route planner API and UI (/routes)`
- Assignee: `kommukuriabhinav-crypto` (Member C — AI, Routing & Analytics)

---

## 2. Architecture & Design Principles

```
  +------------------+         +----------------------------+         +-------------------+
  |  Citizen Browser |         | SafeRoute Backend (FastAPI)|         | Upstream OSRM /   |
  |  (/routes)       |         +----------------------------+         | PostGIS Database  |
  +--------+---------+                       |                        +-------------------+
           | 1. Selects Origin & Destination |                                  |
           |    (Quick-fill, Lat/Lng, Click) |                                  |
           |                                 |                                  |
           | 2. POST /api/routes/plan        |                                  |
           |    [Origin, Dest + CSRF Token]  |                                  |
           +-------------------------------->| 3. Query OSRM Driving Service   |
                                             |    alternatives=true             |
                                             +--------------------------------->|
                                             |                                  |
                                             | 4. OSRM Alternative Polylines    |
                                             |<---------------------------------+
                                             |    (Or cached demo fallback      |
                                             |     corridor on timeout/outage)  |
                                             |                                  |
                                             | 5. PostGIS hazards_near_route    |
                                             |    (route_geojson, buffer_m=25)  |
                                             +--------------------------------->|
                                             |                                  |
                                             | 6. Intersecting Active Hazards   |
                                             |<---------------------------------+
                                             |                                  |
                                             | 7. Compute route_risk & score:   |
                                             |    risk = sum(score * mult)      |
                                             |    score = dur + 0.05 * risk     |
                                             |    Sort ascending by score       |
           | 8. HTTP 200 JSON                |                                  |
           |    RankedRoute[] + Disclaimer   |                                  |
           |<--------------------------------+                                  |
           |                                                                    |
           | 9. Render Polylines & Hazards                                      |
           |    on Leaflet Map + Cards List                                     |
           v                                                                    v
```

### Key Pillars:
1. **Authoritative Risk Scoring (Spec Section 10)**:
   - Severity Multipliers: `low` (score &le; 39) &rarr; `0.5`, `medium` (40–69) &rarr; `0.8`, `high` (&ge; 70) &rarr; `1.0`.
   - Fixed Hazard Exclusion: Hazards with `status == 'fixed'` are completely ignored.
   - Route Risk Formula: $\text{route\_risk} = \sum(\text{severity\_score} \times \text{multiplier})$.
   - Route Score Formula: $\text{route\_score} = \text{duration\_minutes} + (0.05 \times \text{route\_risk})$.
   - Routes are sorted ascending by `route_score`; the lowest score is marked `recommended = True`.
2. **Deterministic Fallback Resilience**:
   - If OSRM is unreachable, times out, or returns HTTP 5xx, the system seamlessly serves a deterministic cached Kukatpally corridor fallback route with `is_fallback_route = True` and a clear notice banner. The route planner never crashes.
3. **Mandatory Route Safety Disclaimer**:
   - Every route view and API response prominently displays:
     > *"Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."*
4. **Privacy & Security**:
   - Live user locations are never recorded in `localStorage` or `sessionStorage`.
   - PostGIS queries execute strictly under the authenticated user's session JWT context (`create_authenticated_supabase_client`). Zero `SUPABASE_SERVICE_KEY` in runtime code.

---

## 3. Configuration

Configured via environment variables in `app/config.py`:

| Variable | Default | Purpose |
|---|---|---|
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Base URL for the OSRM driving routing service |
| `OSRM_TIMEOUT_SECONDS` | `6.0` | Upstream HTTP timeout for the OSRM driving route request |
| `ROUTE_HAZARD_BUFFER_METERS` | `25.0` | PostGIS corridor buffer distance in metres |

---

## 4. API Specification

### `GET /routes`
- **Authentication**: Required (`303` redirect to `/login` for unauthenticated browser requests).
- **Template Context**: Prepares CSRF token, fallback coordinates (Kukatpally Y Junction `17.4849, 78.4138`), default zoom, and preset landmark quick-picks.

### `POST /api/routes/plan`
- **Authentication**: Required (`401 Unauthorized`).
- **CSRF**: Double-submit CSRF protection (`X-CSRF-Token` header, `403 Forbidden`).
- **Request Body**:
  ```json
  {
    "origin_lat": 17.4849,
    "origin_lng": 78.4138,
    "dest_lat": 17.4930,
    "dest_lng": 78.3980
  }
  ```
- **Response Schema (`RoutePlanResponse`)**:
  ```json
  {
    "routes": [
      {
        "id": "osrm-route-1",
        "summary": "Main Arterial Corridor",
        "duration_minutes": 5.4,
        "distance_km": 2.35,
        "route_risk": 15.0,
        "route_score": 6.15,
        "recommended": true,
        "geojson": {
          "type": "LineString",
          "coordinates": [[78.4138, 17.4849], [78.4060, 17.4880], [78.3980, 17.4930]]
        },
        "hazards_count": 1,
        "hazards": [
          {
            "id": "hazard-uuid",
            "category": "pothole",
            "description": "Crater near road shoulder",
            "lat": 17.4881,
            "lng": 78.4061,
            "address_text": "NH 65",
            "status": "open",
            "severity_score": 30,
            "severity_band": "low",
            "confidence": "high",
            "confirm_count": 3,
            "dispute_count": 0,
            "distance_m": 8.4,
            "multiplier": 0.5,
            "risk_contribution": 15.0
          }
        ]
      }
    ],
    "is_fallback_route": false,
    "fallback_notice": null,
    "disclaimer": "Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."
  }
  ```

---

## 5. Frontend UI (`/routes`)

- **Interactive Layout**: Desktop split layout (controls & cards on left, full map on right); tablet/mobile stacked responsive layout with zero horizontal overflow.
- **Waypoints Controls**:
  - Origin / Destination coordinate inputs with landmark quick-select.
  - "Pick on map" interactive toggle: click map to set Origin (A) and Destination (B).
  - "Use my location" browser geolocation with 5s timeout.
- **Route Cards**:
  - Highlights the **★ Recommended** route with mint `#0FFCBE` fill and dark `#0F2A43` text.
  - Displays Duration, Distance, Hazards count within 25m, Route Risk, and total Route Score.
  - Shows an expandable list of intersecting hazards with distance to route and direct links to `/reports/{id}`.
- **Leaflet Integration (`routes.js`)**:
  - Recommended polyline rendered in solid primary `#106EBE` (weight 6).
  - Alternative polyline rendered in muted dashed `#5B6B7C` (weight 4).
  - Clicking any card highlights its polyline and fits map bounds.
  - Hazard circle markers plotted along the active route (Low `#2E9E5B`, Medium `#F5A524`, High `#D92D20`) with safe DOM popups (zero `.innerHTML`).

---

## 6. Verification Results

- **Automated Tests**:
  - `tests/test_stage9_c3_route_planner.py`: **13 passed, 0 failed**.
  - Full repository test suite: **179 passed, 2 skipped, 0 failed**.
- **Live Headless Chrome CDP Verification**:
  - Verified on Desktop (1280px), Tablet (768px), and Mobile (375px).
  - 0 horizontal overflow (`scrollWidth <= clientWidth`).
  - 0 console errors and 0 page exceptions.
  - Route planning, fallback handling, disclaimer visibility, and route switching confirmed.
