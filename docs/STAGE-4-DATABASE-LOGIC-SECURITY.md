# SafeRoute — Stage 4: Database Logic, Triggers, RLS & Storage Policies (Issues A3–A5)

> - **Document Version**: 1.0.0
> - **Milestone**: Day 1 – Core Loop
> - **Spec Issues Covered**: `A3` (Spatial Functions), `A4` (Triggers & Business Logic), `A5` (Row-Level Security & Storage Policies)
> - **Feature Branch**: `feat/backend-A3-A5-database-logic-security`
> - **Target Supabase Project**: `myaadekzqtubygwhjmab` (`https://myaadekzqtubygwhjmab.supabase.co`)
> - **Supabase Execution Status**: **PENDING** (SQL files prepared for manual execution in Supabase SQL Editor; live DB verification pending)

---

## 1. Scope & Safety Boundaries

Stage 4 builds on the merged Stage 3 schema ([supabase/schema.sql](../supabase/schema.sql), merge commit `67e6f29`) without altering existing tables, dropping data, or implementing Stage 5+ application code:

- **Delivered**:
  - [supabase/functions.sql](../supabase/functions.sql) (A3 — PostGIS spatial functions)
  - [supabase/triggers.sql](../supabase/triggers.sql) (A4 — Verification counts, confidence, severity scoring, fixed rule, escalation recommendation, status history, self-vote & role-escalation guards)
  - [supabase/policies.sql](../supabase/policies.sql) (A5 — RLS on all 7 public tables + Storage policies on private bucket `hazard-photos`)
  - [tests/test_stage4_sql.py](../tests/test_stage4_sql.py) (Automated contract, formula parity, and security-invariant pytest suite)
  - [tests/stage4_manual_verification.sql](../tests/stage4_manual_verification.sql) (Safe `BEGIN ... ROLLBACK` live verification script for Supabase SQL Editor)
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
| 5 | `public.hazard_reports` | `BEFORE INSERT OR UPDATE` | `trg_hazard_reports_before_write` | Computes initial/updated severity & confidence, maintains `updated_at`/`escalated_at`/`fixed_at`, blocks ordinary users from altering system/admin columns, and skips internal recursion via `saferoute.internal_metrics_update`. |
| 6 | `public.hazard_reports` | `AFTER UPDATE OF status` | `trg_hazard_reports_log_status_history` | Fires strictly `WHEN (OLD.status IS DISTINCT FROM NEW.status)` to insert an immutable audit row into `public.status_history`. |

### Business Rules Enforced
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
- **Concurrency & Recursion Safety**: `recompute_report_metrics()` acquires `SELECT ... FOR UPDATE` on the parent `hazard_reports` row to serialize concurrent votes and uses transaction-local `set_config('saferoute.internal_metrics_update', 'on', TRUE)` to prevent trigger recursion.

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
1. **Transactional Database Verification (Zero Persisted Data)**:
   - Run [tests/stage4_manual_verification.sql](../tests/stage4_manual_verification.sql) in the Supabase SQL Editor.
   - It executes assertions for A3 spatial functions, A4 triggers (severity, confidence, self-vote, duplicate vote, 2-vote fixed rule, 5-confirm/70-severity escalation recommendation, status history), and A5 RLS roles (`anon`, User A, User B, Admin) inside `BEGIN; ... ROLLBACK;`.
   - Confirm notice: `ALL STAGE 4 LIVE TRANSACTIONAL ASSERTIONS PASSED! Rolling back test fixtures.`
2. **Manual Storage Policy Verification**:
   - Using two test authenticated accounts (or Supabase client in a local non-committed script):
     - Confirm User A can upload to `<user_a_uuid>/<report_uuid>/test.jpg` in `hazard-photos`.
     - Confirm User A is **rejected** when uploading to `<user_b_uuid>/<report_uuid>/test.jpg`.
     - Confirm an anonymous client is **rejected** on both upload and download.
     - Confirm an Admin account can read objects in `hazard-photos`.
     - Delete any temporary test object after verification.

---

## 8. Verification Status Summary

| Verification Layer | Status | Details |
|---|:---:|---|
| **Static SQL & Security Review** | **PASS** | Reviewed syntax, `42P13` parameter safety, GIST index usage, `FOR UPDATE` locking, recursion guards, zero-recursion RLS helpers, and Storage path rules. |
| **Local Automated Tests (`pytest tests/ -v`)** | **PASS** | Stage 1 health tests + Stage 3 schema tests + Stage 4 SQL contract & formula parity tests. |
| **Supabase SQL Execution (`functions.sql`, `policies.sql`, `triggers.sql`)** | **PENDING** | Prepared for manual execution by project owner in Supabase SQL Editor. |
| **Live Supabase Verification (`stage4_manual_verification.sql` & Storage checks)** | **PENDING** | Run after executing the three Stage 4 SQL files above. |
