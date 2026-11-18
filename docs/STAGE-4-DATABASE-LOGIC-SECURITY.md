# SafeRoute — Stage 4: Database Logic, Triggers, RLS & Storage Policies (Issues A3–A5)

> - **Document Version**: 1.1.0
> - **Milestone**: Day 1 – Core Loop
> - **Spec Issues Covered**: `A3` (Spatial Functions), `A4` (Triggers & Business Logic), `A5` (Row-Level Security & Storage Policies)
> - **Feature Branch**: `feat/backend-A3-A5-database-logic-security`
> - **Target Supabase Project**: `myaadekzqtubygwhjmab` (`https://myaadekzqtubygwhjmab.supabase.co`)
> - **Supabase Execution Status**: **PENDING** (SQL files updated with PR #6 security review fixes; live DB & concurrency verification pending manual execution)

---

## 1. Scope & Safety Boundaries

Stage 4 builds on the merged Stage 3 schema ([supabase/schema.sql](../supabase/schema.sql), merge commit `67e6f29`) without altering existing tables, dropping data, or implementing Stage 5+ application code:

- **Delivered**:
  - [supabase/functions.sql](../supabase/functions.sql) (A3 — PostGIS spatial functions)
  - [supabase/triggers.sql](../supabase/triggers.sql) (A4 — Verification counts, confidence, severity scoring, fixed rule, escalation recommendation, status history, self-vote/role guards, GUC-free trigger security, AI/duplicate & timestamp enforcement)
  - [supabase/policies.sql](../supabase/policies.sql) (A5 — RLS on all 7 public tables + Storage policies on private bucket `hazard-photos`)
  - [tests/test_stage4_sql.py](../tests/test_stage4_sql.py) (Automated static SQL contract, formula parity, and security-invariant pytest suite)
  - [tests/test_stage4_pg_integration.py](../tests/test_stage4_pg_integration.py) (Real multi-connection PostgreSQL integration & concurrency pytest suite for disposable PostgreSQL+PostGIS databases via `TEST_DATABASE_URL`)
  - [tests/stage4_manual_verification.sql](../tests/stage4_manual_verification.sql) (Safe `BEGIN ... ROLLBACK` live verification script for Supabase SQL Editor covering all 15 security & functional regression scenarios)
- **Preserved Untouched**: `supabase/schema.sql`, `supabase/seed.sql`, and all `app/` FastAPI/frontend files.

---

## 2. A3 — PostGIS Spatial Functions (`supabase/functions.sql`)

All three functions are `SECURITY INVOKER` (so PostgreSQL RLS is always enforced), fix `SET search_path = public, extensions`, revoke `PUBLIC` execution, and grant `EXECUTE` exclusively to `authenticated` and `service_role`.

| Function | Exact Signature | Spatial Operator & Index | Key Filters & Limits |
|---|---|---|---|
| `public.reports_in_bounds` | `(min_lat, min_lng, max_lat, max_lng, category_filter DEFAULT NULL, status_filter DEFAULT NULL)` | `ST_Intersects(hr.location, ST_MakeEnvelope(...)::geography)` $\rightarrow$ uses `idx_hazard_reports_location_gist` | Validates lat `[-90, 90]`, lng `[-180, 180]`, `min <= max`, optional valid category/status; `ORDER BY severity_score DESC, created_at DESC LIMIT 500`. |
| `public.nearby_reports` | `(lat, lng, radius_m DEFAULT 30.0, category DEFAULT NULL)` | `ST_DWithin(hr.location, v_origin, radius_m)` + `ST_Distance` $\rightarrow$ uses `idx_hazard_reports_location_gist` | Validates lat/lng, `0 < radius_m <= 50000`, required valid `category`; same category, `status <> 'fixed'`, `created_at >= NOW() - INTERVAL '30 days'`; returns `distance_m`, `LIMIT 50`. |
| `public.hazards_near_route` | `(route_geojson JSONB, buffer_m DEFAULT 25.0)` | `ST_DWithin(hr.location, v_route_geog, buffer_m)` + `ST_Distance` $\rightarrow$ uses `idx_hazard_reports_location_gist` | Validates `0 < buffer_m <= 5000`, valid non-empty GeoJSON `LineString` or `MultiLineString` (or `Feature` wrapping one); excludes `status = 'fixed'`; returns `distance_m` + severity/confidence fields, `LIMIT 500`. |

### Documented PostgreSQL / PostGIS Notes
1. **`nearby_reports` Output Column Naming**: PostgreSQL rejects duplicate identifiers between input parameters and `RETURNS TABLE` output columns (`42P13`). To preserve the exact Spec Section 7 input signature `(lat, lng, radius_m, category)` for PostgREST RPC calls, output columns for those three fields are named `report_category`, `report_lat`, and `report_lng`.
2. **Spheroidal Bounding Boxes**: `ST_MakeEnvelope(...)::geography` provides exact spheroidal intersection and GIST index acceleration for city and regional map viewports. Viewport spans wider than 180° longitude should be split by the client if global map panning is ever supported.

---

## 3. A4 — Triggers & Business Rules (`supabase/triggers.sql`)

### Trigger Registry & Execution Order

| # | Table | Timing & Event | Trigger Name | Responsibility |
|---|---|---|---|---|
| 1 | `public.profiles` | `BEFORE UPDATE` | `trg_profiles_prevent_role_escalation` | Blocks non-admin authenticated users from changing `profiles.role`. |
| 2 | `public.verifications` | `BEFORE INSERT OR UPDATE` | `trg_verifications_prevent_self_vote` | Blocks voting on own report (`reporter_id = NEW.user_id`) and reassigning `report_id`/`user_id`. |
| 3 | `public.verifications` | `AFTER INSERT OR DELETE` | `trg_verifications_recompute_metrics` | Calls `recompute_report_metrics()` to refresh counts, confidence, severity, escalation flag, and fixed status. |
| 4 | `public.report_images` | `AFTER INSERT OR DELETE` | `trg_report_images_recompute_metrics` | Recalculates severity & escalation flag when `'evidence'` photos are added or removed. |
| 5 | `public.hazard_reports` | `BEFORE INSERT OR UPDATE` | `trg_hazard_reports_before_write` | Runs `SECURITY INVOKER`: computes initial/updated severity & confidence, forces `created_at = NOW()` for ordinary users, rejects forged `ai_confidence`/`ai_suggested_category`/`duplicate_of` on INSERT and UPDATE, blocks protected column mutations, and allows nested internal recomputation **only** when `pg_trigger_depth() > 1 AND CURRENT_USER NOT IN ('authenticated', 'anon')` (zero custom GUCs). |
| 6 | `public.hazard_reports` | `AFTER UPDATE OF status` | `trg_hazard_reports_log_status_history` | Fires strictly `WHEN (OLD.status IS DISTINCT FROM NEW.status)` to insert an immutable audit row into `public.status_history`. |

### Business Rules & Security Hardening Enforced
- **No User-Settable GUC Bypass**: Zero custom PostgreSQL session settings (`set_config` / `current_setting`) are trusted for trigger authorization. Because `handle_hazard_report_before_write()` runs `SECURITY INVOKER`, a direct client `UPDATE` runs with `CURRENT_USER = 'authenticated'` and `pg_trigger_depth() = 1`, whereas internal recomputation from `SECURITY DEFINER` `recompute_report_metrics()` runs with `CURRENT_USER = <db_owner>` (not `authenticated`/`anon`) and `pg_trigger_depth() >= 2`.
- **Protected AI, Duplicate & Timestamp Fields**: Ordinary authenticated users cannot set `ai_confidence`, `ai_suggested_category`, or `duplicate_of` on `INSERT` (enforced in both trigger and `hazard_reports_insert_own` RLS policy) or modify them on `UPDATE`; `created_at` is unconditionally forced to `NOW()` on `INSERT` for ordinary users (preventing backdating and future-dating). Trusted backend (`service_role`) and `admin` workflows remain fully supported.
- **Severity Formula (`public.calculate_severity_score`)**:
  - Type weights: `pothole=40`, `road_break=40`, `footpath=35`, `streetlight=25`, `other=15`.
  - Evidence: photo (`kind = 'evidence'`) `+10`; `ai_confidence > 0.7` `+10`.
  - Verifications: `+8` per confirmation (capped at `+24`); `-10` per dispute.
  - Recency: `<= 7 days` `+10`; `8–30 days` `+5`; `> 30 days` `+0`.
  - Clamped strictly to `[0, 100]` (Bands: Low `0–39`, Medium `40–69`, High `70–100`).
- **Confidence (`public.calculate_confidence`)**:
  - `low`: `0` confirmations.
  - `medium`: `1–2` confirmations (or `>= 3` when confirmations do not strictly exceed disputes).
  - `high`: `>= 3` confirmations AND `confirm_count > dispute_count`.
- **Fixed Rule**: At `>= 2` distinct eligible `'fixed'` votes, status automatically becomes `'fixed'` with `fixed_at = NOW()`. If a `'fixed'` vote is deleted and `fixed_count` drops below `2`, status reverts to `'escalated'` (if `escalated_at IS NOT NULL`) or `'open'` and clears `fixed_at`. Admins retain full status override capability.
- **Escalation Recommendation**: `escalation_recommended = (confirm_count >= 5 OR severity_score >= 70)`. Actual escalation (`status = 'escalated'` + `escalations` insert) remains admin-only.
- **Concurrency Safety**: `recompute_report_metrics()` acquires `SELECT ... FOR UPDATE` on the parent `hazard_reports` row and recalculates exact counts from `public.verifications` rather than using relative increments.

---

## 4. A5 — Row-Level Security Policy Matrix (`supabase/policies.sql`)

RLS is enabled on all 7 public tables. **Anonymous (`anon`) users have zero policies and zero read/write access.**

### Recursion-Free Helper Functions
- `public.is_admin()` (`SECURITY DEFINER`, `SET search_path = public`, zero parameters): returns `TRUE` iff `auth.uid()` has `role = 'admin'` in `public.profiles`.
- `public.current_user_role()` (`SECURITY DEFINER`, `SET search_path = public`, zero parameters): returns the caller's current `role` so `profiles_update_own` verifies `role = public.current_user_role()` without recursive subqueries on `public.profiles`.

### Table Permissions Matrix

| Table | Anonymous (`anon`) | Authenticated User | Admin (`profiles.role = 'admin'`) |
|---|---|---|---|
| `profiles` | Denied | `SELECT` all; `UPDATE` own row only (role locked) | `SELECT` all; `UPDATE` any profile |
| `hazard_reports` | Denied | `SELECT` all; `INSERT` own open report; `UPDATE`/`DELETE` own `open` report only (system/admin columns locked) | Full `SELECT`, `INSERT`, `UPDATE`, `DELETE` |
| `report_images` | Denied | `SELECT` all; `INSERT`/`DELETE` only on own `open` report | Full `SELECT`, `INSERT`, `DELETE` |
| `verifications` | Denied | `SELECT` all; `INSERT` own vote on non-owned report; `DELETE` own vote (no `UPDATE`) | `SELECT`, `INSERT` (non-owned), `DELETE` any |
| `comments` | Denied | `SELECT` all; `INSERT`/`UPDATE`/`DELETE` own comments | `SELECT`, `INSERT`/`UPDATE` own, `DELETE` any |
| `escalations` | Denied | `SELECT` all (read-only) | Full `SELECT`, `INSERT`, `UPDATE`, `DELETE` |
| `status_history` | Denied | `SELECT` all (read-only; cannot insert/update/delete) | `SELECT` all; `INSERT` (trigger also writes automatically); append-only (no `UPDATE`/`DELETE`) |

> [!NOTE]
> **`report_images` & Future `"This is fixed"` After-Photos**: In strict alignment with Stage 4 security rules, ordinary users cannot attach images to reports they do not own. If a later stage enables non-owner `'after'` photo uploads during `"This is fixed"` voting, that workflow should either run through a validated backend service or introduce a scoped `kind = 'after'` policy paired with an existing `'fixed'` vote.

---

## 5. Supabase Storage Policies (`hazard-photos` Private Bucket)

All 4 policies on `storage.objects` are scoped exclusively to `bucket_id = 'hazard-photos'` and `TO authenticated` (anonymous access denied; bucket remains private):

- **Enforced Storage Path Convention**:
  ```text
  <auth.uid()>/<report_id_uuid>/<filename>
  ```
- **Policy Enforcement**:
  - `hazard_photos_insert_own_path`: Requires `array_length(storage.foldername(name), 1) = 2`, segment `[1] = auth.uid()::text`, segment `[2]` matching strict UUID regex, and `name NOT LIKE '%..%'`. Prevents cross-user uploads and path traversal.
  - `hazard_photos_select_authenticated`: Grants read access only to admins, the uploader's own `<auth.uid()>/...` prefix, or objects legitimately linked in `public.report_images` (`ri.storage_path = name`).
  - `hazard_photos_update_own_or_admin` & `hazard_photos_delete_own_or_admin`: Restricted to the path owner (`[1] = auth.uid()::text`) or `public.is_admin()`.
- **Future Upload Workflow Integration**: When submitting a report in Stage 6, the backend generates/creates `report_id` (UUID), strips EXIF metadata, uploads the image to `hazard-photos` at `<user_id>/<report_id>/<uuid>.<ext>`, and inserts the exact matching `storage_path` into `public.report_images`.

---

## 6. Unresolved Specification Decision — "Hide My Name" Privacy Option

- **Identified Mismatch**: `docs/SPEC.md` Section 12 states *"Allow 'hide my name' on a report"*, whereas `docs/SPEC.md` Section 7 (Data Model) and the merged Stage 3 schema (`public.hazard_reports`) do **not** include a privacy flag column.
- **Stage 4 Action**: Per strict scope discipline, Stage 4 did **not** silently invent or add a column to `public.hazard_reports`.
- **Minimum Required Schema Change (if approved by project owner)**:
  ```sql
  ALTER TABLE public.hazard_reports
      ADD COLUMN IF NOT EXISTS hide_reporter_name BOOLEAN NOT NULL DEFAULT FALSE;
  ```
  - Keep `reporter_id` populated internally so ownership checks, RLS edit/delete rules, and anti-self-voting triggers continue functioning cleanly.
  - Mask the displayed reporter name in FastAPI responses/templates (or an explicit view/RPC projection) whenever `hide_reporter_name = TRUE`.
- **Status**: **Unresolved — awaiting project owner decision before Stage 5/6.**

---

## 7. Manual Supabase Execution Order & Verification Procedure

Do **not** drop tables or reset the project. Execute the three Stage 4 SQL files in the Supabase SQL Editor for project **`myaadekzqtubygwhjmab`** (`https://supabase.com/dashboard/project/myaadekzqtubygwhjmab/sql/new`) in this exact order:

1. Run **[supabase/functions.sql](../supabase/functions.sql)** (A3 — spatial functions)
2. Run **[supabase/policies.sql](../supabase/policies.sql)** (A5 — `is_admin()`, `current_user_role()`, table RLS & Storage policies)
3. Run **[supabase/triggers.sql](../supabase/triggers.sql)** (A4 — business logic triggers & guards)

*(All three scripts are idempotent and safe to rerun.)*

### Live Verification Steps (After Running the 3 Files Above)
1. **Transactional Security & Functional Verification (Zero Persisted Data)**:
   - Run [tests/stage4_manual_verification.sql](../tests/stage4_manual_verification.sql) in the Supabase SQL Editor.
   - Exercises all 15 security & functional regression scenarios inside `BEGIN; ... ROLLBACK;` (GUC bypass rejection across all 10 protected columns, AI/duplicate forgery rejection on INSERT & UPDATE, backdated/future `created_at` forcing to `NOW()`, legitimate edits, verification & evidence-image recomputation, A3 spatial queries, and A5 RLS roles).
   - Confirm notice: `ALL STAGE 4 SECURITY & FUNCTIONAL ASSERTIONS PASSED! Rolling back test fixtures.`
2. **Reproducible Multi-Connection Concurrency Verification**:
   - **Automated (Disposable PostgreSQL + PostGIS)**:
     ```powershell
     set TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/saferoute_test
     python -m pytest tests/test_stage4_pg_integration.py -v
     ```
     Executes multi-threaded concurrent PostgreSQL connections using `threading.Barrier` (5 concurrent confirmations, 2 concurrent fixed votes, and concurrent fixed-vote `DELETE` + dispute `INSERT`).
   - **Manual Two-Session Concurrency Test** (if testing interactively in two separate `psql` / SQL sessions against a disposable test report `<R>`):
     - *Session 1*: `BEGIN; INSERT INTO public.verifications (report_id, user_id, vote) VALUES ('<R>', '<user_b>', 'fixed');` *(holds `FOR UPDATE` row lock on `<R>` — do not commit yet)*
     - *Session 2*: `BEGIN; INSERT INTO public.verifications (report_id, user_id, vote) VALUES ('<R>', '<user_c>', 'fixed');` *(observe Session 2 cleanly blocks waiting on Session 1's `FOR UPDATE` lock)*
     - *Session 1*: `COMMIT;` *(Session 2 immediately unblocks, sees both committed fixed votes, sets `fixed_count = 2` and `status = 'fixed'`)*
     - *Session 2*: `COMMIT;`
     - Verify `SELECT confirm_count, dispute_count, fixed_count, status, fixed_at FROM public.hazard_reports WHERE id = '<R>';` returns `fixed_count = 2, status = 'fixed'`.
     - Repeat with Session 1 deleting `<user_c>`'s `'fixed'` vote while Session 2 concurrently inserts a `'confirm'` vote; after both commit, verify `fixed_count = 1`, `status = 'open'`, and `confirm_count` incremented accurately with zero lost updates.
     - Delete the disposable test report `<R>` afterwards.
3. **Manual Storage Policy Verification**:
   - Using two test authenticated accounts, an anonymous client, and an admin account:
     - Confirm User A can upload to `<user_a_uuid>/<report_uuid>/test.jpg` in `hazard-photos`.
     - Confirm User A is **rejected** when uploading to `<user_b_uuid>/<report_uuid>/test.jpg`.
     - Confirm an anonymous client is **rejected** on both upload and download.
     - Confirm an Admin account can read objects in `hazard-photos`.
     - Delete any temporary test object after verification.

---

## 8. Verification Status Summary

| Verification Layer | Status | Details |
|---|:---:|---|
| **Static SQL & Security Review** | **PASS** | Zero custom GUCs; `SECURITY INVOKER` + `pg_trigger_depth() > 1` + `CURRENT_USER`; AI/duplicate/timestamp enforcement; GIST index usage; `FOR UPDATE` locking; zero-recursion RLS & Storage path guards. |
| **Local Automated Static & Formula Tests (`tests/test_stage4_sql.py`)** | **PASS** | All Stage 1, Stage 3, and Stage 4 static/formula pytest tests pass locally. |
| **Local PostgreSQL Integration & Concurrency Tests (`tests/test_stage4_pg_integration.py`)** | **SKIPPED LOCALLY (PENDING)** | No local PostgreSQL instance or running Docker daemon is available in this workstation environment. Concurrency has **not** yet been validated live; run against a disposable PostgreSQL+PostGIS database via `TEST_DATABASE_URL` or use the Two-Session Manual Concurrency Test above. |
| **Supabase SQL Execution (`functions.sql`, `policies.sql`, `triggers.sql`)** | **PENDING** | Prepared for manual execution by project owner in Supabase SQL Editor. |
| **Live Supabase Verification (`stage4_manual_verification.sql` & Storage checks)** | **PENDING** | Run after executing the three Stage 4 SQL files above. |
