-- ==============================================================================
-- SafeRoute — Stage 3: Core Database Schema (GitHub Issue A2)
-- Reference: docs/SPEC.md Section 7 (Data Model)
--
-- Scope (Stage 3 ONLY):
--   1. PostGIS extension guard
--   2. Seven core tables:
--        - public.profiles
--        - public.hazard_reports
--        - public.report_images
--        - public.verifications
--        - public.comments
--        - public.escalations
--        - public.status_history
--   3. Constraints (PK, FK, UNIQUE, CHECK, NOT NULL, DEFAULT)
--   4. Spatial (GIST), composite B-tree, and foreign-key indexes
--   5. Supabase Auth signup trigger (auth.users -> public.profiles)
--   6. Realtime publication membership for public.hazard_reports
--
-- Out of scope (Stage 4+):
--   Spatial RPC functions (functions.sql), vote/severity/status triggers
--   (triggers.sql), RLS & Storage policies (policies.sql), seed data (seed.sql).
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Required Extensions
-- ------------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgis;

-- ------------------------------------------------------------------------------
-- 2. Tables
-- ------------------------------------------------------------------------------

-- 2.1 profiles
CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    full_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user'
        CONSTRAINT profiles_role_check CHECK (role IN ('user', 'admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2.2 hazard_reports
CREATE TABLE IF NOT EXISTS public.hazard_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporter_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    category TEXT NOT NULL
        CONSTRAINT hazard_reports_category_check
        CHECK (category IN ('pothole', 'road_break', 'streetlight', 'footpath', 'other')),
    description TEXT NOT NULL DEFAULT '',
    location geography(Point, 4326) NOT NULL,
    address_text TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CONSTRAINT hazard_reports_status_check
        CHECK (status IN ('open', 'escalated', 'fixed')),
    escalation_recommended BOOLEAN NOT NULL DEFAULT FALSE,
    severity_score INTEGER NOT NULL DEFAULT 0
        CONSTRAINT hazard_reports_severity_score_check
        CHECK (severity_score >= 0 AND severity_score <= 100),
    confidence TEXT NOT NULL DEFAULT 'low'
        CONSTRAINT hazard_reports_confidence_check
        CHECK (confidence IN ('low', 'medium', 'high')),
    confirm_count INTEGER NOT NULL DEFAULT 0
        CONSTRAINT hazard_reports_confirm_count_check CHECK (confirm_count >= 0),
    dispute_count INTEGER NOT NULL DEFAULT 0
        CONSTRAINT hazard_reports_dispute_count_check CHECK (dispute_count >= 0),
    fixed_count INTEGER NOT NULL DEFAULT 0
        CONSTRAINT hazard_reports_fixed_count_check CHECK (fixed_count >= 0),
    ai_suggested_category TEXT
        CONSTRAINT hazard_reports_ai_suggested_category_check
        CHECK (
            ai_suggested_category IS NULL
            OR ai_suggested_category IN (
                'pothole', 'road_break', 'streetlight', 'footpath', 'other', 'none'
            )
        ),
    ai_confidence DOUBLE PRECISION
        CONSTRAINT hazard_reports_ai_confidence_check
        CHECK (
            ai_confidence IS NULL
            OR (ai_confidence >= 0.0 AND ai_confidence <= 1.0)
        ),
    duplicate_of UUID REFERENCES public.hazard_reports(id) ON DELETE SET NULL,
    is_seed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    escalated_at TIMESTAMPTZ,
    fixed_at TIMESTAMPTZ,
    CONSTRAINT hazard_reports_no_self_duplicate
        CHECK (duplicate_of IS NULL OR duplicate_of <> id)
);

-- 2.3 report_images
CREATE TABLE IF NOT EXISTS public.report_images (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL
        CONSTRAINT report_images_storage_path_not_empty CHECK (BTRIM(storage_path) <> ''),
    kind TEXT NOT NULL DEFAULT 'evidence'
        CONSTRAINT report_images_kind_check CHECK (kind IN ('evidence', 'after')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2.4 verifications
CREATE TABLE IF NOT EXISTS public.verifications (
    report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    vote TEXT NOT NULL
        CONSTRAINT verifications_vote_check CHECK (vote IN ('confirm', 'dispute', 'fixed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT verifications_pkey PRIMARY KEY (report_id, user_id, vote)
);

-- 2.5 comments
CREATE TABLE IF NOT EXISTS public.comments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE,
    user_id UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    body TEXT NOT NULL
        CONSTRAINT comments_body_not_empty CHECK (BTRIM(body) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2.6 escalations
CREATE TABLE IF NOT EXISTS public.escalations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE,
    escalated_by UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    authority_name TEXT NOT NULL
        CONSTRAINT escalations_authority_name_not_empty CHECK (BTRIM(authority_name) <> ''),
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2.7 status_history
CREATE TABLE IF NOT EXISTS public.status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id UUID NOT NULL REFERENCES public.hazard_reports(id) ON DELETE CASCADE,
    old_status TEXT
        CONSTRAINT status_history_old_status_check
        CHECK (old_status IS NULL OR old_status IN ('open', 'escalated', 'fixed')),
    new_status TEXT NOT NULL
        CONSTRAINT status_history_new_status_check
        CHECK (new_status IN ('open', 'escalated', 'fixed')),
    changed_by UUID REFERENCES public.profiles(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ------------------------------------------------------------------------------
-- 3. Indexes
-- ------------------------------------------------------------------------------

-- Required spatial GIST index for bounding-box and route proximity queries
CREATE INDEX IF NOT EXISTS idx_hazard_reports_location_gist
    ON public.hazard_reports USING GIST (location);

-- Required composite B-tree index for filtered hazard listings
CREATE INDEX IF NOT EXISTS idx_hazard_reports_status_category_created
    ON public.hazard_reports (status, category, created_at DESC);

-- Foreign-key and common lookup indexes (non-redundant)
CREATE INDEX IF NOT EXISTS idx_hazard_reports_reporter_id
    ON public.hazard_reports (reporter_id);

CREATE INDEX IF NOT EXISTS idx_hazard_reports_duplicate_of
    ON public.hazard_reports (duplicate_of)
    WHERE duplicate_of IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_report_images_report_id
    ON public.report_images (report_id);

-- Note: verifications_pkey (report_id, user_id, vote) already indexes report_id.
CREATE INDEX IF NOT EXISTS idx_verifications_user_id
    ON public.verifications (user_id);

CREATE INDEX IF NOT EXISTS idx_comments_report_id_created
    ON public.comments (report_id, created_at);

CREATE INDEX IF NOT EXISTS idx_comments_user_id
    ON public.comments (user_id);

CREATE INDEX IF NOT EXISTS idx_escalations_report_id
    ON public.escalations (report_id);

CREATE INDEX IF NOT EXISTS idx_status_history_report_id_created
    ON public.status_history (report_id, created_at);

-- ------------------------------------------------------------------------------
-- 4. Supabase Auth Signup Trigger (auth.users -> public.profiles)
-- ------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_full_name TEXT;
BEGIN
    -- Extract display name safely from user metadata.
    -- Role is strictly hardcoded to 'user' so signup metadata can never elevate privileges.
    v_full_name := COALESCE(
        NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'full_name'), ''),
        NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'name'), ''),
        ''
    );

    INSERT INTO public.profiles (id, full_name, role)
    VALUES (NEW.id, v_full_name, 'user')
    ON CONFLICT (id) DO NOTHING;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.handle_new_user() FROM PUBLIC;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_new_user();

-- ------------------------------------------------------------------------------
-- 5. Supabase Realtime Publication (public.hazard_reports ONLY)
-- ------------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_publication
        WHERE pubname = 'supabase_realtime'
    ) AND NOT EXISTS (
        SELECT 1
        FROM pg_publication_tables
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename = 'hazard_reports'
    ) THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE public.hazard_reports;
    END IF;
END;
$$;
