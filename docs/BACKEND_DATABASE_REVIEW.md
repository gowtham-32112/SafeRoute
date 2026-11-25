# SafeRoute — Backend & Database Engineering Review

> **Document Scope**: Complete end-to-end review of the SafeRoute Backend and Database architecture, PostGIS spatial data modeling, Supabase integration, RESTful endpoints, security policies, concurrency controls, and business logic.
> **Team Alignment**: Member A (Backend & Database Engineering)
> **Repository Location**: `d:\SafeRoute-2`

---

## 1. Technology Stack & Environment

SafeRoute’s backend and persistence layer are engineered for high throughput, strict spatial data integrity, stateless scalability, and multi-tenant security:

| Component | Technology | Version / Specification | Rationale & Responsibility |
| :--- | :--- | :--- | :--- |
| **Runtime & Language** | Python | 3.11 | Modern typing, high performance, native async/await concurrency. |
| **API Framework** | FastAPI | >= 0.110.0 | High-performance ASGI framework with automatic OpenAPI documentation, schema validation, and dependency injection. |
| **Data Validation** | Pydantic v2 | >= 2.6.0 | Strict schema modeling, field normalization, and request/response serialization. |
| **Relational Database** | PostgreSQL | 15+ (Supabase) | Robust relational engine with ACID transactions, foreign keys, triggers, and JSONB support. |
| **Spatial Engine** | PostGIS | 3.3+ | Native geospatial types (`geography(Point, 4326)`), spherical distance math, and GIST spatial indexing. |
| **BaaS Services** | Supabase | Auth, Storage, Realtime | Managed JWT authentication, secure S3-compatible private asset storage (`hazard-photos`), and Postgres CDC Realtime streaming. |
| **Image Processing** | Pillow (PIL) | >= 10.2.0 | EXIF/GPS/XMP metadata sanitization to protect citizen privacy and enforce 5 MB image constraints. |
| **HTTP Client** | HTTPX | >= 0.27.0 | Non-blocking async client for OSRM routing and Gemini Vision AI API calls. |
| **Test Suite** | Pytest & Pytest-Asyncio | >= 8.0.0 | 206 automated tests covering unit logic, PostGIS SQL contracts, RLS policies, and API integration. |

---

## 2. Directory & Component Architecture

```
app/
├── main.py                     # ASGI lifespan, custom size-limit middleware, exception handlers, router registry
├── config.py                   # Pydantic BaseSettings, environment variables, geospatial constants, thresholds
├── deps.py                     # FastAPI dependency injection (Auth, CSRF, Admin checks, Settings)
├── routers/
│   ├── auth.py                 # /auth/signup, /auth/login, /auth/logout, /auth/me, /auth/admin
│   ├── reports.py              # /reports, /reports/suggest-category, /reports/check-duplicates, /reports/realtime
│   ├── admin.py                # /admin/escalations, /admin/resolve, /admin/users, /admin/broadcast
│   ├── routes.py               # /routes, /api/routes/plan
│   ├── analytics.py            # /analytics, /api/analytics
│   ├── ai.py                   # /api/ai/assistant (Civic AI Commuter queries)
│   └── live_issues.py          # /live-issues, /live-issues/{id}/vote, /live-issues/{id}/edit, /live-issues/{id}/delete
└── services/
    ├── admin.py                # Civic authority escalation, resolution with proof, user role administration
    ├── ai_assistant.py         # Grounded civic conversational engine with real-time analytics context
    ├── analytics.py            # KPI counters, category/status breakdown, top affected areas, heatmap generator
    ├── auth.py                 # Supabase Auth client wrapper, profile synchronization
    ├── broadcast.py            # Active broadcast message publishing and dismissal
    ├── community.py            # Voting engine (confirm/dispute/fixed), comment threads, self-vote prevention
    ├── duplicates.py           # 30-meter / 30-day spatial duplicate detection logic
    ├── images.py               # 5 MB validation, MIME inspection, complete EXIF/GPS stripping
    ├── live_issues.py          # Commuter live issues querying and admin mutation workflows
    ├── realtime.py             # Server-Sent Events (SSE) hazard updates streaming
    ├── reports.py              # Core hazard CRUD, bounding-box spatial queries, severity calculations
    ├── routing.py              # OSRM client, 25m buffer hazard intersection, route risk scoring
    ├── session.py              # Secure HttpOnly cookie handling, double-submit CSRF cookie generation
    ├── supabase_client.py      # Authenticated user Supabase client factory (RLS-scoped)
    └── vision_ai.py            # Gemini Vision API integration with safe non-blocking fallbacks

supabase/
├── schema.sql                  # Core tables, constraints, GIST & B-tree indexes, auth signup trigger
├── functions.sql               # Spatial RPC functions: reports_in_bounds, nearby_reports, hazards_near_route
├── triggers.sql                # Concurrency-safe severity calculation, self-vote guard, status history logging
├── policies.sql                # Strict Row-Level Security (RLS) on all 7 tables & storage policies
├── seed.sql                    # Realistic seed data for Kukatpally, Hyderabad (hazards, votes, comments)
└── migrations/                 # Incremental schema migration scripts
```

---

## 3. Database Schema & Data Model (`supabase/schema.sql`)

### Seven Core Application Tables

1. **`public.profiles`**:
   - `id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE`
   - `full_name TEXT NOT NULL DEFAULT ''`
   - `role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin'))`
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
   - *Security Note*: Automatically populated via `handle_new_user()` trigger on `auth.users`. Ordinary users cannot modify their role (enforced by RLS and trigger).

2. **`public.hazard_reports`**:
   - `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
   - `reporter_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL`
   - `category TEXT NOT NULL CHECK (category IN ('pothole', 'road_break', 'streetlight', 'footpath', 'other'))`
   - `description TEXT NOT NULL DEFAULT ''`
   - `location geography(Point, 4326) NOT NULL` (strictly longitude, latitude in PostGIS)
   - `address_text TEXT`
   - `status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'escalated', 'fixed'))`
   - `escalation_recommended BOOLEAN NOT NULL DEFAULT FALSE`
   - `severity_score INTEGER NOT NULL DEFAULT 0 CHECK (severity_score >= 0 AND severity_score <= 100)`
   - `confidence TEXT NOT NULL DEFAULT 'low' CHECK (confidence IN ('low', 'medium', 'high'))`
   - `confirm_count INTEGER NOT NULL DEFAULT 0`
   - `dispute_count INTEGER NOT NULL DEFAULT 0`
   - `fixed_count INTEGER NOT NULL DEFAULT 0`
   - `ai_suggested_category TEXT CHECK (ai_suggested_category IN ('pothole', 'road_break', 'streetlight', 'footpath', 'other', 'none'))`
   - `ai_confidence DOUBLE PRECISION CHECK (ai_confidence >= 0.0 AND ai_confidence <= 1.0)`
   - `duplicate_of UUID REFERENCES public.hazard_reports(id) ON DELETE SET NULL`
   - `is_seed BOOLEAN NOT NULL DEFAULT FALSE`
   - `created_at`, `updated_at`, `escalated_at`, `fixed_at TIMESTAMPTZ`

3. **`public.report_images`**:
   - `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
   - `report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE`
   - `storage_path TEXT NOT NULL` (stored in private bucket `hazard-photos`)
   - `kind TEXT NOT NULL DEFAULT 'evidence' CHECK (kind IN ('evidence', 'after'))`
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

4. **`public.verifications`**:
   - `report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE`
   - `user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE`
   - `vote TEXT NOT NULL CHECK (vote IN ('confirm', 'dispute', 'fixed'))`
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
   - `PRIMARY KEY (report_id, user_id, vote)` — guarantees idempotent, non-duplicative community votes.

5. **`public.comments`**:
   - `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
   - `report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE`
   - `user_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL`
   - `body TEXT NOT NULL CHECK (BTRIM(body) <> '')`
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

6. **`public.escalations`**:
   - `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
   - `report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE`
   - `escalated_by UUID REFERENCES public.profiles(id) ON DELETE SET NULL`
   - `authority_name TEXT NOT NULL` (e.g., "GHMC Road Maintenance Division")
   - `note TEXT NOT NULL DEFAULT ''`
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

7. **`public.status_history`**:
   - `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
   - `report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE`
   - `old_status TEXT CHECK (old_status IN ('open', 'escalated', 'fixed'))`
   - `new_status TEXT NOT NULL CHECK (new_status IN ('open', 'escalated', 'fixed'))`
   - `changed_by UUID REFERENCES public.profiles(id) ON DELETE SET NULL`
   - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

### Indexes for High Performance
- **Spatial GIST Index**: `CREATE INDEX idx_hazard_reports_location_gist ON public.hazard_reports USING GIST (location);`
- **Composite B-Tree Index**: `CREATE INDEX idx_hazard_reports_status_category_created ON public.hazard_reports (status, category, created_at DESC);`
- **Foreign Key Lookups**: Dedicated B-tree indexes on `reporter_id`, `duplicate_of`, `report_images(report_id)`, `comments(report_id, created_at)`, `escalations(report_id)`, and `status_history(report_id)`.

---

## 4. PostGIS Spatial Functions (`supabase/functions.sql`)

All database functions are configured with `SECURITY INVOKER`, ensuring PostgreSQL Row-Level Security (RLS) is applied to every caller, with explicit search path isolation (`search_path = public, extensions`).

1. **`reports_in_bounds(min_lat, min_lng, max_lat, max_lng, category_filter, status_filter)`**:
   - Efficiently filters reports within the map viewport using `ST_MakeEnvelope` and `ST_Intersects(location, bounds)`.
   - Returns up to 500 reports ordered by `severity_score DESC, created_at DESC`.

2. **`nearby_reports(lat, lng, radius_m, category)`**:
   - Duplicate detection engine executed prior to report creation.
   - Uses `ST_DWithin(location, origin, radius_m)` with a 30m default radius and 30-day recency cutoff on non-fixed issues.

3. **`hazards_near_route(route_geojson, buffer_m)`**:
   - Route safety evaluation engine.
   - Accepts a GeoJSON LineString representing an OSRM driving route, constructs a spherical buffer (`ST_DWithin` with default 25m), and returns all intersecting active hazards with exact distance calculations.

---

## 5. Automated Triggers & Business Logic (`supabase/triggers.sql`)

1. **Self-Vote Prevention Trigger (`trg_verifications_prevent_self_vote`)**:
   - Fires `BEFORE INSERT OR UPDATE` on `public.verifications`.
   - Compares `NEW.user_id` against `hazard_reports.reporter_id`. If identical, raises exception `P0001` ("Users cannot vote on their own hazard reports").

2. **Serialized Metric Recomputation (`recompute_report_metrics()`)**:
   - Fires `AFTER INSERT OR DELETE` on `public.verifications` and `public.report_images`.
   - Acquires an exclusive row-level lock (`SELECT ... FOR UPDATE`) on the target `hazard_reports` row.
   - Recounts `confirm_count`, `dispute_count`, and `fixed_count` directly from source verification rows.
   - Recomputes `severity_score` using the approved formula:
     - **Base Weights**: `pothole: 40`, `road_break: 40`, `footpath: 35`, `streetlight: 25`, `other: 15`
     - **Evidence**: `+10` if evidence photo attached, `+10` if `ai_confidence > 0.7`
     - **Verification**: `+8` per confirm vote (clamped to max `+24`), `-10` per dispute vote
     - **Recency**: `+10` if $\le 7$ days old, `+5` if 8–30 days old, `+0` if $> 30$ days old
     - **Total**: Clamped to $[0, 100]$.
   - Evaluates **Confidence Rating**:
     - `high`: Has photo, $\ge 3$ confirms, 0 disputes.
     - `medium`: Has photo or $\ge 1$ confirm, with $\le 1$ dispute.
     - `low`: Default state.
   - Evaluates **Escalation Recommendation**:
     - Automatically flags `escalation_recommended = TRUE` when `confirm_count >= 5` OR `severity_score >= 70`.
   - Evaluates **Auto-Resolution**:
     - Automatically transitions status to `fixed` when `fixed_count >= 2`.

3. **Status Audit Logging Trigger (`trg_hazard_reports_log_status_history`)**:
   - Fires `AFTER UPDATE OF status` on `public.hazard_reports`.
   - Automatically writes an immutable audit record into `public.status_history` capturing `old_status`, `new_status`, timestamp, and modifying user.

---

## 6. Security, RLS & Storage Policies (`supabase/policies.sql`)

### Strict Row-Level Security Rules
- **RLS Enabled**: Enabled on all 7 application tables.
- **Anonymous Access**: Anonymous (`anon`) role has **zero** read and **zero** write access to any database table or storage bucket.
- **Commuter Privileges**: Authenticated users can view all hazards, submit new reports, add comments, and vote (confirm/dispute/fixed) on reports created by other commuters. Commuters can only update their own profile name.
- **Admin Isolation**: Only users with `role = 'admin'` can insert into `escalations`, update report status to `escalated` or `fixed`, modify other users' roles, or delete reports.
- **Storage Path Convention**: Storage bucket `hazard-photos` enforces folder structure `<auth.uid()>/<report_id>/<filename>`; authenticated users can only upload into their own UID directory.

---

## 7. Backend Services & Core APIs

### 1. Request Size & Anti-Abuse Protection (`RequestSizeLimitMiddleware`)
- Pure ASGI streaming middleware running before multipart parsing or authentication.
- Checks `Content-Length` header; terminates immediately with `413 Content Too Large` if declared size $> 6\text{ MB}$.
- Wraps body stream with a chunked byte counter to block spoofed or missing `Content-Length` payloads.

### 2. Authentication & Session Management (`app/routers/auth.py` & `app/services/session.py`)
- Stateless authentication using Supabase Auth JWTs.
- Tokens stored in secure HttpOnly cookies (`sr_access_token` with 1-hour expiry, `sr_refresh_token` with 7-day expiry).
- Double-Submit CSRF cookie (`sr_csrf_token`) validated on all mutating `POST`, `PUT`, `DELETE` operations via `require_csrf` dependency.

### 3. Hazard Reports Router (`app/routers/reports.py`)
- `POST /reports`: Handles multipart submissions (`category`, `description`, `lat`, `lng`, `image`). Validates coordinates, calls `validate_and_strip_exif` (Pillow), saves photo to Supabase Storage, and inserts PostGIS point.
- `GET /reports`: High-speed bounding box query querying `reports_in_bounds()`.
- `POST /reports/check-duplicates`: Returns existing non-fixed hazards within 30m.
- `POST /reports/confirm-duplicate`: Adds an upvote to an existing report, avoiding redundant database records.
- `GET /reports/{id}`: Returns comprehensive hazard details with photos, comments, and vote tallies.
- `POST /reports/{id}/vote`: Casts `confirm`, `dispute`, or `fixed` vote; triggers automatic recomputation.
- `GET /reports/realtime`: Server-Sent Events (SSE) streaming live hazard additions and status updates to connected clients.

### 4. Admin Management Router (`app/routers/admin.py`)
- `GET /admin/escalations`: Triage view of high-priority hazards.
- `POST /admin/escalations/{id}`: Records official civic escalation with authority assignment and audit notes.
- `POST /admin/resolve/{id}`: Records hazard repair with optional after-fix photo evidence and sets status to `fixed`.
- `GET /admin/users` & `POST /admin/users/{id}/role`: Lists registered profiles and permits role elevation/demotion.
- `POST /admin/broadcast` & `DELETE /admin/broadcast`: Manages active system-wide emergency civic alerts.

### 5. Live Issues Router (`app/routers/live_issues.py`)
- Direct commuter access to search, filter, and review community hazard reports in grid or table view.
- Provides quick-voting shortcuts and administrative moderation controls (edit category/status, delete spam).

---

## 8. Backend & Database Use Cases

### Use Case 1: Tamper-Proof Hazard Submission
1. Mobile commuter uploads a 4.2 MB camera snapshot with embedded GPS and camera metadata.
2. `RequestSizeLimitMiddleware` verifies size is under 6 MB.
3. `deps.py` validates JWT session cookie and checks CSRF token.
4. `images.py` inspects image bytes, strips 100% of EXIF/GPS/XMP tags using Pillow, and re-encodes to clean JPEG.
5. Service uploads clean image to Supabase Storage: `hazard-photos/<user_id>/<report_id>/evidence.jpg`.
6. Record inserted into `public.hazard_reports` with PostGIS point `ST_SetSRID(ST_MakePoint(lng, lat), 4326)`.
7. Initial severity score computed automatically; SSE engine notifies all connected map clients.

### Use Case 2: Concurrency-Safe Community Verification & Auto-Resolution
1. Three commuters simultaneously tap "Fixed" on a resolved road break.
2. Each request hits `POST /reports/{id}/vote` with `vote=fixed`.
3. Database trigger `recompute_report_metrics()` fires for each transaction:
   - Row-level lock (`FOR UPDATE`) serializes the three operations.
   - `fixed_count` reaches 2.
   - Status automatically transitions from `open` to `fixed`.
   - `fixed_at` timestamp set to `NOW()`.
   - Audit record automatically written to `status_history`.
4. The resolved hazard is immediately excluded from active route penalty calculations.

### Use Case 3: Admin Civic Authority Escalation
1. Multiple commuters confirm a dangerous pothole near JNTU, driving its severity score to 78 and confirm count to 6.
2. Trigger marks `escalation_recommended = TRUE`.
3. Civic administrator opens `/admin/escalations`.
4. Administrator assigns the issue to "GHMC Engineering Division" with priority note: "Major arterial route; immediate patch required."
5. Status changes to `escalated`, escalation record logged, and public status updated across map markers in real-time.
