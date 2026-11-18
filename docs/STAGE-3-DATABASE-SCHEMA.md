# SafeRoute — Stage 3: Database Schema Documentation (Issue A2)

> - **Document Version**: 1.0.0
> - **Milestone**: Day 1 – Core Loop
> - **GitHub Issue**: `A2 · Database schema`
> - **Feature Branch**: `feat/backend-A2-database-schema`
> - **Target Supabase Project**: `myaadekzqtubygwhjmab` (`https://myaadekzqtubygwhjmab.supabase.co`)
> - **SQL Execution Status**: **PENDING** (awaiting execution in Supabase SQL Editor by project owner)

---

## 1. Scope Summary

Implements the complete Section 7 core relational and PostGIS schema in [supabase/schema.sql](file:///D:/SafeRoute-2/supabase/schema.sql), strictly limited to Stage 3:

- **Included**: PostGIS extension guard, 7 core tables, PK/FK/UNIQUE/CHECK/NOT NULL/DEFAULT constraints, spatial GIST and B-tree indexes, `auth.users` signup trigger (`public.handle_new_user()`), and idempotent Realtime publication enrollment for `public.hazard_reports`.
- **Excluded (Reserved for Stage 4+)**: Spatial RPC functions (`functions.sql`), vote/severity/status triggers (`triggers.sql`), RLS & Storage policies (`policies.sql`), and seed data (`seed.sql`).

---

## 2. Tables and Relationships

| # | Table | Primary Key | Foreign Keys & Delete Behavior |
|---|---|---|---|
| 1 | `public.profiles` | `id` (`UUID`) | `id` $\rightarrow$ `auth.users(id)` (`ON DELETE CASCADE`) |
| 2 | `public.hazard_reports` | `id` (`UUID`, `gen_random_uuid()`) | `reporter_id` $\rightarrow$ `public.profiles(id)` (`ON DELETE SET NULL`)<br>`duplicate_of` $\rightarrow$ `public.hazard_reports(id)` (`ON DELETE SET NULL`) |
| 3 | `public.report_images` | `id` (`UUID`, `gen_random_uuid()`) | `report_id` $\rightarrow$ `public.hazard_reports(id)` (`ON DELETE CASCADE`) |
| 4 | `public.verifications` | Composite `(report_id, user_id, vote)` | `report_id` $\rightarrow$ `public.hazard_reports(id)` (`ON DELETE CASCADE`)<br>`user_id` $\rightarrow$ `public.profiles(id)` (`ON DELETE CASCADE`) |
| 5 | `public.comments` | `id` (`UUID`, `gen_random_uuid()`) | `report_id` $\rightarrow$ `public.hazard_reports(id)` (`ON DELETE CASCADE`)<br>`user_id` $\rightarrow$ `public.profiles(id)` (`ON DELETE SET NULL`) |
| 6 | `public.escalations` | `id` (`UUID`, `gen_random_uuid()`) | `report_id` $\rightarrow$ `public.hazard_reports(id)` (`ON DELETE CASCADE`)<br>`escalated_by` $\rightarrow$ `public.profiles(id)` (`ON DELETE SET NULL`) |
| 7 | `public.status_history` | `id` (`UUID`, `gen_random_uuid()`) | `report_id` $\rightarrow$ `public.hazard_reports(id)` (`ON DELETE CASCADE`)<br>`changed_by` $\rightarrow$ `public.profiles(id)` (`ON DELETE SET NULL`) |

### Rationale for Delete Behavior
- Deleting a `hazard_reports` record cascades cleanly to its child records (`report_images`, `verifications`, `comments`, `escalations`, `status_history`).
- Deleting a user (`profiles`) preserves community hazard history (`hazard_reports.reporter_id`, `comments.user_id`, `escalations.escalated_by`, `status_history.changed_by` use `ON DELETE SET NULL`) while cascading the user's votes (`verifications.user_id`).

---

## 3. Constraints and Indexes

### Domain & Integrity Constraints
- **`profiles.role`**: `IN ('user', 'admin')`, default `'user'`.
- **`hazard_reports.category`**: `IN ('pothole', 'road_break', 'streetlight', 'footpath', 'other')`.
- **`hazard_reports.location`**: `geography(Point, 4326) NOT NULL`.
- **`hazard_reports.status`**: `IN ('open', 'escalated', 'fixed')`, default `'open'`.
- **`hazard_reports.severity_score`**: `0 <= severity_score <= 100`, default `0`.
- **`hazard_reports.confidence`**: `IN ('low', 'medium', 'high')`, default `'low'`.
- **`hazard_reports.confirm_count / dispute_count / fixed_count`**: `>= 0`, default `0`.
- **`hazard_reports.ai_suggested_category`**: `NULL` or `IN ('pothole', 'road_break', 'streetlight', 'footpath', 'other', 'none')`.
- **`hazard_reports.ai_confidence`**: `NULL` or `0.0 <= ai_confidence <= 1.0`.
- **`hazard_reports.duplicate_of`**: Self-reference forbidden (`duplicate_of IS NULL OR duplicate_of <> id`).
- **`report_images.kind`**: `IN ('evidence', 'after')`, default `'evidence'`; `storage_path` non-empty.
- **`verifications.vote`**: `IN ('confirm', 'dispute', 'fixed')`; unique `(report_id, user_id, vote)` enforced by composite primary key `verifications_pkey`.
- **`comments.body`** & **`escalations.authority_name`**: Non-empty trimmed text required.
- **`status_history.old_status`**: `NULL` or `IN ('open', 'escalated', 'fixed')`; **`new_status`**: `NOT NULL`, `IN ('open', 'escalated', 'fixed')`.

### Indexes Implemented (Non-Redundant)
- `idx_hazard_reports_location_gist`: **GIST** on `public.hazard_reports (location)`
- `idx_hazard_reports_status_category_created`: **B-tree** on `public.hazard_reports (status, category, created_at DESC)`
- `idx_hazard_reports_reporter_id`: **B-tree** on `public.hazard_reports (reporter_id)`
- `idx_hazard_reports_duplicate_of`: **Partial B-tree** on `public.hazard_reports (duplicate_of) WHERE duplicate_of IS NOT NULL`
- `idx_report_images_report_id`: **B-tree** on `public.report_images (report_id)`
- `idx_verifications_user_id`: **B-tree** on `public.verifications (user_id)` *(leading `report_id` is already covered by `verifications_pkey`)*
- `idx_comments_report_id_created`: **B-tree** on `public.comments (report_id, created_at)`
- `idx_comments_user_id`: **B-tree** on `public.comments (user_id)`
- `idx_escalations_report_id`: **B-tree** on `public.escalations (report_id)`
- `idx_status_history_report_id_created`: **B-tree** on `public.status_history (report_id, created_at)`

---

## 4. Supabase Auth Signup Trigger

- **Function**: `public.handle_new_user()` (`LANGUAGE plpgsql`, `SECURITY DEFINER`, `SET search_path = public`)
- **Trigger**: `on_auth_user_created` (`AFTER INSERT ON auth.users FOR EACH ROW`)
- **Security Guarantees**:
  - Uses `NEW.id` as `public.profiles.id`.
  - Extracts `full_name` safely from `NEW.raw_user_meta_data` (`full_name` fallback to `name`, fallback to `''`).
  - **Hardcodes `role = 'user'`** — never inspects user-supplied metadata for role values, making client-side admin privilege escalation impossible.
  - Uses `ON CONFLICT (id) DO NOTHING` to prevent duplicate-key errors.
  - Executes `REVOKE ALL ON FUNCTION public.handle_new_user() FROM PUBLIC;` to prevent direct unprivileged invocation.

---

## 5. Realtime Configuration

- An idempotent `DO $$ ... END $$;` block checks `pg_publication` and `pg_publication_tables` and runs:
  ```sql
  ALTER PUBLICATION supabase_realtime ADD TABLE public.hazard_reports;
  ```
- Safe to rerun without error; adds **only** `public.hazard_reports`.

---

## 6. Execution Instructions (Supabase SQL Editor)

Because privileged database credentials (`service_role` / DB password) are intentionally kept private and unavailable to the local publishable-key environment, execute `supabase/schema.sql` directly in the Supabase Dashboard:

1. Open the Supabase SQL Editor for project **`myaadekzqtubygwhjmab`**:
   `https://supabase.com/dashboard/project/myaadekzqtubygwhjmab/sql/new`
2. Paste the entire contents of `supabase/schema.sql` and click **Run**.
3. Confirm it finishes with `Success. No rows returned`.
4. Run the read-only verification queries in Section 7 below and record the results.

---

## 7. Live Verification SQL Queries (Run After Executing `schema.sql`)

```sql
-- 1. Verify all 7 tables exist in public
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
      'profiles', 'hazard_reports', 'report_images',
      'verifications', 'comments', 'escalations', 'status_history'
  )
ORDER BY table_name;

-- 2. Verify hazard_reports geography(Point, 4326) column and all 21 columns
SELECT column_name, data_type, udt_name, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'hazard_reports'
ORDER BY ordinal_position;

-- 3. Verify all foreign keys across the 7 tables
SELECT
    tc.table_name,
    kcu.column_name,
    ccu.table_schema AS foreign_table_schema,
    ccu.table_name AS foreign_table_name,
    ccu.column_name AS foreign_column_name,
    rc.delete_rule
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
JOIN information_schema.referential_constraints AS rc
    ON rc.constraint_name = tc.constraint_name
    AND rc.constraint_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema = 'public'
ORDER BY tc.table_name, kcu.column_name;

-- 4. Verify CHECK and UNIQUE / PRIMARY KEY constraints
SELECT
    conrelid::regclass AS table_name,
    conname AS constraint_name,
    contype AS type,
    pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace
  AND contype IN ('c', 'p', 'u')
ORDER BY table_name, contype, conname;

-- 5. Verify GIST, B-tree, and FK indexes
SELECT tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename IN (
      'profiles', 'hazard_reports', 'report_images',
      'verifications', 'comments', 'escalations', 'status_history'
  )
ORDER BY tablename, indexname;

-- 6. Verify signup trigger on auth.users and function security settings
SELECT
    tgname,
    tgrelid::regclass AS target_table,
    tgenabled,
    pg_get_triggerdef(oid) AS trigger_def
FROM pg_trigger
WHERE tgname = 'on_auth_user_created';

SELECT
    proname,
    prosecdef AS is_security_definer,
    proconfig AS config_settings
FROM pg_proc
WHERE pronamespace = 'public'::regnamespace
  AND proname = 'handle_new_user';

-- 7. Verify Realtime publication membership (must show public.hazard_reports ONLY)
SELECT pubname, schemaname, tablename
FROM pg_publication_tables
WHERE pubname = 'supabase_realtime';
```

---

## 8. Current Verification Status Matrix

| Verification Layer | Status | Details |
|---|:---:|---|
| **Static SQL Review** | **PASS** | Verified dependency order, syntax, constraints, indexes, trigger security, and Realtime idempotency. |
| **Local Automated Tests** | **PASS** | `pytest tests/ -v` (Stage 1 health tests + Stage 3 schema validation tests). |
| **Supabase SQL Execution** | **PENDING** | Awaiting project owner execution of `supabase/schema.sql` in Supabase SQL Editor (`myaadekzqtubygwhjmab`). |
| **Live Supabase DB Verification** | **PENDING** | Awaiting execution of Section 7 read-only queries and live signup trigger check after SQL run. |
