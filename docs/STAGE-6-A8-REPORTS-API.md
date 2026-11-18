# SafeRoute — Stage 6, Issue #9 (A8 · Reports API with Duplicate Check)

**Issue**: `#9` (`A8 · Reports API with duplicate check`)
**Branch**: `feat/backend-9-reports-api`
**Scope**: Authenticated Reports API, bounded request-size middleware, EXIF-stripped Storage upload with rollback, fixed 30 m PostGIS duplicate detection, server-side duplicate confirmation revalidation, spatial bounding-box query, and Option R1 authenticated SSE Realtime bridge.

---

## 1. Architecture & File Map

All A8 endpoints run strictly under the authenticated user's Supabase JWT and PostgreSQL Row Level Security (RLS) context via `create_authenticated_supabase_client()`. The Supabase `service_role` key is never used in any A8 handler, service, or Realtime subscription.

| Component | Path | Responsibility |
| :--- | :--- | :--- |
| Config | [app/config.py](../app/config.py) | `MAX_REQUEST_BODY_BYTES` (default `6 MiB`), `DUPLICATE_RADIUS_METERS` (`30.0`, validated `<= 30.0`), `DUPLICATE_WINDOW_DAYS` (`30`) |
| App & ASGI Middleware | [app/main.py](../app/main.py) | Pure ASGI `RequestSizeLimitMiddleware` (`413` before buffering/parsing), mounts `reports_router`, registers redacted JSON error handlers |
| Router | [app/routers/reports.py](../app/routers/reports.py) | HTTP parsing, bounded `request.form()` after auth/CSRF, duplicate confirmation validation, SSE streaming response |
| Report Service | [app/services/reports.py](../app/services/reports.py) | Report creation, orphan-free Storage/DB rollback, signed URL generation, EWKB/GeoJSON/WKT decoding, `reports_in_bounds` RPC |
| Image Service | [app/services/images.py](../app/services/images.py) | 5 MB image size cap, Pillow JPEG/PNG/WebP validation, EXIF/GPS metadata stripping, Stage 4 Storage path construction |
| Duplicate Service | [app/services/duplicates.py](../app/services/duplicates.py) | Fixed 30 m `nearby_reports` RPC and authoritative PostGIS revalidation before `verifications` `confirm` vote creation |
| Realtime Service | [app/services/realtime.py](../app/services/realtime.py) | Option R1 server-side FastAPI SSE bridge wrapping `realtime.AsyncRealtimeClient` with user JWT |
| Auth Dependencies | [app/deps.py](../app/deps.py) | `AuthenticatedContext` + `get_authenticated_context` reusing Stage 5 session verification and transparent refresh |
| Automated Tests | [tests/test_stage6_a8_reports.py](../tests/test_stage6_a8_reports.py) | 16 mocked unit and API tests covering every A8 requirement and security boundary |

---

## 2. Request-Body Resource Protection & Parsing Order

### FastAPI Lifecycle & Defense-in-Depth

In FastAPI (`fastapi.routing.get_request_handler`), any route that declares `Form(...)` or `File(...)` parameters in its function signature causes FastAPI to call `await request.form()` **before** `solve_dependencies()` runs. Relying on dependency parameter ordering alone does not prevent multipart parsing on routes with `Form`/`File` parameters.

SafeRoute protects against unbounded buffering and unauthenticated multipart parsing with two layered controls:

1. **Outermost Pure ASGI `RequestSizeLimitMiddleware`** ([app/main.py](../app/main.py)):
   - Enforces configurable `Settings.MAX_REQUEST_BODY_BYTES` (default `6 MiB` = `6,291,456` bytes: the approved `5 MB` image cap plus `1 MiB` for multipart boundaries and form metadata).
   - **Fast-path `Content-Length` check**: Rejects declared lengths `> MAX_REQUEST_BODY_BYTES` immediately with `413 Payload Too Large` (and malformed/negative `Content-Length` with `400 Bad Request`) before calling `receive()`, routing, authentication, or multipart parsing.
   - **Chunked / Streaming / Spoofed `Content-Length` enforcement**: Wraps ASGI `receive()` to count cumulative incoming bytes and aborts immediately with `413 Payload Too Large` as soon as streamed bytes exceed `MAX_REQUEST_BODY_BYTES`, preventing unbounded memory/disk buffering.
2. **Explicit `await request.form()` Inside `submit_hazard_report`** ([app/routers/reports.py](../app/routers/reports.py)):
   - `POST /reports` accepts `request: Request` rather than `Form`/`File` signature parameters.
   - Consequently, for bounded requests (`<= MAX_REQUEST_BODY_BYTES`), `get_authenticated_context` genuinely executes **before** `request.form()` is called (unauthenticated callers are rejected with `401` / `/login` redirect without invoking multipart parsing), followed by `require_csrf` (which skips `request.form()` when `X-CSRF-Token` is sent in the header), followed by bounded `await request.form()` (closed cleanly in `finally:`) and the exact `5 MB` image-only read cap (`MAX_IMAGE_SIZE_BYTES + 1`).

### Production Reverse-Proxy Requirements

Application middleware is defense-in-depth, not a replacement for edge/proxy limits. In production deployments, configure the trusted reverse proxy / edge tier with matching request-body and timeout ceilings so abusive payloads are dropped before reaching Uvicorn workers:

- **Nginx**: `client_max_body_size 6m;` and `client_body_timeout 15s;`
- **Caddy**: `request_body { max_size 6MB }`
- **Render / Cloud Load Balancers / Cloudflare**: Enforce a `<= 6 MB` upload body cap on `/reports*` routes and enable standard slow-loris / body read timeouts.

---

## 3. Endpoint Contracts

All endpoints require a valid authenticated session (`get_authenticated_context`). All state-changing endpoints (`POST`) enforce `require_csrf` (same-origin check + constant-time double-submit CSRF cookie/header or form field validation for browser/cookie sessions).

### 3.1 `POST /reports` — Submit a New Hazard Report (or Confirm Existing Duplicate)

- **Content-Type**: `multipart/form-data` (or `application/x-www-form-urlencoded` for `duplicate_action=same_hazard`)
- **Form Fields (New Report)**:
  - `category` (required): `pothole | road_break | streetlight | footpath | other`
  - `description` (required): non-empty string, max `2000` characters
  - `lat` (required): finite float in `[-90.0, 90.0]`
  - `lng` (required): finite float in `[-180.0, 180.0]`
  - `image` (required): JPEG, PNG, or WebP file, max `5 MB` (`5,242,880` bytes)
  - `address_text` (optional): string, max `300` characters
- **Form Fields (Duplicate Confirmation Shortcut)**:
  - `duplicate_action="same_hazard"`, `existing_report_id="<uuid>"`, `category`, `lat`, and `lng` (all required).
  - Independently revalidates all duplicate criteria server-side via PostGIS before creating a `confirm` vote in `public.verifications`; returns `200 OK` with `ConfirmDuplicateResponse` (never creates a second report).
- **Untrusted Client Fields Ignored**: Any client-supplied `reporter_id`, `status`, `severity_score`, `confidence`, `confirm_count`, `dispute_count`, `fixed_count`, `escalation_recommended`, `ai_*`, `is_seed`, `duplicate_of`, or `distance_m` values are completely ignored. `reporter_id` is set server-side from `ctx.user.id` and enforced by RLS (`auth.uid()`).
- **Success Response (`201 Created`)**: `ReportDetailResponse` JSON including `id`, `reporter_id`, `category`, `description`, `lat`, `lng`, `address_text`, `status`, `escalation_recommended`, `severity_score`, `severity_band` (`low | medium | high`), `confidence`, counts, timestamps, and `images[]` (with short-lived signed Storage URLs).

### 3.2 `POST /reports/check-duplicate` — Check Nearby Duplicate Reports

- **Content-Type**: `application/json` or `application/x-www-form-urlencoded` / `multipart/form-data`
- **Request Body**:
  ```json
  {
    "lat": 17.48495,
    "lng": 78.41385,
    "category": "pothole"
  }
  ```
- **Fixed 30-Metre Radius Enforcement**:
  - The duplicate search radius is locked server-side to `Settings.DUPLICATE_RADIUS_METERS` (`30.0` metres) and `Settings.DUPLICATE_WINDOW_DAYS` (`30` days).
  - **Chosen Behavior for Client Radius Overrides**: If a client submits `radius_m` with any value other than `30.0` (including values `> 30`, `<= 0`, `< 30`, or non-finite values), the API **explicitly rejects** the request with `422 Unprocessable Content` without invoking the SQL RPC. Omitting `radius_m` (recommended) or passing `30` uses the server-side `30.0` configuration.
- **Behavior**: Calls the Stage 4 PostGIS RPC `public.nearby_reports(lat, lng, radius_m=30.0, category)` under the caller's authenticated RLS client. Returns non-fixed reports of the same category within 30 metres created within the last 30 days, ordered by distance ascending.
- **Success Response (`200 OK`)**:
  ```json
  {
    "has_duplicates": true,
    "radius_m": 30.0,
    "lookback_days": 30,
    "matches": [
      {
        "id": "...",
        "reporter_id": "...",
        "category": "pothole",
        "description": "Nearby pothole",
        "lat": 17.4849,
        "lng": 78.4138,
        "address_text": "Kukatpally",
        "status": "open",
        "severity_score": 60,
        "confidence": "low",
        "confirm_count": 0,
        "dispute_count": 0,
        "created_at": "2026-11-18T04:00:00Z",
        "distance_m": 12.35
      }
    ]
  }
  ```

### 3.3 `POST /reports/confirm-duplicate` — Confirm Existing Duplicate Instead of Creating New Report

- **Content-Type**: `application/json` or form-encoded
- **Request Body** (minimum required fields for independent server-side revalidation):
  ```json
  {
    "report_id": "<uuid>",
    "lat": 17.48495,
    "lng": 78.41385,
    "category": "pothole"
  }
  ```
- **Server-Side Authoritative Revalidation** (applied identically to both `POST /reports/confirm-duplicate` and `POST /reports` with `duplicate_action=same_hazard`):
  1. Validates `report_id` (UUID), `lat`/`lng` (finite, valid WGS84 bounds), and `category` (one of the 5 allowed categories). Never trusts client-supplied `distance_m` or prior duplicate-check results.
  2. Queries `public.hazard_reports` under user RLS (`404` if not found).
  3. Rejects self-confirmation (`reporter_id == ctx.user.id` -> `403 Forbidden`).
  4. Rejects already-fixed reports (`status == "fixed"` -> `400 Bad Request`).
  5. Rejects category mismatch (`existing.category != submitted_category` -> `400 Bad Request`).
  6. Rejects reports older than 30 days (`400 Bad Request`).
  7. Calls the authoritative Stage 4 PostGIS RPC `public.nearby_reports(lat, lng, radius_m=30.0, category)` and verifies that `report_id` is returned as an eligible match (same category, non-fixed, `ST_DWithin` within 30 metres, created within the last 30 days). If not matched, rejects with `400 Bad Request` and inserts **no** confirmation vote.
  8. Inserts `{"report_id": ..., "user_id": ctx.user.id, "vote": "confirm"}` into `public.verifications`. Duplicate vote conflicts (PostgreSQL `23505`) return `409 Conflict`.
- **Success Response (`200 OK`)**: `{"action": "confirmed_existing", "report_id": "<uuid>", "vote": "confirm", "message": "..."}`.

### 3.4 `GET /reports/{report_id}` — Retrieve Authorized Hazard Report Details

- **Behavior**: Validates UUID (`400` if malformed), reads the report and its linked `public.report_images` rows under the caller's RLS context (`404` if missing), parses PostGIS coordinates (EWKB hex, GeoJSON, or WKT), computes `severity_band`, and generates short-lived (`3600 s`) signed Storage URLs for private `hazard-photos` objects.
- **Success Response (`200 OK`)**: `ReportDetailResponse`.

### 3.5 `GET /api/reports?bounds=min_lat,min_lng,max_lat,max_lng` — Spatial Map Query

- **Query Parameters**:
  - `bounds` (required): `min_lat,min_lng,max_lat,max_lng` (finite floats; `-90 <= min_lat <= max_lat <= 90`, `-180 <= min_lng <= max_lng <= 180`).
  - `category` (optional): one of the 5 allowed categories.
  - `status` (optional): `open | escalated | fixed`.
- **Behavior**: Invokes the Stage 4 PostGIS RPC `public.reports_in_bounds(min_lat, min_lng, max_lat, max_lng, category_filter, status_filter)` under the caller's RLS context and caps the returned array at `500` items (`MAX_BOUNDS_LIMIT = 500`).
- **Success Response (`200 OK`)**: `{"count": <int>, "limit": 500, "reports": [ReportMapItem, ...]}`.

### 3.6 `GET /api/reports/stream` — Authenticated SSE Realtime Stream (Option R1)

- **Media Type**: `text/event-stream` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`)
- **Events**:
  - `event: connected` — emitted once Supabase Realtime subscription to `public.hazard_reports` is active.
  - `event: hazard_change` — sanitized JSON payload containing `event_type` (`INSERT | UPDATE | DELETE`) and allowlisted hazard fields (`id`, `category`, `description`, `lat`, `lng`, `address_text`, `status`, `escalation_recommended`, `severity_score`, `severity_band`, `confidence`, counts, timestamps).
  - `event: error` — emitted with a redacted message if the Realtime upstream connection fails or closes unexpectedly.
  - `: keepalive` — periodic SSE comment keepalive (`15 s` default) to detect client disconnects cleanly.

---

## 4. Image Security, EXIF Stripping & Orphan-Free Rollback

1. **Bounded Request & Image Size Enforcement**: `RequestSizeLimitMiddleware` rejects total request bodies exceeding `MAX_REQUEST_BODY_BYTES` (`6 MiB`) with `413` before multipart parsing; inside `POST /reports`, the uploaded file read is bounded to `MAX_IMAGE_SIZE_BYTES + 1` (`5,242,881` bytes) to enforce the exact `5 MB` image cap (`413`).
2. **Format Verification**: Uses Pillow (`PIL.Image`) to decode the actual binary format and permits only `JPEG`, `PNG`, and `WEBP`. Spoofed MIME types or unsupported formats (e.g., PDF, SVG, GIF) return `415 Unsupported Media Type`; corrupt/truncated images return `400 Bad Request`.
3. **EXIF & GPS Stripping**: Applies `ImageOps.exif_transpose()` to preserve visual orientation, copies raw pixel data into a clean image buffer, and re-encodes without EXIF, GPS, IPTC, XMP, or ICC metadata blocks.
4. **Stage 4 Storage Ownership Path**: Uploads to private bucket `hazard-photos` at:
   ```text
   <auth.uid()>/<report_id_uuid>/<random_uuid>.<ext>
   ```
   This strictly satisfies the Stage 4 `storage.objects` RLS policies in [supabase/policies.sql](../supabase/policies.sql) (`array_length(storage.foldername(name), 1) = 2`, UUID check on folder segment 2, no `..` traversal).
5. **Orphan-Free 3-Step Creation Flow**:
   - Step 1: Upload EXIF-stripped image to `hazard-photos`. If upload fails -> `503` with zero database writes.
   - Step 2: Insert `public.hazard_reports` row with pre-generated `report_id`. If insert fails -> remove uploaded Storage object immediately (zero orphaned file) and return `503`.
   - Step 3: Insert `public.report_images` row (`kind="evidence"`). If insert fails -> delete the `hazard_reports` row AND remove the Storage object, then return `503`.

---

## 5. Option R1 Realtime Architecture Investigation

- **Installed Packages Verified**: `supabase==2.34.1`, `realtime==2.34.1`.
- **SDK Support Verified**: `realtime.AsyncRealtimeClient` natively supports:
  - WebSocket connection to `wss://<project-ref>.supabase.co/realtime/v1` with `apikey=SUPABASE_ANON_KEY`.
  - User-level JWT authentication via `await client.set_auth(access_token)` so PostgreSQL RLS policies apply to `postgres_changes` events.
  - Channel subscription via `channel.on_postgres_changes(event="*", schema="public", table="hazard_reports", callback=...)` and `await channel.subscribe()`.
  - Clean disconnect teardown in a `finally:` block: cancels the listener task, awaits `client.remove_all_channels()`, and awaits `client.close()`.
- **Security Guarantee**: Authentication cookies remain strictly `HttpOnly`. Neither the user's JWT, refresh token, nor `SUPABASE_SERVICE_KEY` is ever exposed to browser JavaScript or emitted into SSE frames. Frontend Issue B4 will consume `new EventSource('/api/reports/stream')`.

---

## 6. Error Status Code Matrix

| Status | Trigger Conditions |
| :--- | :--- |
| `400 Bad Request` | Malformed `Content-Length`, missing required form fields, invalid category/status/UUID, invalid coordinates/bounds, corrupt image, failed duplicate revalidation (different category, >30 m, >30 days, or fixed report) |
| `401 Unauthorized` | Missing, invalid, or expired authentication session on API requests |
| `403 Forbidden` | CSRF validation failure, cross-origin request, self-confirmation on own report, missing profile record |
| `404 Not Found` | Report UUID does not exist (or is not visible under RLS) |
| `409 Conflict` | User already submitted a `confirm` vote on the same hazard report (`23505`) |
| `413 Payload Too Large` | Request body exceeds `MAX_REQUEST_BODY_BYTES` (`6 MiB`) OR uploaded image exceeds `5 MB` |
| `415 Unsupported Media Type` | Uploaded file is not JPEG, PNG, or WebP |
| `422 Unprocessable Content` | Pydantic validation failure on duplicate-check/confirm JSON payloads, including attempts to override the fixed 30 m duplicate search radius |
| `503 Service Unavailable` | Supabase Storage, PostgREST, or Realtime upstream error (redacted message; no stack traces or secrets) |

---

## 7. Verification & Testing

### Automated Tests Executed (Mocked Supabase)

Command:
```bash
python -m pytest tests/ -v
```

Result on branch `feat/backend-9-reports-api`:
- **87 passed, 2 skipped** (0 failed).
- All 16 tests in [tests/test_stage6_a8_reports.py](../tests/test_stage6_a8_reports.py) pass, covering:
  - Request-body size limit middleware (`Content-Length`, chunked streaming, spoofed `Content-Length`, and proof that `Request.form()` is never invoked on oversized or unauthenticated requests).
  - JPEG EXIF/GPS metadata stripping, 5 MB image limit (`413`), unsupported format rejection (`415`), corrupt image rejection (`400`), and Stage 4 Storage path formatting.
  - EWKB hex, GeoJSON, and WKT PostGIS point decoding.
  - Valid `POST /reports` submission, forged-field stripping, missing/invalid field validation, and Storage/DB failure rollbacks.
  - `POST /reports/check-duplicate` fixed 30 m radius enforcement and `422` rejection of client radius override attempts (`> 30 m`, `<= 0`, `< 30 m`).
  - Independent server-side duplicate confirmation revalidation on both `POST /reports/confirm-duplicate` and `POST /reports` (`duplicate_action=same_hazard`): valid match, different category, outside 30 m (including forged `distance_m`), older than 30 days, fixed report, own report (`403`), duplicate vote (`409`), and missing/forged location/category inputs.
  - `GET /reports/{id}` retrieval, missing report `404`, and malformed UUID `400`.
  - `GET /api/reports?bounds=...` validation, optional `category`/`status` filtering, and 500-row cap.
  - Auth `401`, CSRF `403`, and cross-origin `403` enforcement.
  - Option R1 SSE Realtime bridge (`set_auth(user_jwt)`, payload sanitization, zero JWT leakage, and clean teardown).
- The 2 skipped tests are pre-existing local PostgreSQL integration tests in [tests/test_stage4_pg_integration.py](../tests/test_stage4_pg_integration.py) (skipped when `SAFEROUTE_TEST_PG_DSN` is not configured).

### Remaining Live Supabase Verification (Pending Manual Test)

Because automated tests mock Supabase PostgREST, Storage, and Realtime so CI never mutates live data, the following live end-to-end checks remain to be verified against the live Supabase project once deployed or tested manually with two ordinary accounts:

1. Upload a real photo with GPS EXIF via `POST /reports`, download the stored file from private bucket `hazard-photos`, and confirm EXIF tags are stripped and the path matches `<auth.uid()>/<report_id>/<uuid>.<ext>`.
2. Submit a second check within 30 m (`POST /reports/check-duplicate`) and confirm live PostGIS `nearby_reports` returns the first report; test "same hazard" confirmation with a second account (both within 30 m and outside 30 m) and verify `confirm_count` increments via Stage 4 triggers only when eligible.
3. Open `GET /api/reports/stream` with an authenticated session cookie while creating/updating a report and verify live `event: hazard_change` SSE frames arrive and cleanly disconnect when closed.
4. Complete the previously deferred Stage 4 live DB assertions (`tests/stage4_manual_verification.sql` and local PostgreSQL concurrency tests).
