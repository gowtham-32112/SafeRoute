-- ==============================================================================
-- SafeRoute — Stage 4: Database Triggers & Business Logic (Issue A4)
-- Reference: docs/SPEC.md Section 7 & Section 8
--
-- Prerequisites:
--   - supabase/schema.sql (Stage 3) already applied
--
-- Trigger Execution Order & Dependencies:
--   1. public.profiles BEFORE UPDATE:
--        - trg_profiles_prevent_role_escalation
--   2. public.verifications BEFORE INSERT OR UPDATE:
--        - trg_verifications_prevent_self_vote
--   3. public.verifications AFTER INSERT OR DELETE:
--        - trg_verifications_recompute_metrics -> recompute_report_metrics()
--   4. public.report_images AFTER INSERT OR DELETE:
--        - trg_report_images_recompute_metrics -> recompute_report_metrics()
--   5. public.hazard_reports BEFORE INSERT OR UPDATE:
--        - trg_hazard_reports_before_write (guarded against recursion via
--          transaction-local setting saferoute.internal_metrics_update)
--   6. public.hazard_reports AFTER UPDATE OF status:
--        - trg_hazard_reports_log_status_history (fires only when status changes)
--
-- Security & Concurrency:
--   - recompute_report_metrics() locks the target hazard_reports row FOR UPDATE
--     to serialize concurrent verification updates safely.
--   - All SECURITY DEFINER functions fix search_path and revoke PUBLIC execute.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Shared Admin Role Helper (Idempotent with supabase/policies.sql)
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

-- ------------------------------------------------------------------------------
-- 2. Pure Business Logic Helpers: Severity Score & Confidence
-- ------------------------------------------------------------------------------

-- Exact approved severity formula (docs/SPEC.md Section 8):
--   Base weights: pothole=40, road_break=40, footpath=35, streetlight=25, other=15
--   Evidence:     photo (+10), ai_confidence > 0.7 (+10)
--   Verification: +8 per confirm (max +24), -10 per dispute
--   Recency:      <= 7 days (+10), 8..30 days (+5), > 30 days (+0)
--   Clamped to [0, 100].
CREATE OR REPLACE FUNCTION public.calculate_severity_score(
    p_category TEXT,
    p_has_photo BOOLEAN,
    p_ai_confidence DOUBLE PRECISION,
    p_confirm_count INTEGER,
    p_dispute_count INTEGER,
    p_created_at TIMESTAMPTZ
)
RETURNS INTEGER
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    v_type_weight INTEGER;
    v_evidence INTEGER := 0;
    v_verification INTEGER := 0;
    v_recency INTEGER := 0;
    v_age INTERVAL;
    v_raw INTEGER;
BEGIN
    v_type_weight := CASE p_category
        WHEN 'pothole' THEN 40
        WHEN 'road_break' THEN 40
        WHEN 'footpath' THEN 35
        WHEN 'streetlight' THEN 25
        WHEN 'other' THEN 15
        ELSE 15
    END;

    IF COALESCE(p_has_photo, FALSE) THEN
        v_evidence := v_evidence + 10;
    END IF;

    IF p_ai_confidence IS NOT NULL AND p_ai_confidence > 0.7 THEN
        v_evidence := v_evidence + 10;
    END IF;

    v_verification := LEAST(GREATEST(COALESCE(p_confirm_count, 0), 0) * 8, 24)
                      - (GREATEST(COALESCE(p_dispute_count, 0), 0) * 10);

    v_age := NOW() - COALESCE(p_created_at, NOW());
    IF v_age <= INTERVAL '7 days' THEN
        v_recency := 10;
    ELSIF v_age <= INTERVAL '30 days' THEN
        v_recency := 5;
    ELSE
        v_recency := 0;
    END IF;

    v_raw := v_type_weight + v_evidence + v_verification + v_recency;
    RETURN GREATEST(0, LEAST(100, v_raw));
END;
$$;

REVOKE ALL ON FUNCTION public.calculate_severity_score(
    TEXT, BOOLEAN, DOUBLE PRECISION, INTEGER, INTEGER, TIMESTAMPTZ
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.calculate_severity_score(
    TEXT, BOOLEAN, DOUBLE PRECISION, INTEGER, INTEGER, TIMESTAMPTZ
) TO authenticated, service_role;

-- Exact approved confidence rule (docs/SPEC.md Section 8):
--   Low:    0 confirmations
--   Medium: 1..2 confirmations (or >= 3 when not strictly exceeding disputes)
--   High:   >= 3 confirmations AND confirmations > disputes
CREATE OR REPLACE FUNCTION public.calculate_confidence(
    p_confirm_count INTEGER,
    p_dispute_count INTEGER
)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    v_confirms INTEGER := GREATEST(COALESCE(p_confirm_count, 0), 0);
    v_disputes INTEGER := GREATEST(COALESCE(p_dispute_count, 0), 0);
BEGIN
    IF v_confirms >= 3 AND v_confirms > v_disputes THEN
        RETURN 'high';
    ELSIF v_confirms >= 1 THEN
        RETURN 'medium';
    ELSE
        RETURN 'low';
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.calculate_confidence(INTEGER, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.calculate_confidence(INTEGER, INTEGER)
    TO authenticated, service_role;

-- ------------------------------------------------------------------------------
-- 3. Transaction-Safe Report Metrics Recompute Function
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.recompute_report_metrics(
    p_report_id UUID,
    p_trigger_op TEXT DEFAULT NULL,
    p_trigger_vote TEXT DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
DECLARE
    v_report public.hazard_reports%ROWTYPE;
    v_confirm_count INTEGER := 0;
    v_dispute_count INTEGER := 0;
    v_fixed_count INTEGER := 0;
    v_has_photo BOOLEAN := FALSE;
    v_confidence TEXT;
    v_severity_score INTEGER;
    v_escalation_recommended BOOLEAN;
    v_new_status TEXT;
    v_fixed_at TIMESTAMPTZ;
BEGIN
    -- Lock the parent report row to prevent concurrent verification race conditions
    SELECT *
    INTO v_report
    FROM public.hazard_reports
    WHERE id = p_report_id
    FOR UPDATE;

    -- Report may have been cascade-deleted in the same transaction
    IF NOT FOUND THEN
        RETURN;
    END IF;

    -- Recalculate distinct eligible user counts (excluding reporter self-votes)
    SELECT
        COALESCE(COUNT(DISTINCT v.user_id) FILTER (WHERE v.vote = 'confirm'), 0)::INTEGER,
        COALESCE(COUNT(DISTINCT v.user_id) FILTER (WHERE v.vote = 'dispute'), 0)::INTEGER,
        COALESCE(COUNT(DISTINCT v.user_id) FILTER (WHERE v.vote = 'fixed'), 0)::INTEGER
    INTO v_confirm_count, v_dispute_count, v_fixed_count
    FROM public.verifications AS v
    WHERE v.report_id = p_report_id
      AND (v_report.reporter_id IS NULL OR v.user_id <> v_report.reporter_id);

    -- Check for evidence photo attachment
    SELECT EXISTS (
        SELECT 1
        FROM public.report_images AS ri
        WHERE ri.report_id = p_report_id
          AND ri.kind = 'evidence'
    ) INTO v_has_photo;

    v_confidence := public.calculate_confidence(v_confirm_count, v_dispute_count);
    v_severity_score := public.calculate_severity_score(
        v_report.category,
        v_has_photo,
        v_report.ai_confidence,
        v_confirm_count,
        v_dispute_count,
        v_report.created_at
    );
    v_escalation_recommended := (v_confirm_count >= 5 OR v_severity_score >= 70);

    -- Apply fixed-report threshold rule (2 distinct eligible 'fixed' votes)
    v_new_status := v_report.status;
    v_fixed_at := v_report.fixed_at;

    IF v_fixed_count >= 2 THEN
        IF v_new_status <> 'fixed' THEN
            v_new_status := 'fixed';
            v_fixed_at := COALESCE(v_report.fixed_at, NOW());
        END IF;
    ELSIF p_trigger_op = 'DELETE'
          AND p_trigger_vote = 'fixed'
          AND v_report.status = 'fixed' THEN
        -- Threshold no longer met after a 'fixed' vote deletion:
        -- revert to 'escalated' if previously escalated, otherwise 'open'.
        v_new_status := CASE
            WHEN v_report.escalated_at IS NOT NULL THEN 'escalated'
            ELSE 'open'
        END;
        v_fixed_at := NULL;
    END IF;

    -- Guard against recursive BEFORE UPDATE trigger execution
    PERFORM set_config('saferoute.internal_metrics_update', 'on', TRUE);
    BEGIN
        UPDATE public.hazard_reports
        SET
            confirm_count = v_confirm_count,
            dispute_count = v_dispute_count,
            fixed_count = v_fixed_count,
            confidence = v_confidence,
            severity_score = v_severity_score,
            escalation_recommended = v_escalation_recommended,
            status = v_new_status,
            fixed_at = v_fixed_at,
            updated_at = NOW()
        WHERE id = p_report_id;
    EXCEPTION WHEN OTHERS THEN
        PERFORM set_config('saferoute.internal_metrics_update', 'off', TRUE);
        RAISE;
    END;
    PERFORM set_config('saferoute.internal_metrics_update', 'off', TRUE);
END;
$$;

REVOKE ALL ON FUNCTION public.recompute_report_metrics(UUID, TEXT, TEXT) FROM PUBLIC;

-- ------------------------------------------------------------------------------
-- 4. Trigger: Prevent Self-Voting & Vote Impersonation on public.verifications
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.prevent_self_verification()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_reporter_id UUID;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.report_id IS DISTINCT FROM OLD.report_id
           OR NEW.user_id IS DISTINCT FROM OLD.user_id THEN
            RAISE EXCEPTION 'Cannot reassign report_id or user_id on an existing verification'
                USING ERRCODE = '42501';
        END IF;
    END IF;

    SELECT hr.reporter_id
    INTO v_reporter_id
    FROM public.hazard_reports AS hr
    WHERE hr.id = NEW.report_id;

    IF v_reporter_id IS NOT NULL AND v_reporter_id = NEW.user_id THEN
        RAISE EXCEPTION 'Users cannot vote on their own hazard report'
            USING ERRCODE = '42501';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.prevent_self_verification() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_verifications_prevent_self_vote ON public.verifications;
CREATE TRIGGER trg_verifications_prevent_self_vote
    BEFORE INSERT OR UPDATE ON public.verifications
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_self_verification();

-- ------------------------------------------------------------------------------
-- 5. Trigger: Recompute Metrics on public.verifications INSERT / DELETE
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_verification_change()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM public.recompute_report_metrics(NEW.report_id, TG_OP, NEW.vote);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM public.recompute_report_metrics(OLD.report_id, TG_OP, OLD.vote);
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION public.handle_verification_change() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_verifications_recompute_metrics ON public.verifications;
CREATE TRIGGER trg_verifications_recompute_metrics
    AFTER INSERT OR DELETE ON public.verifications
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_verification_change();

-- ------------------------------------------------------------------------------
-- 6. Trigger: Recompute Metrics on Evidence Photo INSERT / DELETE
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_report_image_change()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.kind = 'evidence' THEN
        PERFORM public.recompute_report_metrics(NEW.report_id, TG_OP, NULL);
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' AND OLD.kind = 'evidence' THEN
        PERFORM public.recompute_report_metrics(OLD.report_id, TG_OP, NULL);
        RETURN OLD;
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

REVOKE ALL ON FUNCTION public.handle_report_image_change() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_report_images_recompute_metrics ON public.report_images;
CREATE TRIGGER trg_report_images_recompute_metrics
    AFTER INSERT OR DELETE ON public.report_images
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_report_image_change();

-- ------------------------------------------------------------------------------
-- 7. Trigger: Hazard Reports BEFORE INSERT / UPDATE (Scoring & Field Protection)
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_hazard_report_before_write()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, extensions
AS $$
DECLARE
    v_is_internal BOOLEAN;
    v_is_regular_user BOOLEAN;
    v_has_photo BOOLEAN := FALSE;
BEGIN
    v_is_internal := (current_setting('saferoute.internal_metrics_update', TRUE) = 'on');
    v_is_regular_user := (auth.uid() IS NOT NULL AND NOT public.is_admin());

    IF TG_OP = 'INSERT' THEN
        NEW.created_at := COALESCE(NEW.created_at, NOW());
        NEW.updated_at := NOW();

        IF v_is_regular_user THEN
            -- Ordinary users cannot forge system or admin-controlled fields on insert
            NEW.status := 'open';
            NEW.confirm_count := 0;
            NEW.dispute_count := 0;
            NEW.fixed_count := 0;
            NEW.confidence := 'low';
            NEW.escalated_at := NULL;
            NEW.fixed_at := NULL;
            NEW.is_seed := FALSE;
        END IF;

        NEW.severity_score := public.calculate_severity_score(
            NEW.category,
            FALSE,
            NEW.ai_confidence,
            NEW.confirm_count,
            NEW.dispute_count,
            NEW.created_at
        );
        NEW.confidence := public.calculate_confidence(NEW.confirm_count, NEW.dispute_count);
        NEW.escalation_recommended := (
            NEW.confirm_count >= 5 OR NEW.severity_score >= 70
        );

        IF NEW.status = 'escalated' THEN
            NEW.escalated_at := COALESCE(NEW.escalated_at, NOW());
        ELSIF NEW.status = 'fixed' THEN
            NEW.fixed_at := COALESCE(NEW.fixed_at, NOW());
        END IF;

        RETURN NEW;
    END IF;

    -- TG_OP = 'UPDATE'
    NEW.updated_at := NOW();

    -- Skip recursion when invoked internally by recompute_report_metrics()
    IF v_is_internal THEN
        RETURN NEW;
    END IF;

    -- Block ordinary users from modifying system- or admin-controlled columns
    IF v_is_regular_user THEN
        IF NEW.reporter_id IS DISTINCT FROM OLD.reporter_id
           OR NEW.status IS DISTINCT FROM OLD.status
           OR NEW.escalation_recommended IS DISTINCT FROM OLD.escalation_recommended
           OR NEW.severity_score IS DISTINCT FROM OLD.severity_score
           OR NEW.confidence IS DISTINCT FROM OLD.confidence
           OR NEW.confirm_count IS DISTINCT FROM OLD.confirm_count
           OR NEW.dispute_count IS DISTINCT FROM OLD.dispute_count
           OR NEW.fixed_count IS DISTINCT FROM OLD.fixed_count
           OR NEW.escalated_at IS DISTINCT FROM OLD.escalated_at
           OR NEW.fixed_at IS DISTINCT FROM OLD.fixed_at
           OR NEW.is_seed IS DISTINCT FROM OLD.is_seed
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'Ordinary users cannot modify system- or admin-controlled report fields'
                USING ERRCODE = '42501';
        END IF;
    END IF;

    -- Recalculate severity & escalation recommendation if scoring inputs changed
    IF NEW.category IS DISTINCT FROM OLD.category
       OR NEW.ai_confidence IS DISTINCT FROM OLD.ai_confidence
       OR NEW.confirm_count IS DISTINCT FROM OLD.confirm_count
       OR NEW.dispute_count IS DISTINCT FROM OLD.dispute_count
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        SELECT EXISTS (
            SELECT 1
            FROM public.report_images AS ri
            WHERE ri.report_id = NEW.id
              AND ri.kind = 'evidence'
        ) INTO v_has_photo;

        NEW.severity_score := public.calculate_severity_score(
            NEW.category,
            v_has_photo,
            NEW.ai_confidence,
            NEW.confirm_count,
            NEW.dispute_count,
            NEW.created_at
        );
        NEW.confidence := public.calculate_confidence(NEW.confirm_count, NEW.dispute_count);
        NEW.escalation_recommended := (
            NEW.confirm_count >= 5 OR NEW.severity_score >= 70
        );
    END IF;

    -- Maintain status transition timestamps
    IF NEW.status = 'escalated' AND OLD.status IS DISTINCT FROM 'escalated' THEN
        NEW.escalated_at := COALESCE(NEW.escalated_at, NOW());
    END IF;

    IF NEW.status = 'fixed' AND OLD.status IS DISTINCT FROM 'fixed' THEN
        NEW.fixed_at := COALESCE(NEW.fixed_at, NOW());
    ELSIF NEW.status <> 'fixed' AND OLD.status = 'fixed' THEN
        NEW.fixed_at := NULL;
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.handle_hazard_report_before_write() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_hazard_reports_before_write ON public.hazard_reports;
CREATE TRIGGER trg_hazard_reports_before_write
    BEFORE INSERT OR UPDATE ON public.hazard_reports
    FOR EACH ROW
    EXECUTE FUNCTION public.handle_hazard_report_before_write();

-- ------------------------------------------------------------------------------
-- 8. Trigger: Automatic Status History Logging on public.hazard_reports
-- Fires strictly when status changes; prevents duplicate entries and forgery.
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.log_hazard_status_change()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_actor_id UUID;
    v_changed_by UUID := NULL;
BEGIN
    IF OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;

    v_actor_id := auth.uid();
    IF v_actor_id IS NOT NULL THEN
        SELECT p.id
        INTO v_changed_by
        FROM public.profiles AS p
        WHERE p.id = v_actor_id;
    END IF;

    INSERT INTO public.status_history (
        report_id,
        old_status,
        new_status,
        changed_by,
        created_at
    )
    VALUES (
        NEW.id,
        OLD.status,
        NEW.status,
        v_changed_by,
        NOW()
    );

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.log_hazard_status_change() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_hazard_reports_log_status_history ON public.hazard_reports;
CREATE TRIGGER trg_hazard_reports_log_status_history
    AFTER UPDATE OF status ON public.hazard_reports
    FOR EACH ROW
    WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION public.log_hazard_status_change();

-- ------------------------------------------------------------------------------
-- 9. Trigger: Prevent Self-Promotion to Admin on public.profiles
-- Defense-in-depth alongside Row-Level Security policies.
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.prevent_profile_role_escalation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NEW.role IS DISTINCT FROM OLD.role THEN
        IF auth.uid() IS NOT NULL AND NOT public.is_admin() THEN
            RAISE EXCEPTION 'Only administrators may change profile roles'
                USING ERRCODE = '42501';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.prevent_profile_role_escalation() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_profiles_prevent_role_escalation ON public.profiles;
CREATE TRIGGER trg_profiles_prevent_role_escalation
    BEFORE UPDATE ON public.profiles
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_profile_role_escalation();
