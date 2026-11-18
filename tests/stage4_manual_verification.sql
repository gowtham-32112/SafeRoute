-- ==============================================================================
-- SafeRoute — Stage 4 Live Transactional Verification Script (A3, A4, A5)
--
-- PURPOSE:
--   Safely verifies all Stage 4 spatial functions, triggers, security regression
--   fixes (zero GUC bypass, protected AI/duplicate fields, trusted created_at),
--   and RLS policies inside a single PostgreSQL transaction that ends with
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
    v_report_user_a UUID;
    v_report_past UUID;
    v_report_future UUID;
    v_report_old UUID;
    v_report_fixed UUID;
    v_row RECORD;
    v_count INTEGER;
    v_blocked BOOLEAN;
    v_route JSONB;
    v_stmt TEXT;
    v_attack_updates TEXT[];
BEGIN
    -- --------------------------------------------------------------------------
    -- 0. Create isolated synthetic users in auth.users (also tests signup trigger)
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

    SELECT role INTO v_row FROM public.profiles WHERE id = v_user_a;
    ASSERT v_row.role = 'user', 'Signup trigger must hardcode role=user even when metadata has role=admin';

    UPDATE public.profiles SET role = 'admin' WHERE id = v_admin;

    -- --------------------------------------------------------------------------
    -- 1. Trusted backend / admin report with AI confidence (verifies trusted flow)
    -- --------------------------------------------------------------------------
    INSERT INTO public.hazard_reports (reporter_id, category, description, location, ai_suggested_category, ai_confidence)
    VALUES (
        v_user_a, 'pothole', 'Deep pothole near junction',
        extensions.ST_SetSRID(extensions.ST_MakePoint(78.4138, 17.4849), 4326)::extensions.geography,
        'pothole', 0.85
    )
    RETURNING id INTO v_report_1;

    -- Base pothole(40) + ai>0.7(10) + <=7d(10) = 60, confidence = low, escalation_recommended = false
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.severity_score = 60, format('Expected initial severity 60, got %s', v_row.severity_score);
    ASSERT v_row.confidence = 'low', 'Expected initial confidence low';
    ASSERT v_row.escalation_recommended = FALSE, 'Expected escalation_recommended FALSE at score 60';

    -- --------------------------------------------------------------------------
    -- 2. Security Regression Tests as Ordinary Authenticated User A
    -- --------------------------------------------------------------------------
    EXECUTE 'SET LOCAL ROLE authenticated';
    PERFORM set_config('request.jwt.claim.sub', v_user_a::text, TRUE);
    PERFORM set_config('request.jwt.claim.role', 'authenticated', TRUE);

    -- 2a. Ordinary user CANNOT forge ai_confidence on INSERT
    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.hazard_reports (reporter_id, category, description, location, ai_confidence)
        VALUES (
            v_user_a, 'pothole', 'Forged AI conf',
            extensions.ST_SetSRID(extensions.ST_MakePoint(78.4139, 17.4850), 4326)::extensions.geography,
            0.99
        );
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Ordinary user must not forge ai_confidence on INSERT';

    -- 2b. Ordinary user CANNOT forge ai_suggested_category on INSERT
    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.hazard_reports (reporter_id, category, description, location, ai_suggested_category)
        VALUES (
            v_user_a, 'pothole', 'Forged AI category',
            extensions.ST_SetSRID(extensions.ST_MakePoint(78.4139, 17.4850), 4326)::extensions.geography,
            'pothole'
        );
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Ordinary user must not forge ai_suggested_category on INSERT';

    -- 2c. Ordinary user CANNOT forge duplicate_of on INSERT
    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.hazard_reports (reporter_id, category, description, location, duplicate_of)
        VALUES (
            v_user_a, 'pothole', 'Forged duplicate link',
            extensions.ST_SetSRID(extensions.ST_MakePoint(78.4139, 17.4850), 4326)::extensions.geography,
            v_report_1
        );
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Ordinary user must not forge duplicate_of on INSERT';

    -- 2d. Ordinary user submitting BACKDATED created_at -> forced to NOW()
    INSERT INTO public.hazard_reports (reporter_id, category, description, location, created_at)
    VALUES (
        v_user_a, 'streetlight', 'Backdate attempt',
        extensions.ST_SetSRID(extensions.ST_MakePoint(78.4140, 17.4851), 4326)::extensions.geography,
        NOW() - INTERVAL '60 days'
    )
    RETURNING id INTO v_report_past;

    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_past;
    ASSERT ABS(EXTRACT(EPOCH FROM (NOW() - v_row.created_at))) < 5,
        'Backdated created_at from ordinary user must be forced to NOW()';

    -- 2e. Ordinary user submitting FUTURE created_at -> forced to NOW()
    INSERT INTO public.hazard_reports (reporter_id, category, description, location, created_at)
    VALUES (
        v_user_a, 'streetlight', 'Future-date attempt',
        extensions.ST_SetSRID(extensions.ST_MakePoint(78.4141, 17.4852), 4326)::extensions.geography,
        NOW() + INTERVAL '30 days'
    )
    RETURNING id INTO v_report_future;

    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_future;
    ASSERT ABS(EXTRACT(EPOCH FROM (NOW() - v_row.created_at))) < 5,
        'Future created_at from ordinary user must be forced to NOW()';

    -- 2f. Legitimate ordinary-user INSERT & UPDATE on own open report works
    INSERT INTO public.hazard_reports (reporter_id, category, description, location)
    VALUES (
        v_user_a, 'other', 'Legitimate user report',
        extensions.ST_SetSRID(extensions.ST_MakePoint(78.4142, 17.4853), 4326)::extensions.geography
    )
    RETURNING id INTO v_report_user_a;

    UPDATE public.hazard_reports
    SET description = 'Updated legitimate description', category = 'footpath'
    WHERE id = v_report_user_a;

    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_user_a;
    ASSERT v_row.description = 'Updated legitimate description'
       AND v_row.category = 'footpath'
       AND v_row.severity_score = 45, -- footpath(35) + <=7d(10)
        'Legitimate edit on own open report must succeed and recompute severity';

    -- 2g. CRITICAL REGRESSION: Attempt custom GUC bypass on ALL protected fields
    PERFORM set_config('saferoute.internal_metrics_update', 'on', TRUE);

    v_attack_updates := ARRAY[
        format('UPDATE public.hazard_reports SET severity_score = 100 WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET confidence = ''high'' WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET confirm_count = 99 WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET dispute_count = 5 WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET fixed_count = 2 WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET escalation_recommended = TRUE WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET status = ''escalated'' WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET escalated_at = NOW() WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET fixed_at = NOW() WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET is_seed = TRUE WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET ai_confidence = 0.99 WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET ai_suggested_category = ''pothole'' WHERE id = %L', v_report_user_a),
        format('UPDATE public.hazard_reports SET duplicate_of = %L WHERE id = %L', v_report_1, v_report_user_a)
    ];

    FOREACH v_stmt IN ARRAY v_attack_updates LOOP
        v_blocked := FALSE;
        BEGIN
            EXECUTE v_stmt;
        EXCEPTION WHEN insufficient_privilege THEN
            v_blocked := TRUE;
        END;
        ASSERT v_blocked, format('Protected field update MUST be rejected even with custom GUC set: %s', v_stmt);
    END LOOP;

    -- 2h. Evidence image recomputation STILL WORKS for authenticated owner
    INSERT INTO public.report_images (report_id, storage_path, kind)
    VALUES (v_report_1, v_user_a::text || '/' || v_report_1::text || '/photo.jpg', 'evidence');

    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.severity_score = 70 AND v_row.escalation_recommended = TRUE,
        format('Evidence photo insert as authenticated owner must recompute severity to 70, got %s', v_row.severity_score);

    -- 2i. Self-voting on own report is rejected under authenticated role
    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_a, 'confirm');
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Self-voting on own report must be rejected';

    -- --------------------------------------------------------------------------
    -- 3. Verification Recomputation as Ordinary Authenticated User B
    -- --------------------------------------------------------------------------
    PERFORM set_config('request.jwt.claim.sub', v_user_b::text, TRUE);

    -- 3a. User B cannot update User A's report
    UPDATE public.hazard_reports SET description = 'Hacked by B' WHERE id = v_report_1;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    ASSERT v_count = 0, 'User B must not be able to update User A report';

    -- 3b. User B cannot self-promote to admin, escalate, or forge status_history
    v_blocked := FALSE;
    BEGIN
        UPDATE public.profiles SET role = 'admin' WHERE id = v_user_b;
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'User B must not self-promote to admin';

    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.escalations (report_id, escalated_by, authority_name, note)
        VALUES (v_report_1, v_user_b, 'GHMC', 'Unauthorized escalation');
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'User B must not insert escalation records';

    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.status_history (report_id, old_status, new_status, changed_by)
        VALUES (v_report_1, 'open', 'escalated', v_user_b);
    EXCEPTION WHEN insufficient_privilege THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'User B must not forge status_history';

    -- 3c. Legitimate confirmation by User B recomputes metrics internally
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'confirm');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.confirm_count = 1 AND v_row.confidence = 'medium' AND v_row.severity_score = 78,
        '1 confirm as authenticated user should yield medium confidence and severity 78';

    -- Duplicate confirm rejected by composite PK
    v_blocked := FALSE;
    BEGIN
        INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'confirm');
    EXCEPTION WHEN unique_violation THEN
        v_blocked := TRUE;
    END;
    ASSERT v_blocked, 'Duplicate vote of same type by same user must be rejected';

    -- User B votes fixed (count = 1, still open)
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'fixed');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.status = 'open' AND v_row.fixed_count = 1, '1 fixed vote must keep report open';

    -- Switch to User C: 2nd fixed vote transitions status -> fixed & logs status_history
    PERFORM set_config('request.jwt.claim.sub', v_user_c::text, TRUE);
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_c, 'fixed');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.status = 'fixed' AND v_row.fixed_count = 2 AND v_row.fixed_at IS NOT NULL,
        '2 distinct fixed votes must mark report fixed with fixed_at';

    SELECT COUNT(*) INTO v_count FROM public.status_history
    WHERE report_id = v_report_1 AND old_status = 'open' AND new_status = 'fixed';
    ASSERT v_count = 1, 'Expected 1 status_history entry for open -> fixed';

    -- User C deletes fixed vote -> reverts status -> open
    DELETE FROM public.verifications WHERE report_id = v_report_1 AND user_id = v_user_c AND vote = 'fixed';
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.status = 'open' AND v_row.fixed_count = 1 AND v_row.fixed_at IS NULL,
        'Deleting 2nd fixed vote must revert status to open';

    EXECUTE 'RESET ROLE';

    -- --------------------------------------------------------------------------
    -- 4. Multi-user confirmations (3 & 5 confirms), Dispute, and Spatial Checks
    -- --------------------------------------------------------------------------
    INSERT INTO public.verifications (report_id, user_id, vote) VALUES
        (v_report_1, v_user_c, 'confirm'),
        (v_report_1, v_user_d, 'confirm');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.confirm_count = 3 AND v_row.confidence = 'high' AND v_row.severity_score = 94,
        '3 confirms must yield high confidence and capped +24 bonus (score 94)';

    INSERT INTO public.verifications (report_id, user_id, vote) VALUES
        (v_report_1, v_user_e, 'confirm'),
        (v_report_1, v_user_f, 'confirm');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.confirm_count = 5 AND v_row.severity_score = 94 AND v_row.escalation_recommended = TRUE,
        '5 confirms must cap bonus at +24 and set escalation_recommended=TRUE';

    INSERT INTO public.verifications (report_id, user_id, vote) VALUES (v_report_1, v_user_b, 'dispute');
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.dispute_count = 1 AND v_row.severity_score = 84, 'Dispute should subtract 10';

    DELETE FROM public.verifications WHERE report_id = v_report_1 AND user_id = v_user_b AND vote = 'dispute';
    SELECT * INTO v_row FROM public.hazard_reports WHERE id = v_report_1;
    ASSERT v_row.dispute_count = 0 AND v_row.severity_score = 94, 'Deleting dispute should restore severity';

    -- Create >30d old report & fixed report for A3 spatial checks
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

    SELECT COUNT(*) INTO v_count
    FROM public.reports_in_bounds(17.4800, 78.4100, 17.4900, 78.4200, 'pothole', 'open');
    ASSERT v_count >= 2, 'reports_in_bounds should return open potholes inside viewport';

    SELECT COUNT(*) INTO v_count
    FROM public.nearby_reports(17.4849, 78.4138, 30.0, 'pothole')
    WHERE id IN (v_report_1, v_report_old, v_report_fixed);
    ASSERT v_count = 1, 'nearby_reports must include only active <=30d report';

    v_route := '{"type":"LineString","coordinates":[[78.4130,17.4849],[78.4150,17.4849]]}'::jsonb;
    SELECT COUNT(*) INTO v_count
    FROM public.hazards_near_route(v_route, 25.0)
    WHERE id IN (v_report_1, v_report_fixed);
    ASSERT v_count = 1, 'hazards_near_route must find active hazard and exclude fixed hazard';

    -- --------------------------------------------------------------------------
    -- 5. Verify Anonymous Denied & Admin Authorized
    -- --------------------------------------------------------------------------
    EXECUTE 'SET LOCAL ROLE anon';
    SELECT COUNT(*) INTO v_count FROM public.hazard_reports;
    ASSERT v_count = 0, 'Anonymous users must read 0 hazard_reports';
    EXECUTE 'RESET ROLE';

    EXECUTE 'SET LOCAL ROLE authenticated';
    PERFORM set_config('request.jwt.claim.sub', v_admin::text, TRUE);
    PERFORM set_config('request.jwt.claim.role', 'authenticated', TRUE);

    UPDATE public.hazard_reports SET status = 'escalated', ai_confidence = 0.91 WHERE id = v_report_1;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    ASSERT v_count = 1, 'Admin must be able to escalate hazard report and set AI fields';

    INSERT INTO public.escalations (report_id, escalated_by, authority_name, note)
    VALUES (v_report_1, v_admin, 'GHMC Kukatpally Zone', 'Escalated for road repair review');

    EXECUTE 'RESET ROLE';

    RAISE NOTICE 'ALL STAGE 4 SECURITY & FUNCTIONAL ASSERTIONS PASSED! Rolling back test fixtures.';
END;
$$;

ROLLBACK;
