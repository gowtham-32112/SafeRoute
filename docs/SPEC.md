# SafeRoute – Final Build Spec v2

> **Source of Truth**: This specification replaces the earlier plan wherever the two differ. Commit it to the repo as `docs/SPEC.md` and give it to Antigravity together with `AGENTS.md`.

---

## 1. What We Are Building

SafeRoute is a community road-hazard **awareness** platform. Registered users report hazards (potholes, road breaks, broken streetlights, blocked footpaths, other) on a live map, other users verify them, and people can plan routes that avoid known hazards.

**SafeRoute does not repair anything.** It warns people to take care, and when a hazard collects enough reports it is flagged so the admin can mark it as escalated to the higher authority.

---

## 2. Locked Decisions

| Topic | Decision |
|---|---|
| **Stack** | Python 3.11, FastAPI, Jinja2, HTMX, Alpine.js, Tailwind (CDN), Leaflet, Supabase (Postgres + PostGIS, Auth, Storage, Realtime) |
| **Login** | Email + password (Supabase Auth). No Google sign-in |
| **Access** | **Logged-in users only** can see the map and reports. The landing page is public and shows only the pitch with Login / Sign up |
| **Roles** | `user` (everyone who registers) and `admin` (one seeded sample account) |
| **Theme** | Light only |
| **Device priority** | Desktop first, still usable on mobile (down to 375 px) |
| **Language** | English |
| **Map start** | User's live location; fallback **Kukatpally Y Junction** (17.4849° N, 78.4138° E) |
| **Fixed rule** | A report becomes `fixed` when **2 different users** vote "fixed" (admin can override). Confirm this number with the team |
| **Escalation** | Automatic flag when `confirm_count >= 5` or `severity_score >= 70`. Admin marks it Escalated from an admin page. No PDF or email |

---

## 3. Roles and Permissions

| Action | User | Admin |
|---|---|---|
| View map and reports | Yes | Yes |
| Report a hazard (photo, pin, category, description) | Yes | Yes |
| Confirm or dispute a report (not their own, once per report) | Yes | Yes |
| Vote "fixed" with optional after photo | Yes | Yes |
| Comment | Yes | Yes |
| Plan routes, see analytics | Yes | Yes |
| Edit or delete a report | No | Yes |
| See Escalations page, mark Escalated, add authority name and note | No | Yes |
| Override any status, delete any report | No | Yes |

**Sample admin for the demo:** `admin@saferoute.demo` (password set in the seed script and shared privately, never committed in plain text in a public repo).

---

## 4. Pages

| Route | Page | Access |
|---|---|---|
| `/` | Landing: hero, 3 feature cards, Login / Sign up buttons | Public |
| `/login`, `/signup` | Auth forms | Public |
| `/map` | Main app: live map, filters, side list | User |
| `/reports/new` | Report form | User |
| `/live-issues` | Browse open/closed reports, photos, community votes, and admin broadcasts | User |
| `/reports/{id}` | Report detail: photos, severity breakdown, votes, comments, timeline | User |
| `/routes` | Route planner | User |
| `/analytics` | Hotspots and stats | User |
| `/admin/escalations` | Escalation list and actions | Admin |

On `/live-issues`, commuters can read reports and vote to confirm or dispute open reports. Only admins can update or delete reports. Admin broadcasts are shown in the page's broadcast section and refreshed while the page is open.

*After login, users go straight to `/map`.*

---

## 5. Design System

### Palette

| Token | Hex | Use |
|---|---|---|
| `primary` | `#106EBE` | Buttons, links, active states, header |
| `primary-dark` | `#0B4F8A` | Hover, headings on light backgrounds |
| `primary-soft` | `#E6F1FA` | Tinted backgrounds, selected chips |
| `accent` (mint) | `#0FFCBE` | Highlights, badges, focus ring, small accents on **dark blue only** |
| `ink` | `#0F2A43` | Main text |
| `muted` | `#5B6B7C` | Secondary text |
| `surface` | `#FFFFFF` | Cards |
| `canvas` | `#F6FAFD` | Page background |
| `border` | `#D9E4EE` | Borders and dividers |

### Severity Colours (separate from brand colours)
- **Low**: `#2E9E5B`
- **Medium**: `#F5A524`
- **High**: `#D92D20`

*Always show a text label too, never colour alone.*

### Rules
- Never put mint text on a white background (low contrast). Use mint as a highlight on `primary` or `primary-dark` surfaces, as a small badge fill with dark text, or as a focus ring.
- **Type**: Plus Jakarta Sans (Google Fonts), weights 400, 500, 600, 700, 800. Headings 700 to 800, body 400 to 500.
- **Shape and feel**: Rounded corners (12 px cards, 999 px chips), soft shadows, generous spacing, friendly and trustworthy civic tone. Desktop layout for `/map`: left sidebar (filters and report list, about 360 px) with the map filling the rest. On small screens the sidebar becomes a bottom sheet.
- **Every page needs**: Loading state, empty state, error state, visible keyboard focus.

---

## 6. Map Behaviour

1. On `/map`, call `navigator.geolocation.getCurrentPosition`. If granted, centre on the user (zoom 16) and draw a "you are here" marker. If denied, unavailable or timed out (5 s), centre on **Kukatpally Y Junction**. Keep the coordinates as a constant in `config.py` and confirm the exact point on OpenStreetMap before coding.
2. The user can pan and zoom anywhere. On every move end (debounced 400 ms), request hazards for the visible bounds with `reports_in_bounds`. Never load all reports.
3. Subscribe to Supabase Realtime on `hazard_reports` so new, escalated and fixed reports appear or update without a refresh.
4. Markers are coloured by severity band, clustered when zoomed out, and filtered by category chips (pothole, road break, streetlight, footpath, other) and status.
5. Popup shows photo thumbnail, category, severity band, freshness ("3 days ago"), confidence badge and "View details".
6. Geolocation only works on HTTPS or localhost, so deploy with HTTPS.

---

## 7. Data Model (Supabase / PostgreSQL + PostGIS)

Enable first: `CREATE EXTENSION IF NOT EXISTS postgis;`

### Tables
- **`profiles`**: `id` (uuid = auth.users.id), `full_name`, `role` (`user` | `admin`, default `user`), `created_at`. A trigger creates the row on signup.
- **`hazard_reports`**: `id`, `reporter_id`, `category` (`pothole` | `road_break` | `streetlight` | `footpath` | `other`), `description`, `location` `geography(Point,4326)`, `address_text`, `status` (`open` | `escalated` | `fixed`), `escalation_recommended` (bool), `severity_score` (0 to 100), `confidence` (`low` | `medium` | `high`), `confirm_count`, `dispute_count`, `fixed_count`, `ai_suggested_category`, `ai_confidence`, `duplicate_of` (nullable), `is_seed` (bool), `created_at`, `updated_at`, `escalated_at`, `fixed_at`.
- **`report_images`**: `id`, `report_id`, `storage_path`, `kind` (`evidence` | `after`), `created_at`.
- **`verifications`**: `report_id`, `user_id`, `vote` (`confirm` | `dispute` | `fixed`), `created_at`. Unique on `(report_id, user_id, vote)`. A user cannot vote on their own report.
- **`comments`**: `id`, `report_id`, `user_id`, `body`, `created_at`.
- **`escalations`**: `id`, `report_id`, `escalated_by`, `authority_name`, `note`, `created_at`.
- **`status_history`**: `id`, `report_id`, `old_status`, `new_status`, `changed_by`, `created_at`.

### Indexes
- GIST on `hazard_reports.location`
- B-tree on `(status, category, created_at)`

### Storage Bucket
- `hazard-photos` (authenticated upload, authenticated read).

### SQL Functions
- `reports_in_bounds(min_lat, min_lng, max_lat, max_lng, category_filter, status_filter)`: returns visible hazards (limit 500).
- `nearby_reports(lat, lng, radius_m, category)`: for duplicate detection.
- `hazards_near_route(route_geojson, buffer_m)`: for route scoring.

### Triggers
- On insert or delete in `verifications`: recompute counts and `confidence` on the report.
- After recompute: set `escalation_recommended = true` if `confirm_count >= 5` or `severity_score >= 70`. Set `status = 'fixed'` and `fixed_at` when `fixed_count >= 2`.
- On any status change: insert into `status_history`.

### Row Level Security (RLS)
- Anonymous users read nothing. Authenticated users read all reports, images, comments.
- Users insert reports, votes, and comments as themselves. Only admins may update or delete reports.
- Only admin can write `escalations`, set `status = 'escalated'`, override statuses or delete others' reports.

---

## 8. Business Logic

### Duplicate Detection
Before saving a new report, call `nearby_reports(lat, lng, 30, category)` for reports in the last 30 days that are not fixed. If found, show the existing report with two buttons: "Same hazard, add my confirmation" (creates a confirm vote) or "Different, submit anyway" (sets nothing, creates a new report). Log the choice so duplicate rate can be computed.

### Confidence
- **Low**: 0 confirmations.
- **Medium**: 1 to 2 confirmations.
- **High**: 3 or more confirmations and confirmations greater than disputes.
- A report with no activity for 30 days shows "Stale – may no longer be accurate".

### Severity Score (0 to 100, shown with a breakdown)
$$\text{severity\_score} = \text{type\_weight} + \text{evidence} + \text{verification} + \text{recency} \quad (\text{clamped between } 0 \text{ and } 100)$$
- **Type**: `pothole`: 40, `road_break`: 40, `footpath`: 35, `streetlight`: 25, `other`: 15.
- **Evidence**: photo present: +10, AI confidence above 0.7: +10.
- **Verification**: +8 per confirmation (max +24), -10 per dispute.
- **Recency**: within 7 days: +10, 8 to 30 days: +5.
- **Bands**:
  - Low: 0 to 39
  - Medium: 40 to 69
  - High: 70 to 100
- Recompute whenever votes change.

### Escalation
Reports with `escalation_recommended = true` and `status = 'open'` appear in `/admin/escalations`, sorted by severity. Admin clicks "Mark as escalated", enters authority name and a note. This sets `status = 'escalated'`, writes `escalations` and `status_history`, and the report shows an "Escalated to higher authority" badge to everyone.

### Fixed
Any user can press "This is fixed" and optionally upload an after photo. At 2 distinct fixed votes the report becomes `fixed`, leaves the default map view (shown only if "Show fixed" filter is on) and counts toward average time to fix (`fixed_at - created_at`).

---

## 9. AI Category Suggestion

When the user selects a photo, call `POST /reports/suggest-category`. The backend sends the image to a vision model with a JSON-only prompt: classify as `pothole`, `road_break`, `streetlight`, `footpath`, `other` or `none`, with confidence 0 to 1 and a one-line reason.

The form shows a chip like "Suggested: Pothole (82%)" which the user may accept or ignore. Store `ai_suggested_category` and `ai_confidence`. If the call fails, return `{"category": null}` and the form still works. Strip EXIF data from uploaded images.

---

## 10. Route Planner

User sets start and end (search box or map click). Backend calls OSRM (`router.project-osrm.org`, `alternatives=true`), then for each route calls `hazards_near_route(geojson, 25)`. Ignore fixed hazards.

- $\text{route\_risk} = \sum(\text{severity\_score} \times \text{multiplier})$ with multiplier: low 0.5, medium 0.8, high 1.0.
- $\text{route\_score} = \text{duration\_minutes} + (0.05 \times \text{route\_risk})$ (tune with seed data).

Show routes in different colours with duration, distance, hazard count and risk, and highlight the lowest score.

**Always display:**
> *"Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."*

Cache the demo route in case OSRM is slow.

---

## 11. Analytics Page

- Hotspot heatmap (Leaflet.heat)
- Top 5 areas by report count
- Reports by category and status (Chart.js)
- Verification rate, duplicate rate, escalation count, and average time to fix
- Seeded data is excluded when the "Exclude seed data" toggle is on

---

## 12. Safety and Privacy (Must Appear in the Product)

1. **Report form warning**: *"Never photograph hazards while driving or riding. Stop safely first."*
2. **Location Privacy**: Store the hazard location only, never the user's live position. Allow "hide my name" on a report.
3. **Freshness & Verification**: Show freshness and verification status wherever a report appears.
4. **Risk Disclaimer**: Route ranking is a risk indicator only. AI suggestions can be wrong and the user decides.
5. **Limitation Disclosure**: No reports in an area does not mean the area is hazard-free.

---

## 13. Project Structure and Environment

```
saferoute/
├── AGENTS.md
├── docs/
│   └── SPEC.md
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── deps.py
│   ├── routers/ (auth, reports, votes, comments, admin, routes, analytics)
│   ├── services/ (severity, duplicates, vision, routing, analytics)
│   ├── templates/ (base, index, login, signup, map, report_new,
│   │              report_detail, routes, analytics, admin_escalations)
│   └── static/ (css, js, img)
├── supabase/ (schema.sql, functions.sql, triggers.sql, policies.sql, seed.sql)
└── tests/
```

**Environment variables:**
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_KEY`
- `VISION_API_KEY`
- `SECRET_KEY`

*Never commit `.env`.*

---

## 14. Seed Data

About 40 demo reports in and around Kukatpally Y Junction with a mix of categories, severities, ages and a few escalated and fixed ones, all with `is_seed = true`. Plus the sample admin account.

---

## 15. Definition of Done

- A new user can sign up, log in, see the map on their location, report a hazard with a photo, and see it appear live on a second browser.
- Duplicate prompt, confirm and dispute, severity change and the fixed rule all work.
- An admin can open Escalations and mark a report Escalated.
- Route planner returns ranked routes with the disclaimer. Analytics page loads with real numbers.
- Deployed on an HTTPS URL, README with setup steps and validation numbers.

---

## 16. Still Open (Confirm Before Building)

1. **Fixed votes needed**: 2 (default in this spec).
2. **Vision model and API key**: which vision model and API key the team will use for the AI suggestion (e.g., Gemini Flash or OpenAI GPT-4o-mini).
3. **Exact Kukatpally Y Junction fallback coordinates**: 17.4849° N, 78.4138° E.
4. **Member names mapped to**:
   - Member A: Backend and Database
   - Member B: Frontend and UI
   - Member C: AI, Routing, Analytics
