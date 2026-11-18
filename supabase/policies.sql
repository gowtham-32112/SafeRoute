-- ==============================================================================
-- SafeRoute — Stage 4: Row-Level Security & Storage Policies (Issue A5)
-- Reference: docs/SPEC.md Section 3, Section 7 & Stage 4 Security Requirements
--
-- Prerequisites:
--   - supabase/schema.sql (Stage 3) already applied
--   - Private storage bucket 'hazard-photos' exists (Stage 2)
--
-- Security Model:
--   - RLS enabled on all 7 public application tables.
--   - Anonymous (anon) users have ZERO read/write access to application tables
--     or the private 'hazard-photos' storage bucket.
--   - Narrowly scoped SECURITY DEFINER helpers (is_admin, current_user_role)
--     avoid RLS recursion on public.profiles while fixing search_path = public.
--   - Storage upload path convention enforced:
--       <auth.uid()>/<report_id_uuid>/<filename>
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Narrowly Scoped RLS Helper Functions (Zero-Parameter, Recursion-Safe)
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.profiles
        WHERE id = auth.uid()
          AND role = 'admin'
    );
$$;

REVOKE ALL ON FUNCTION public.is_admin() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.is_admin() TO authenticated, service_role;

CREATE OR REPLACE FUNCTION public.current_user_role()
RETURNS TEXT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT p.role
    FROM public.profiles AS p
    WHERE p.id = auth.uid();
$$;

REVOKE ALL ON FUNCTION public.current_user_role() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.current_user_role() TO authenticated, service_role;

-- ------------------------------------------------------------------------------
-- 2. Enable Row-Level Security on All Seven Public Tables
-- ------------------------------------------------------------------------------
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.hazard_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.report_images ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.comments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.status_history ENABLE ROW LEVEL SECURITY;

-- ------------------------------------------------------------------------------
-- 3. Table Policies: public.profiles
-- ------------------------------------------------------------------------------
DROP POLICY IF EXISTS profiles_select_authenticated ON public.profiles;
CREATE POLICY profiles_select_authenticated
    ON public.profiles
    FOR SELECT
    TO authenticated
    USING (TRUE);

DROP POLICY IF EXISTS profiles_update_own ON public.profiles;
CREATE POLICY profiles_update_own
    ON public.profiles
    FOR UPDATE
    TO authenticated
    USING (id = auth.uid())
    WITH CHECK (
        id = auth.uid()
        AND role = public.current_user_role()
    );

DROP POLICY IF EXISTS profiles_update_admin ON public.profiles;
CREATE POLICY profiles_update_admin
    ON public.profiles
    FOR UPDATE
    TO authenticated
    USING (public.is_admin())
    WITH CHECK (public.is_admin());

-- Note: No INSERT or DELETE policy for ordinary users on public.profiles.
-- Profile rows are created automatically by auth.users trigger handle_new_user().

-- ------------------------------------------------------------------------------
-- 4. Table Policies: public.hazard_reports
-- ------------------------------------------------------------------------------
DROP POLICY IF EXISTS hazard_reports_select_authenticated ON public.hazard_reports;
CREATE POLICY hazard_reports_select_authenticated
    ON public.hazard_reports
    FOR SELECT
    TO authenticated
    USING (TRUE);

DROP POLICY IF EXISTS hazard_reports_insert_own ON public.hazard_reports;
CREATE POLICY hazard_reports_insert_own
    ON public.hazard_reports
    FOR INSERT
    TO authenticated
    WITH CHECK (
        reporter_id = auth.uid()
        AND status = 'open'
        AND is_seed = FALSE
        AND escalated_at IS NULL
        AND fixed_at IS NULL
        AND ai_confidence IS NULL
        AND ai_suggested_category IS NULL
        AND duplicate_of IS NULL
    );

DROP POLICY IF EXISTS hazard_reports_insert_admin ON public.hazard_reports;
CREATE POLICY hazard_reports_insert_admin
    ON public.hazard_reports
    FOR INSERT
    TO authenticated
    WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS hazard_reports_update_own_open ON public.hazard_reports;
CREATE POLICY hazard_reports_update_own_open
    ON public.hazard_reports
    FOR UPDATE
    TO authenticated
    USING (
        reporter_id = auth.uid()
        AND status = 'open'
    )
    WITH CHECK (
        reporter_id = auth.uid()
        AND status = 'open'
    );

DROP POLICY IF EXISTS hazard_reports_update_admin ON public.hazard_reports;
CREATE POLICY hazard_reports_update_admin
    ON public.hazard_reports
    FOR UPDATE
    TO authenticated
    USING (public.is_admin())
    WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS hazard_reports_delete_own_open ON public.hazard_reports;
CREATE POLICY hazard_reports_delete_own_open
    ON public.hazard_reports
    FOR DELETE
    TO authenticated
    USING (
        reporter_id = auth.uid()
        AND status = 'open'
    );

DROP POLICY IF EXISTS hazard_reports_delete_admin ON public.hazard_reports;
CREATE POLICY hazard_reports_delete_admin
    ON public.hazard_reports
    FOR DELETE
    TO authenticated
    USING (public.is_admin());

-- ------------------------------------------------------------------------------
-- 5. Table Policies: public.report_images
-- ------------------------------------------------------------------------------
DROP POLICY IF EXISTS report_images_select_authenticated ON public.report_images;
CREATE POLICY report_images_select_authenticated
    ON public.report_images
    FOR SELECT
    TO authenticated
    USING (TRUE);

-- Users can attach images only to their own open reports; admins can attach to any report.
DROP POLICY IF EXISTS report_images_insert_owner_or_admin ON public.report_images;
CREATE POLICY report_images_insert_owner_or_admin
    ON public.report_images
    FOR INSERT
    TO authenticated
    WITH CHECK (
        public.is_admin()
        OR EXISTS (
            SELECT 1
            FROM public.hazard_reports AS hr
            WHERE hr.id = report_id
              AND hr.reporter_id = auth.uid()
              AND hr.status = 'open'
        )
    );

DROP POLICY IF EXISTS report_images_delete_owner_or_admin ON public.report_images;
CREATE POLICY report_images_delete_owner_or_admin
    ON public.report_images
    FOR DELETE
    TO authenticated
    USING (
        public.is_admin()
        OR EXISTS (
            SELECT 1
            FROM public.hazard_reports AS hr
            WHERE hr.id = report_id
              AND hr.reporter_id = auth.uid()
              AND hr.status = 'open'
        )
    );

-- ------------------------------------------------------------------------------
-- 6. Table Policies: public.verifications
-- ------------------------------------------------------------------------------
DROP POLICY IF EXISTS verifications_select_authenticated ON public.verifications;
CREATE POLICY verifications_select_authenticated
    ON public.verifications
    FOR SELECT
    TO authenticated
    USING (TRUE);

DROP POLICY IF EXISTS verifications_insert_own_non_owner ON public.verifications;
CREATE POLICY verifications_insert_own_non_owner
    ON public.verifications
    FOR INSERT
    TO authenticated
    WITH CHECK (
        user_id = auth.uid()
        AND vote IN ('confirm', 'dispute', 'fixed')
        AND EXISTS (
            SELECT 1
            FROM public.hazard_reports AS hr
            WHERE hr.id = report_id
              AND (hr.reporter_id IS NULL OR hr.reporter_id <> auth.uid())
        )
    );

DROP POLICY IF EXISTS verifications_delete_own_or_admin ON public.verifications;
CREATE POLICY verifications_delete_own_or_admin
    ON public.verifications
    FOR DELETE
    TO authenticated
    USING (
        user_id = auth.uid()
        OR public.is_admin()
    );

-- Note: No UPDATE policy on public.verifications; votes are immutable (delete & re-insert).

-- ------------------------------------------------------------------------------
-- 7. Table Policies: public.comments
-- ------------------------------------------------------------------------------
DROP POLICY IF EXISTS comments_select_authenticated ON public.comments;
CREATE POLICY comments_select_authenticated
    ON public.comments
    FOR SELECT
    TO authenticated
    USING (TRUE);

DROP POLICY IF EXISTS comments_insert_own ON public.comments;
CREATE POLICY comments_insert_own
    ON public.comments
    FOR INSERT
    TO authenticated
    WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS comments_update_own ON public.comments;
CREATE POLICY comments_update_own
    ON public.comments
    FOR UPDATE
    TO authenticated
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS comments_delete_own_or_admin ON public.comments;
CREATE POLICY comments_delete_own_or_admin
    ON public.comments
    FOR DELETE
    TO authenticated
    USING (
        user_id = auth.uid()
        OR public.is_admin()
    );

-- ------------------------------------------------------------------------------
-- 8. Table Policies: public.escalations
-- ------------------------------------------------------------------------------
DROP POLICY IF EXISTS escalations_select_authenticated ON public.escalations;
CREATE POLICY escalations_select_authenticated
    ON public.escalations
    FOR SELECT
    TO authenticated
    USING (TRUE);

DROP POLICY IF EXISTS escalations_insert_admin ON public.escalations;
CREATE POLICY escalations_insert_admin
    ON public.escalations
    FOR INSERT
    TO authenticated
    WITH CHECK (
        public.is_admin()
        AND escalated_by = auth.uid()
    );

DROP POLICY IF EXISTS escalations_update_admin ON public.escalations;
CREATE POLICY escalations_update_admin
    ON public.escalations
    FOR UPDATE
    TO authenticated
    USING (public.is_admin())
    WITH CHECK (public.is_admin());

DROP POLICY IF EXISTS escalations_delete_admin ON public.escalations;
CREATE POLICY escalations_delete_admin
    ON public.escalations
    FOR DELETE
    TO authenticated
    USING (public.is_admin());

-- ------------------------------------------------------------------------------
-- 9. Table Policies: public.status_history
-- Append-only audit log: ordinary users can read only; writes come from trusted
-- SECURITY DEFINER trigger log_hazard_status_change() or authorized admins.
-- ------------------------------------------------------------------------------
DROP POLICY IF EXISTS status_history_select_authenticated ON public.status_history;
CREATE POLICY status_history_select_authenticated
    ON public.status_history
    FOR SELECT
    TO authenticated
    USING (TRUE);

DROP POLICY IF EXISTS status_history_insert_admin ON public.status_history;
CREATE POLICY status_history_insert_admin
    ON public.status_history
    FOR INSERT
    TO authenticated
    WITH CHECK (
        public.is_admin()
        AND changed_by = auth.uid()
    );

-- Note: No UPDATE or DELETE policies on public.status_history to keep audit history immutable.

-- ------------------------------------------------------------------------------
-- 10. Supabase Storage Policies (storage.objects — Private Bucket: hazard-photos)
-- Enforced path convention:
--   <auth.uid()>/<report_id_uuid>/<filename>
--
-- Where storage.foldername(name) returns 1-indexed segments:
--   [1] = uploader UUID (auth.uid()::text)
--   [2] = report UUID
-- ------------------------------------------------------------------------------

DROP POLICY IF EXISTS hazard_photos_select_authenticated ON storage.objects;
CREATE POLICY hazard_photos_select_authenticated
    ON storage.objects
    FOR SELECT
    TO authenticated
    USING (
        bucket_id = 'hazard-photos'
        AND (
            public.is_admin()
            OR (storage.foldername(name))[1] = auth.uid()::TEXT
            OR (
                array_length(storage.foldername(name), 1) = 2
                AND EXISTS (
                    SELECT 1
                    FROM public.report_images AS ri
                    WHERE ri.storage_path = name
                )
            )
        )
    );

DROP POLICY IF EXISTS hazard_photos_insert_own_path ON storage.objects;
CREATE POLICY hazard_photos_insert_own_path
    ON storage.objects
    FOR INSERT
    TO authenticated
    WITH CHECK (
        bucket_id = 'hazard-photos'
        AND array_length(storage.foldername(name), 1) = 2
        AND (storage.foldername(name))[1] = auth.uid()::TEXT
        AND (storage.foldername(name))[2] ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND name NOT LIKE '%..%'
    );

DROP POLICY IF EXISTS hazard_photos_update_own_or_admin ON storage.objects;
CREATE POLICY hazard_photos_update_own_or_admin
    ON storage.objects
    FOR UPDATE
    TO authenticated
    USING (
        bucket_id = 'hazard-photos'
        AND (
            (storage.foldername(name))[1] = auth.uid()::TEXT
            OR public.is_admin()
        )
    )
    WITH CHECK (
        bucket_id = 'hazard-photos'
        AND array_length(storage.foldername(name), 1) = 2
        AND (
            (storage.foldername(name))[1] = auth.uid()::TEXT
            OR public.is_admin()
        )
        AND (storage.foldername(name))[2] ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND name NOT LIKE '%..%'
    );

DROP POLICY IF EXISTS hazard_photos_delete_own_or_admin ON storage.objects;
CREATE POLICY hazard_photos_delete_own_or_admin
    ON storage.objects
    FOR DELETE
    TO authenticated
    USING (
        bucket_id = 'hazard-photos'
        AND (
            (storage.foldername(name))[1] = auth.uid()::TEXT
            OR public.is_admin()
        )
    );
