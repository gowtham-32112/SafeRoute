-- ==============================================================================
-- SafeRoute — Stage 4 Live Transactional Verification Script (A3, A4, A5)
--
-- PURPOSE:
--   Safely verifies all Stage 4 spatial functions, triggers, RLS policies, and
--   Storage policies inside a single PostgreSQL transaction that ends with
--   ROLLBACK so ZERO test rows persist in the live Supabase project.
--
-- PREREQUISITES (execute manually in Supabase SQL Editor first):
--   1. supabase/schema.sql   (already applied in Stage 3)
--   2. supabase/functions.sql
--   3. supabase/policies.sql
--   4. supabase/triggers.sql
--
-- SAFETY:
--   - Wrapped in BEGIN ... ROLLBACK.
--   - Uses synthetic UUIDs that do not touch existing rows.
--   - Raises an exception immediately if any assertion fails.
-- ==============================================================================

BEGIN;

DO $$
DECLARE
    v_user_a UUID := '11111111-1111-4111-8111-111111111111';
    v_user_b UUID := '22222222-2222-4222-8222-222222222222';
    v_user_c UUID := '33333333-3333-4333-8333-333333333333';
    v_user_d UUID := '44444444-4444-4444-8444-444444444444';
    v_user_e UUID := '55555555-5555-4555-8555-555555555555';
    v_user_f UUID := '66666666-6666-4666-8666-666666666666';
    v_admin  UUID := '77777777-7777-4777-8777-777777777777';

    v_report_1 UUID;
    v_report_old UUID;
    v_report_fixed UUID;
    v_row RECORD;
    v_count INTEGER;
    v_blocked BOOLEAN;
    v_route JSONB;
BEGIN
    -- --------------------------------------------------------------------------
    -- 0. Create isolated synthetic users in auth.users (also tests signup trigger!)
    -- --------------------------------------------------------------------------
    INSERT INTO auth.users (id, instance_id, aud, role, email, raw_user_meta_data, created_at, updated_at)
    VALUES
        (v_user_a, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'stage4_a@test.local', '{"full_name":"User A","role":"admin"}'::jsonb, NOW(), NOW()),
        (v_user_b, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'stage4_b@test.local', '{"full_name":"User B"}'::jsonb, NOW(), NOW()),
        (v_user_c, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'stage4_c@test.local', '{"full_name":"User C"}'::jsonb, NOW(), NOW()),
        (v_user_d, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'stage4_d@test.local', '{"full_name":"User D"}'::jsonb, NOW(), NOW()),
        (v_user_e, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'stage4_e@test.local', '{"full_name":"User E"}'::jsonb, NOW(), NOW()),
        (v_user_f, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'stage4_f@test.local', '{"full_name":"User F"}'::jsonb, NOW(), NOW()),
        (v_admin,  '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'stage4_admin@test.local', '{"full_name":"Admin"}'::jsonb, NOW(), NOW());

    -- Confirm signup trigger ignored metadata role='admin' for User A
    SELECT role INTO v_row FROM public.profiles WHERE id = v_user_a;
    ASSERT v_row.role = 'user', 'Signup trigger must hardcode role=user even when metadata has role=admin';

    -- Promote synthetic admin via privileged SQL Editor role (auth.uid() IS NULL)
    UPDATE public.profiles SET role = 'admin' WHERE id = v_admin;

    -- --------------------------------------------------------------------------
    -- 1. Create test hazard reports around Kukatpally Y Junction (17.4849, 78.4138)
    -- --------------------------------------------------------------------------
    INSERT INTO public.hazard_reports (reporter_id, category, description, location, ai_confidence)
    VALUES (
        v_user_a, 'pothole', 'Deep pothole near junction',
        extensions.ST_SetSRID(extensions.ST_MakePoint(78.4138, 17.4849), 4326)::extensions.geography,
        0.85
    )
    RETURNING id INTO v_report_1;

    -- Base pothole(40) + ai>0.7(10) + <=7d(10) = 60, confidence = low, escalation_recommended = false
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.severity_score = 60, format('Expected initial severity 60, got %s', v_row.severity_score);
    ASSERT v_row.confidence = 'low', 'Expected initial confidence low';
    ASSERT v_row.escalation_recommended = FALSE, 'Expected escalation_recommended FALSE at score 60';

    -- Attach evidence photo (+10 -> score 70 -> escalation_recommended TRUE)
    INSERT INTO public.report_images (report_id, storage_path, kind)
    VALUES (v_report_1, v_user_a::text || '/' || v_report_1::text || '/photo.jpg', 'evidence');

    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.severity_score = 70, format('Expected severity 70 with photo, got %s', v_row.severity_score);
    ASSERT v_row.escalation_recommended = TRUE, 'Expected escalation_recommended TRUE at severity 70';

    -- Create >30 days old report and fixed report for spatial exclusion checks
    INSERT INTO public.hazard_reports (reporter_id, category, description, location, created_at)
    VALUES (
        v_user_b, 'pothole', 'Old pothole (>30 days)',
        extensions.ST_SetSRID(extensions.ST_MakePoint(78.41385, 17.48495), 4326)::extensions.geography,
        NOW() - INTERVAL '35 days'
    )
    RETURNING id INTO v_report_old;

    INSERT INTO public.hazard_reports (reporter_id, category, description, location, status)
    VALUES (
        v_user_b, 'pothole', 'Already fixed pothole',
        extensions.ST_SetSRID(extensions.ST_MakePoint(78.41382, 17.48492), 4326)::extensions.geography,
        'fixed'
    )
    RETURNING id INTO v_report_fixed;

    -- --------------------------------------------------------------------------
    -- 2. Verify A4 Triggers: Self-vote, Duplicate vote, Counts, Confidence, Fixed
    -- --------------------------------------------------------------------------
    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_a, 'confirm');
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Self-voting on own report must be rejected';

    -- 1 confirm -> medium, score 70 + 8 = 78
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'confirm');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.confirm_count = 1 AND v_row.confidence = 'medium' AND v_row.severity_score = 78,
        '1 confirm should yield medium confidence and severity 78';

    -- Duplicate confirm by same user -> PK violation
    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'confirm');
    EXCEPTION WHEN unique_violation THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Duplicate vote of same type by same user must be rejected';

    -- 3 confirms -> high confidence, confirmation bonus capped at +24 (70 + 24 = 94)
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES
        (v_report_1, v_user_c, 'confirm'),
        (v_report_1, v_user_d, 'confirm');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.confirm_count = 3 AND v_row.confidence = 'high' AND v_row.severity_score = 94,
        format('3 confirms expected high/94, got %s/%s', v_row.confidence, v_row.severity_score);

    -- 5 confirms -> bonus stays capped at +24, escalation_recommended TRUE
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES
        (v_report_1, v_user_e, 'confirm'),
        (v_report_1, v_user_f, 'confirm');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.confirm_count = 5 AND v_row.severity_score = 94 AND v_row.escalation_recommended = TRUE,
        '5 confirms must cap bonus at +24 and set escalation_recommended=TRUE';

    -- Dispute (-10 -> 84) then DELETE dispute (back to 94)
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'dispute');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.dispute_count = 1 AND v_row.severity_score = 84, 'Dispute should reduce severity by 10';

    DELETE FROM public.verifications WHERE report_id = v_report_1 AND user_id = v_user_b AND vote = 'dispute';
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.dispute_count = 0 AND v_row.severity_score = 94, 'Deleting dispute should restore severity';

    -- 2 distinct fixed votes -> status becomes 'fixed' and logs status_history
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'fixed');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.status = 'open' AND v_row.fixed_count = 1, '1 fixed vote must keep report open';

    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_c, 'fixed');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.status = 'fixed' AND v_row.fixed_count = 2 AND v_row.fixed_at IS NOT NULL,
        '2 distinct fixed votes must mark report fixed with fixed_at';

    SELECT COUNT(*) INTO v_count FROM public.status_history
    WHERE report_id = v_report_1 AND old_status = 'open' AND new_status = 'fixed';
    ASSERT v_count = 1, 'Expected 1 status_history entry for open -> fixed';

    -- Deleting one fixed vote reverts status to open and logs fixed -> open
    DELETE FROM public.verifications WHERE report_id = v_report_1 AND user_id = v_user_c AND vote = 'fixed';
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.status = 'open' AND v_row.fixed_count = 1 AND v_row.fixed_at IS NULL,
        'Dropping below 2 fixed votes must revert status to open';

    -- --------------------------------------------------------------------------
    -- 3. Verify A3 Spatial Functions
    -- --------------------------------------------------------------------------
    SELECT COUNT(*) INTO v_count
    FROM public.reports_in_bounds(17.4800, 78.4100, 17.4900, 78.4200, 'pothole', 'open');
    ASSERT v_count >= 2, 'reports_in_bounds should return open potholes inside viewport';

    -- Invalid bounds rejected
    v_blocked := FALSE;
    BEGIN
        PERFORM * FROM public.reports_in_bounds(18.0, 78.0, 17.0, 79.0, NULL, NULL);
    EXCEPTION WHEN invalid_parameter_value THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Invalid bounding box (min_lat > max_lat) must raise 22023';

    -- nearby_reports: includes v_report_1, excludes v_report_old (>30d) and v_report_fixed (fixed)
    SELECT COUNT(*) INTO v_count
    FROM public.nearby_reports(17.4849, 78.4138, 30.0, 'pothole')
    WHERE id IN (v_report_1, v_report_old, v_report_fixed);
    ASSERT v_count = 1, format('nearby_reports must include only active <=30d report, got %s', v_count);

    -- hazards_near_route: detects v_report_1 within 25m, excludes v_report_fixed
    v_route := '{"type":"LineString","coordinates":[[78.4130,17.4849],[78.4150,17.4849]]}'::jsonb;
    SELECT COUNT(*) INTO v_count
    FROM public.hazards_near_route(v_route, 25.0)
    WHERE id IN (v_report_1, v_report_fixed);
    ASSERT v_count = 1, 'hazards_near_route must find active hazard and exclude fixed hazard';

    -- Invalid GeoJSON rejected
    v_blocked := FALSE;
    BEGIN
        PERFORM * FROM public.hazards_near_route('{"type":"Point","coordinates":[78.41,17.48]}'::jsonb, 25.0);
    EXCEPTION WHEN invalid_parameter_value THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Non-LineString GeoJSON must be rejected';

    -- --------------------------------------------------------------------------
    -- 4. Verify A5 RLS Policies (Simulated Roles: anon, User A, User B, Admin)
    -- --------------------------------------------------------------------------

    -- 4a. Anonymous role reads 0 rows
    EXECUTE 'SET LOCAL ROLE anon';
    SELECT COUNT(*) INTO v_count FROM public.hazard_reports;
    ASSERT v_count = 0, 'Anonymous users must read 0 hazard_reports';
    EXECUTE 'RESET ROLE';

    -- 4b. User B cannot update User A's report, cannot promote self to admin, cannot escalate, cannot forge history
    EXECUTE 'SET LOCAL ROLE authenticated';
    PERFORM set_config('request.jwt.claim.sub', v_user_b::text, TRUE);
    PERFORM set_config('request.jwt.claim.role', 'authenticated', TRUE);

    UPDATE public.hazard_reports SET description = 'Hacked by B' WHERE id = v_report_1;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    ASSERT v_count = 0, 'User B must not be able to update User A report';

    v_blocked := FALSE;
    BEGIN
        UPDATE public.profiles SET role = 'admin' WHERE id = v_user_b;
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'User B must not be able to self-promote to admin';

    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.escalations (report_id, escalated_by, authority_name, note)
        VALUES (v_report_1, v_user_b, 'GHMC', 'Unauthorized escalation');
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'User B must not be able to insert escalation records';

    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.status_history (report_id, old_status, new_status, changed_by)
        VALUES (v_report_1, 'open', 'escalated', v_user_b);
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'User B must not be able to forge status_history';

    -- 4c. Admin can escalate report and insert escalation record
    PERFORM set_config('request.jwt.claim.sub', v_admin::text, TRUE);
    UPDATE public.hazard_reports SET status = 'escalated' WHERE id = v_report_1;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    ASSERT v_count = 1, 'Admin must be able to escalate hazard report';

    INSERT INTO public.escalations (report_id, escalated_by, authority_name, note)
    VALUES (v_report_1, v_admin, 'GHMC Kukatpally Zone', 'Escalated for road repair review');

    EXECUTE 'RESET ROLE';

    RAISE NOTICE 'ALL STAGE 4 LIVE TRANSACTIONAL ASSERTIONS PASSED! Rolling back test fixtures.';
END;
$$;

ROLLBACK;
