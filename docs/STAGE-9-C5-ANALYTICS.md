# Stage 9: Hazard Analytics Dashboard & Heatmap (C5)

## 1. Overview & Mission

In Stage 9 Part 2, SafeRoute implements the **Hazard Analytics Dashboard & Heatmap** (`/analytics` and `/api/analytics`). Citizens, city administrators, and urban planners gain deep situational awareness into community-reported road hazards through real-time aggregated metrics, interactive geographic heatmaps, ranked priority zones, and category/status visual breakdowns.

**Assigned Issues**:
- Issue #32: `C5 · Hazard analytics dashboard and heatmap (/analytics)`
- Assignee: `kommukuriabhinav-crypto` (Member C — AI, Routing & Analytics)

---

## 2. Architecture & Design Principles

```
  +------------------+         +----------------------------+         +-------------------+
  |  Citizen Browser |         | SafeRoute Backend (FastAPI)|         | Supabase PostGIS  |
  |  (/analytics)    |         +----------------------------+         | Database (RLS)    |
  +--------+---------+                       |                        +-------------------+
           | 1. GET /analytics               |                                  |
           +-------------------------------->| 2. Verify Session JWT            |
           |                                 |    (303 to /login if unauth)     |
           |                                 |                                  |
           | 3. Render HTML Template         | 4. Fetch Active Reports          |
           |    (KPI cards, Map & Chart IDs) |<-------------------------------->|
           |<--------------------------------+                                  |
           |                                                                    |
           | 5. GET /api/analytics?exclude_seed=false                           |
           +-------------------------------->| 6. Query Reports & Escalations   |
           |                                 |    under user JWT / RLS          |
           |                                 +--------------------------------->|
           |                                 |                                  |
           |                                 | 7. Compute Analytics Metrics:    |
           |                                 |    - Total & Breakdown Counts    |
           |                                 |    - Verification & Dupl Rates   |
           |                                 |    - Escalations & Fix Time      |
           |                                 |    - Top 5 Affected Areas        |
           |                                 |    - Normalized Heatmap Points   |
           | 8. HTTP 200 JSON                |                                  |
           |<--------------------------------+                                  |
           |                                                                    |
           | 9. Alpine Controller:                                              |
           |    - Leaflet.heat overlay                                          |
           |    - Chart.js category bar chart                                   |
           |    - Chart.js status doughnut                                      |
           |    - Top 5 ranked zones list                                       |
           |    - Dynamic re-fetch on Exclude Seed toggle                       |
           v                                                                    v
```

### Key Pillars:
1. **Authoritative Metric Formulations (Spec Section 11)**:
   - **Verification Rate**: Percentage of reports with at least one community verification vote:
     $$\text{Verification Rate} = \frac{\text{count}(\text{confirm\_count} + \text{dispute\_count} + \text{fixed\_count} > 0)}{\text{total\_reports}} \times 100$$
   - **Duplicate Rate**: Percentage of reports marked as duplicate:
     $$\text{Duplicate Rate} = \frac{\text{count}(\text{duplicate\_of} \neq \text{null})}{\text{total\_reports}} \times 100$$
   - **Escalation Count**: Count of active municipal escalations from the `escalations` table.
   - **Average Time to Fix**: Mean elapsed hours between `created_at` and `resolved_at` for all reports in `fixed` status:
     $$\text{Avg. Time to Fix (hrs)} = \frac{\sum (\text{resolved\_at} - \text{created\_at})}{\text{count}(\text{fixed reports})}$$
     (Returns `null` if no fixed reports exist).
   - **Top 5 Affected Areas**: Clustered by `address_text` (or fallback coordinate grid `Near lat, lng`), ranked descending by report count and average severity score, strictly capped at 5 areas.
   - **Heatmap Points**: Formatted as `[lat, lng, intensity]`, where `intensity` is normalized from severity score `[0, 100]` to `[0.2, 1.0]`.
2. **Deterministic Zero-Division Safety**:
   - When `total_reports == 0`, all rates return `0.0`, `avg_time_to_fix_hours` returns `null`, and category/status breakdowns return zero without runtime errors or crashes.
3. **Exclude Seed Data Toggle**:
   - Supports filtering out synthetic seed data (`description ILIKE '[SEED]%'`) via dynamic query parameter `?exclude_seed=true|false`, updating the dashboard in place without page reloads.
4. **Privacy & Security**:
   - Zero storage of citizen locations in browser storage.
   - Database operations execute strictly under the authenticated user's session JWT context (`create_authenticated_supabase_client`). Zero `SUPABASE_SERVICE_KEY` in runtime code.
   - Light-theme only (`color-scheme: only light;`, zero `dark:` classes), no mint text on light surfaces, and strict safe DOM text binding (zero `innerHTML` on dynamic content).

---

## 3. API Specification

### `GET /analytics`
- **Authentication**: Required (`303` redirect to `/login` for unauthenticated requests).
- **Template Context**: Prepares default dashboard layout, KPI placeholder cards, heatmap container (`#sr-analytics-heatmap`), Chart.js canvas elements (`#sr-chart-category`, `#sr-chart-status`), and "Exclude seed data" toggle.

### `GET /api/analytics`
- **Authentication**: Required (`401 Unauthorized` for missing/invalid session).
- **Query Parameters**:
  - `exclude_seed` (bool, default `False`): When `True`, excludes all hazard records marked as `[SEED]`.
- **Response Model (`HazardAnalyticsResponse`)**:
  ```json
  {
    "total_reports": 12,
    "verification_rate": 75.0,
    "duplicate_rate": 8.3,
    "escalation_count": 3,
    "avg_time_to_fix_hours": 36.5,
    "by_category": {
      "pothole": 6,
      "road_break": 3,
      "streetlight": 1,
      "footpath": 2,
      "other": 0
    },
    "by_status": {
      "open": 8,
      "escalated": 3,
      "fixed": 1
    },
    "top_affected_areas": [
      {
        "area_name": "Kukatpally Y Junction, NH65",
        "report_count": 5,
        "avg_severity": 78.4
      }
    ],
    "heatmap_points": [
      [17.4849, 78.4138, 0.82]
    ],
    "disclaimer": "Hazard analytics and heatmaps reflect community-submitted reports and do not certify comprehensive roadway safety."
  }
  ```

---

## 4. Frontend Implementation

- **Template**: `app/templates/analytics.html` extending `templates/base.html`.
- **Controller**: `app/static/js/analytics.js` (`srAnalyticsDashboard` Alpine component).
- **Libraries**:
  - Leaflet (`L.map`) with Leaflet.heat (`L.heatLayer`).
  - Chart.js 4.4.x via CDN (`Chart.js`).
- **Accessibility & Contrast**:
  - Every chart canvas is paired with an accessible fallback data summary table for screen readers.
  - SafeRoute design system tokens used throughout: `primary` (`#106EBE`), `accent` (`#0FFCBE`), and standard severity colors (Green `#2E9E5B`, Amber `#F5A524`, Red `#D92D20`).
  - Zero `innerHTML` operations. All dynamic labels, counts, and lists use Alpine `x-text` and template bindings.

---

## 5. Verification & Testing

- Automated tests: `tests/test_stage9_c5_analytics.py` (9/9 pass):
  1. `test_analytics_page_unauthenticated_redirects_to_login`
  2. `test_analytics_api_unauthenticated_returns_401`
  3. `test_analytics_page_authenticated_renders_html`
  4. `test_analytics_api_authenticated_returns_metrics`
  5. `test_analytics_zero_division_safety_on_empty_dataset`
  6. `test_analytics_calculations_accuracy`
  7. `test_analytics_exclude_seed_toggle`
  8. `test_analytics_top_affected_areas_max_5`
  9. `test_analytics_static_assets_and_security_standards`
- Full test suite: 188 passed, 2 skipped, 0 failures.
- Headless Chrome testing across 3 viewports: Desktop (1280x800), Tablet (768x1024), Mobile (375x667) confirming 0 horizontal overflow and reactive toggle re-fetching.
