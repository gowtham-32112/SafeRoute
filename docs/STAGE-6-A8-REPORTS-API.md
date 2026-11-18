# SafeRoute — Stage 6, Issue #9 (A8 · Reports API with Duplicate Check)

**Issue**: `#9` (`A8 · Reports API with duplicate check`)
**Branch**: `feat/backend-9-reports-api`
**Scope**: Authenticated Reports API, EXIF-stripped Storage upload with rollback, PostGIS duplicate check & confirmation flow, spatial bounding-box query, and Option R1 authenticated SSE Realtime bridge.

---

## 1. Architecture & File Map

All A8 endpoints run strictly under the authenticated user's Supabase JWT and PostgreSQL Row Level Security (RLS) context via `create_authenticated_supabase_client()`. The Supabase `service_role` key is never used in any A8 handler, service, or Realtime subscription.

| Component | Path | Responsibility |
| :--- | :--- | :--- |
| Router | [app/routers/reports.py](../app/routers/reports.py) | HTTP parsing, auth + CSRF dependency ordering, multipart/JSON handling, SSE streaming response |
| Report Service | [app/services/reports.py](../app/services/reports.py) | Report creation, orphan-free Storage/DB rollback, signed URL generation, EWKB/GeoJSON/WKT decoding, `reports_in_bounds` RPC |
| Image Service | [app/services/images.py](../app/services/images.py) | 5 MB size cap, Pillow JPEG/PNG/WebP validation, EXIF/GPS metadata stripping, Stage 4 Storage path construction |
| Duplicate Service | [app/services/duplicates.py](../app/services/duplicates.py) | `nearby_reports` RPC (30 m / 30 days / same category / non-fixed) and `verifications` `confirm` vote creation |
| Realtime Service | [app/services/realtime.py](../app/services/realtime.py) | Option R1 server-side FastAPI SSE bridge wrapping `realtime.AsyncRealtimeClient` with user JWT |
| Auth Dependencies | [app/deps.py](../app/deps.py) | `AuthenticatedContext` + `get_authenticated_context` reusing Stage 5 session verification and transparent refresh |
| App Registration | [app/main.py](../app/main.py) | Mounts `reports_router` and registers redacted JSON error handlers for image, duplicate, and report domain errors |
| Automated Tests | [tests/test_stage6_a8_reports.py](../tests/test_stage6_a8_reports.py) | 13 mocked unit and API tests covering every A8 requirement and security boundary |

---

## 2. Endpoint Contracts

All endpoints require a valid authenticated session (`get_authenticated_context`). Unauthenticated API calls receive `401 Unauthorized` before CSRF or payload parsing; unauthenticated browser document requests redirect to `/login`. All state-changing endpoints (`POST`) enforce `require_csrf` (same-origin check + constant-time double-submit CSRF cookie/header or form field validation for browser/cookie sessions).

### 2.1 `POST /reports` — Submit a New Hazard Report (or Confirm Existing Duplicate)

- **Content-Type**: `multipart/form-data`
- **Form Fields (New Report)**:
  - `category` (required): `pothole | road_break | streetlight | footpath | other`
  - `description` (required): non-empty string, max `2000` characters
  - `lat` (required): finite float in `[-90.0, 90.0]`
  - `lng` (required): finite float in `[-180.0, 180.0]`
  - `image` (required): JPEG, PNG, or WebP file, max `5 MB` (`5,242,880` bytes)
  - `address_text` (optional): string, max `300` characters
- **Form Fields (Duplicate Confirmation Shortcut)**:
  - `duplicate_action="same_hazard"` and `existing_report_id="<uuid>"`
  - Creates a `confirm` vote in `public.verifications` instead of creating a duplicate report and returns `200 OK` with `ConfirmDuplicateResponse`.
- **Untrusted Client Fields Ignored**: Any client-supplied `reporter_id`, `status`, `severity_score`, `confidence`, `confirm_count`, `dispute_count`, `fixed_count`, `escalation_recommended`, `ai_*`, `is_seed`, or `duplicate_of` values are completely ignored. `reporter_id` is set server-side from `ctx.user.id` and enforced by RLS (`auth.uid()`).
- **Success Response (`201 Created`)**: `ReportDetailResponse` JSON including `id`, `reporter_id`, `category`, `description`, `lat`, `lng`, `address_text`, `status`, `escalation_recommended`, `severity_score`, `severity_band` (`low | medium | high`), `confidence`, counts, timestamps, and `images[]` (with short-lived signed Storage URLs).

### 2.2 `POST /reports/check-duplicate` — Check Nearby Duplicate Reports

- **Content-Type**: `application/json` or `application/x-www-form-urlencoded` / `multipart/form-data`
- **Request Body**:
  ```json
  {
    "lat": 17.48495,
    "lng": 78.41385,
    "category": "pothole"
  }
  ```
- **Behavior**: Calls the Stage 4 PostGIS RPC `public.nearby_reports(lat, lng, radius_m=30.0, category)` under the caller's authenticated RLS client. Returns non-fixed reports of the same category within 30 meters created within the last 30 days, ordered by distance ascending.
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

### 2.3 `POST /reports/confirm-duplicate` — Confirm Existing Duplicate Instead of Creating New Report

- **Content-Type**: `application/json` or form-encoded
- **Request Body**: `{"report_id": "<uuid>"}`
- **Behavior**:
  1. Validates UUID format (`400` if invalid).
  2. Queries `public.hazard_reports` under user RLS (`404` if not found).
  3. Rejects self-confirmation (`reporter_id == ctx.user.id` -> `403 Forbidden`).
  4. Rejects already-fixed reports (`status == "fixed"` -> `400 Bad Request`).
  5. Inserts `{"report_id": ..., "user_id": ctx.user.id, "vote": "confirm"}` into `public.verifications`. Duplicate vote conflicts (PostgreSQL `23505`) return `409 Conflict`.
- **Success Response (`200 OK`)**: `{"action": "confirmed_existing", "report_id": "<uuid>", "vote": "confirm", "message": "..."}`.

### 2.4 `GET /reports/{report_id}` — Retrieve Authorized Hazard Report Details

- **Behavior**: Validates UUID (`400` if malformed), reads the report and its linked `public.report_images` rows under the caller's RLS context (`404` if missing), parses PostGIS coordinates (EWKB hex, GeoJSON, or WKT), computes `severity_band`, and generates short-lived (`3600 s`) signed Storage URLs for private `hazard-photos` objects.
- **Success Response (`200 OK`)**: `ReportDetailResponse`.

### 2.5 `GET /api/reports?bounds=min_lat,min_lng,max_lat,max_lng` — Spatial Map Query

- **Query Parameters**:
  - `bounds` (required): `min_lat,min_lng,max_lat,max_lng` (finite floats; `-90 <= min_lat <= max_lat <= 90`, `-180 <= min_lng <= max_lng <= 180`).
  - `category` (optional): one of the 5 allowed categories.
  - `status` (optional): `open | escalated | fixed`.
- **Behavior**: Invokes the Stage 4 PostGIS RPC `public.reports_in_bounds(min_lat, min_lng, max_lat, max_lng, category_filter, status_filter)` under the caller's RLS context and caps the returned array at `500` items (`MAX_BOUNDS_LIMIT = 500`).
- **Success Response (`200 OK`)**: `{"count": <int>, "limit": 500, "reports": [ReportMapItem, ...]}`.

### 2.6 `GET /api/reports/stream` — Authenticated SSE Realtime Stream (Option R1)

- **Media Type**: `text/event-stream` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`)
- **Events**:
  - `event: connected` — emitted once Supabase Realtime subscription to `public.hazard_reports` is active.
  - `event: hazard_change` — sanitized JSON payload containing `event_type` (`INSERT | UPDATE | DELETE`) and allowlisted hazard fields (`id`, `category`, `description`, `lat`, `lng`, `address_text`, `status`, `escalation_recommended`, `severity_score`, `severity_band`, `confidence`, counts, timestamps).
  - `event: error` — emitted with a redacted message if the Realtime upstream connection fails or closes unexpectedly.
  - `: keepalive` — periodic SSE comment keepalive (`15 s` default) to detect client disconnects cleanly.

---

## 3. Image Security, EXIF Stripping & Orphan-Free Rollback

1. **Bounded Read & Size Enforcement**: `POST /reports` reads at most `MAX_IMAGE_SIZE_BYTES + 1` bytes (`5,242,881` bytes) to bound memory and immediately rejects larger payloads with `413 Payload Too Large`.
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

## 4. Option R1 Realtime Architecture Investigation

- **Installed Packages Verified**: `supabase==2.34.1`, `realtime==2.34.1`.
- **SDK Support Verified**: `realtime.AsyncRealtimeClient` natively supports:
  - WebSocket connection to `wss://<project-ref>.supabase.co/realtime/v1` with `apikey=SUPABASE_ANON_KEY`.
  - User-level JWT authentication via `await client.set_auth(access_token)` so PostgreSQL RLS policies apply to `postgres_changes` events.
  - Channel subscription via `channel.on_postgres_changes(event="*", schema="public", table="hazard_reports", callback=...)` and `await channel.subscribe()`.
  - Clean disconnect teardown in a `finally:` block: cancels the listener task, awaits `client.remove_all_channels()`, and awaits `client.close()`.
- **Security Guarantee**: Authentication cookies remain strictly `HttpOnly`. Neither the user's JWT, refresh token, nor `SUPABASE_SERVICE_KEY` is ever exposed to browser JavaScript or emitted into SSE frames. Frontend Issue B4 will consume `new EventSource('/api/reports/stream')`.

---

## 5. Error Status Code Matrix

| Status | Trigger Conditions |
| :--- | :--- |
| `400 Bad Request` | Missing required fields, invalid category/status/UUID, invalid coordinates/bounds, corrupt image, confirming a fixed report |
| `401 Unauthorized` | Missing, invalid, or expired authentication session on API requests |
| `403 Forbidden` | CSRF validation failure, cross-origin request, self-confirmation on own report, missing profile record |
| `404 Not Found` | Report UUID does not exist (or is not visible under RLS) |
| `409 Conflict` | User already submitted a `confirm` vote on the same hazard report (`23505`) |
| `413 Payload Too Large` | Uploaded image exceeds `5 MB` |
| `415 Unsupported Media Type` | Uploaded file is not JPEG, PNG, or WebP |
| `422 Unprocessable Content` | Pydantic schema validation error on duplicate-check / confirm JSON payloads |
| `503 Service Unavailable` | Supabase Storage, PostgREST, or Realtime upstream error (redacted message; no stack traces or secrets) |

---

## 6. Verification & Testing

### Automated Tests Executed (Mocked Supabase)

Command:
```bash
python -m pytest tests/ -v
```

Result on branch `feat/backend-9-reports-api`:
- **84 passed, 2 skipped** (0 failed).
- All 13 new tests in [tests/test_stage6_a8_reports.py](../tests/test_stage6_a8_reports.py) pass, covering:
  - JPEG EXIF/GPS metadata stripping, 5 MB limit (`413`), unsupported format rejection (`415`), corrupt image rejection (`400`), and Stage 4 Storage path formatting.
  - EWKB hex, GeoJSON, and WKT PostGIS point decoding.
  - Valid `POST /reports` submission, forged-field stripping, missing/invalid field validation, and Storage/DB failure rollbacks.
  - `POST /reports/check-duplicate` RPC invocation (`nearby_reports`) and response mapping.
  - Same-hazard confirmation (`verifications` `confirm` vote, self-vote `403`, duplicate vote `409`).
  - `GET /reports/{id}` retrieval, missing report `404`, and malformed UUID `400`.
  - `GET /api/reports?bounds=...` validation, optional `category`/`status` filtering, and 500-row cap.
  - Unauthenticated `401` enforcement before CSRF/body parsing, CSRF `403` on missing token, and cross-origin `403` rejection.
  - Option R1 SSE Realtime bridge (`set_auth(user_jwt)`, payload sanitization, zero JWT leakage, and clean teardown).
- The 2 skipped tests are pre-existing local PostgreSQL integration tests in [tests/test_stage4_pg_integration.py](../tests/test_stage4_pg_integration.py) (skipped when `SAFEROUTE_TEST_PG_DSN` is not configured).

### Remaining Live Supabase Verification (Pending Manual Test)

Because automated tests mock Supabase PostgREST, Storage, and Realtime so CI never mutates live data, the following live end-to-end checks remain to be verified against the live Supabase project once deployed or tested manually with two ordinary accounts:

1. Upload a real photo with GPS EXIF via `POST /reports`, download the stored file from private bucket `hazard-photos`, and confirm EXIF tags are stripped and the path matches `<auth.uid()>/<report_id>/<uuid>.<ext>`.
2. Submit a second check within 30 m (`POST /reports/check-duplicate`) and confirm `nearby_reports` returns the first report; test "same hazard" confirmation with a second account and verify `confirm_count` increments via Stage 4 triggers.
3. Open `GET /api/reports/stream` with an authenticated session cookie while creating/updating a report and verify live `event: hazard_change` SSE frames arrive and cleanly disconnect when closed.
4. Complete the previously deferred Stage 4 live DB assertions (`tests/stage4_manual_verification.sql` and local PostgreSQL concurrency tests).
