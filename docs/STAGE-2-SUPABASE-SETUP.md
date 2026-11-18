# SafeRoute — Stage 2: Supabase Infrastructure Setup & Final Verification

> - **Document Version**: 2.0.0
> - **Milestone**: Day 1 – Core Loop
> - **GitHub Issue**: `A1 · Supabase project, PostGIS, Auth and Storage`
> - **Project Reference**: `myaadekzqtubygwhjmab`
> - **Project URL**: `https://myaadekzqtubygwhjmab.supabase.co`
> - **Verification Status**: **COMPLETE (Stage 2 Passed; Realtime Pending Stage 3)**
> - **Last Verified**: 2026-11-18

---

## 1. Executive Summary & Scope Boundary

Stage 2 provisions and verifies the foundational Supabase cloud infrastructure for **SafeRoute** without implementing application-level schemas, triggers, SQL functions, or RLS policies.

- **In Scope**: Supabase project provisioning, PostGIS extension enablement, Auth provider and redirect URL configuration, private `hazard-photos` storage bucket creation, local `.env` configuration, and read-only verification.
- **Strictly Out of Scope (Reserved for Stages 3–5)**: Application tables (`profiles`, `hazard_reports`, etc.), geography columns, spatial RPC functions, triggers, RLS policies, seed data, and FastAPI authentication handlers.

---

## 2. Supabase Project Configuration

| Setting | Configured Value | Verification Method | Status |
|---|---|---|:---:|
| **Project Name** | `SafeRoute` | Supabase Dashboard | **PASS** |
| **Project Reference** | `myaadekzqtubygwhjmab` | Live API + Dashboard | **PASS** |
| **Project URL** | `https://myaadekzqtubygwhjmab.supabase.co` | Live API (`GoTrue v2.198.0`, HTTP 200) | **PASS** |
| **PostgreSQL / PostgREST** | Supabase-managed PostgreSQL | Live PostgREST schema cache response | **PASS** |

*Note: API keys, service-role keys, and database passwords are never stored in documentation or committed to Git.*

---

## 3. Database Extension Configuration: PostGIS

SafeRoute requires PostGIS for Stage 3/4 spatial storage (`geography(Point, 4326)`) and proximity/route calculations.

- **Read-Only SQL Verification Executed (Supabase SQL Editor)**:
  ```sql
  SELECT
      extname,
      extversion,
      extnamespace::regnamespace AS schema_name
  FROM pg_extension
  WHERE extname = 'postgis';
  ```
- **Verification Outcome**: PostGIS extension is installed and present in the database catalog.
- **Status**: **PASS** *(Verified directly via user's read-only SQL Editor query; automated script cannot read `pg_extension` with publishable key).*

> [!IMPORTANT]
> No application tables, geography columns, or spatial SQL functions were created in Stage 2.

---

## 4. Supabase Authentication Configuration

| Setting | Configured / Observed Value | Verification Method | Status |
|---|---|---|:---:|
| **GoTrue Auth Health** | `GoTrue v2.198.0` (`HTTP 200 OK`) | Automated Live API (`/auth/v1/health`) | **PASS** |
| **Email & Password Auth** | Enabled (`external.email = true`, `disable_signup = false`) | Automated Live API (`/auth/v1/settings`) | **PASS** |
| **Google OAuth Provider** | Disabled (`external.google = false`) | Automated Live API (`/auth/v1/settings`) | **PASS** |
| **Email Confirmation** | Enabled (`mailer_autoconfirm = false`) | Automated Live API (`/auth/v1/settings`) | **PASS** |
| **Site URL** | `http://127.0.0.1:8000` | Manual Dashboard Verification (Project Owner) | **PASS** |
| **Redirect URL 1** | `http://127.0.0.1:8000/**` | Manual Dashboard Verification (Project Owner) | **PASS** |
| **Redirect URL 2** | `http://localhost:8000/**` | Manual Dashboard Verification (Project Owner) | **PASS** |

*Note: Public `/auth/v1/settings` does not expose `site_url` or redirect URLs to publishable keys; those settings were confirmed directly in Supabase Dashboard $\rightarrow$ Authentication $\rightarrow$ URL Configuration.*

---

## 5. Supabase Storage Bucket Configuration (`hazard-photos`)

- **Read-Only SQL Verification Executed (Supabase SQL Editor)**:
  ```sql
  SELECT
      id,
      name,
      public,
      file_size_limit,
      allowed_mime_types
  FROM storage.buckets
  WHERE name = 'hazard-photos';
  ```

| Setting | Expected & Verified Value | Verification Method | Status |
|---|---|---|:---:|
| **Bucket Exists** | `hazard-photos` | Live Storage Probe (`NoSuchKey`) + SQL Editor | **PASS** |
| **Public Access** | `false` (Private) | Live Public Probe (`NoSuchBucket`) + SQL Editor | **PASS** |
| **File Size Limit** | `5242880` bytes (5 MB) | Manual SQL Editor Verification | **PASS** |
| **Allowed MIME Types** | `image/jpeg`, `image/png`, `image/webp` | Manual SQL Editor Verification | **PASS** |

- **Security Boundary**: Broad public read/write access is disabled. Final authenticated Storage RLS policies belong to Stage 4 (`supabase/policies.sql`). No test or production photos were uploaded.

---

## 6. Database Scope Compliance Verification

Verification confirms that **no SafeRoute application objects exist in the `public` schema yet**:

1. **Application Tables**:
   - Automated live PostgREST probes confirmed all 7 planned SafeRoute tables (`profiles`, `hazard_reports`, `report_images`, `verifications`, `comments`, `escalations`, `status_history`) return `404 PGRST205`.
   - User executed read-only SQL Editor verification confirming zero SafeRoute application tables exist in `public`.
   - **Status**: **PASS**
2. **Application SQL Functions**:
   - Automated live PostgREST probes confirmed all 3 planned RPCs (`reports_in_bounds`, `nearby_reports`, `hazards_near_route`) return `404 PGRST202`.
   - **Status**: **PASS**
3. **Application Triggers**:
   - User executed read-only SQL Editor check confirming no SafeRoute application triggers exist in `public`.
   - **Status**: **PASS**
4. **Application RLS Policies**:
   - User executed read-only SQL Editor check confirming no SafeRoute application RLS policies exist in `public`.
   - **Status**: **PASS**

> [!NOTE]
> These scope checks specifically cover SafeRoute application objects in the inspected `public` schema. Supabase's internal system schemas, tables, triggers, and platform policies remain untouched.

---

## 7. Realtime Pub/Sub Preparation

- **Status**: **`PENDING STAGE 3`**
- **Requirement**: Enable Supabase Realtime publication for `hazard_reports` in Stage 3 immediately after the table is created:
  ```sql
  -- Planned for Stage 3 after hazard_reports is created:
  -- ALTER PUBLICATION supabase_realtime ADD TABLE hazard_reports;
  ```
- No dummy table was created and no Realtime publication changes were made in Stage 2.

---

## 8. Application Environment & Security Verification

- `.env.example` defines only empty placeholders: `SUPABASE_URL=`, `SUPABASE_ANON_KEY=`, `SUPABASE_SERVICE_KEY=`, `VISION_API_KEY=`, `SECRET_KEY=`.
- Local `.env` exists at `D:\SafeRoute-2\.env` and is verified excluded by Git (`git check-ignore .env`).
- Zero live API keys, service-role keys, or passwords appear in tracked files, documentation, Git diffs, or frontend assets.
- Developers must add their own local credentials privately into `.env` (never commit `.env` or expose `SUPABASE_SERVICE_KEY` to templates/JavaScript).

---

## 9. Automated Script Limitations & Verification Log

The read-only helper script `tests/verify_supabase.py` uses only the local publishable key (`sb_publishable_...`) and intentionally reports privileged catalog/dashboard items (`pg_extension`, `storage.buckets` metadata, `pg_trigger`, `pg_policies`, and Auth Redirect URLs) as `NOT VERIFIED` by the script. Those items were verified directly by the project owner in the Supabase Dashboard and SQL Editor as documented in Sections 3–6 above.
