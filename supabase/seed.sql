-- ==============================================================================
-- SafeRoute — Stage 10: Seed Dataset (GitHub Issue #34)
-- Reference: docs/SPEC.md Section 14 (Seed Data) & Section 7 (Data Model)
--
-- Features:
--   1. 100% Idempotent: Uses deterministic UUIDs and ON CONFLICT DO NOTHING.
--   2. Realistic Kukatpally Corridor: Centered around Kukatpally Y Junction (17.4849, 78.4138),
--      KPHB Metro, JNTU Junction, Moosapet, and Balanagar.
--   3. All 5 Categories: pothole, road_break, streetlight, footpath, other.
--   4. All 3 Statuses: open (12), escalated (5), fixed (3 with historical timestamps).
--   5. Edge Cases: Stale report (> 30 days old), nearby duplicate pair (< 25m).
--   6. Relational Integrity: Seed users, profile roles, evidence images, community
--      verifications, comments, and municipal escalations.
--   7. Zero Hardcoded Production Secrets.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. Demo User Accounts (Admin & Citizens)
-- ------------------------------------------------------------------------------

DO $$
BEGIN
    -- Safely seed auth.users if the auth schema exists (Supabase & standard test DBs)
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'auth' AND table_name = 'users'
    ) THEN
        INSERT INTO auth.users (
            id,
            instance_id,
            aud,
            role,
            email,
            encrypted_password,
            email_confirmed_at,
            raw_app_meta_data,
            raw_user_meta_data,
            created_at,
            updated_at
        )
        VALUES
            (
                'a0000000-0000-0000-0000-000000000001'::uuid,
                '00000000-0000-0000-0000-000000000000'::uuid,
                'authenticated',
                'authenticated',
                'admin@saferoute.demo',
                '$2a$10$w8.d8vK8oOqYh9qLd0QvMeO9x.w7E9b8hN3wK0l9qA8vO6zM1b2c3',
                NOW(),
                '{"provider":"email","providers":["email"]}'::jsonb,
                '{"full_name":"SafeRoute Admin","role":"admin"}'::jsonb,
                NOW() - INTERVAL '60 days',
                NOW()
            ),
            (
                'c0000000-0000-0000-0000-000000000001'::uuid,
                '00000000-0000-0000-0000-000000000000'::uuid,
                'authenticated',
                'authenticated',
                'citizen1@saferoute.demo',
                '$2a$10$w8.d8vK8oOqYh9qLd0QvMeO9x.w7E9b8hN3wK0l9qA8vO6zM1b2c3',
                NOW(),
                '{"provider":"email","providers":["email"]}'::jsonb,
                '{"full_name":"Priya Sharma"}'::jsonb,
                NOW() - INTERVAL '50 days',
                NOW()
            ),
            (
                'c0000000-0000-0000-0000-000000000002'::uuid,
                '00000000-0000-0000-0000-000000000000'::uuid,
                'authenticated',
                'authenticated',
                'citizen2@saferoute.demo',
                '$2a$10$w8.d8vK8oOqYh9qLd0QvMeO9x.w7E9b8hN3wK0l9qA8vO6zM1b2c3',
                NOW(),
                '{"provider":"email","providers":["email"]}'::jsonb,
                '{"full_name":"Rahul Varma"}'::jsonb,
                NOW() - INTERVAL '45 days',
                NOW()
            ),
            (
                'c0000000-0000-0000-0000-000000000003'::uuid,
                '00000000-0000-0000-0000-000000000000'::uuid,
                'authenticated',
                'authenticated',
                'citizen3@saferoute.demo',
                '$2a$10$w8.d8vK8oOqYh9qLd0QvMeO9x.w7E9b8hN3wK0l9qA8vO6zM1b2c3',
                NOW(),
                '{"provider":"email","providers":["email"]}'::jsonb,
                '{"full_name":"Ananya Reddy"}'::jsonb,
                NOW() - INTERVAL '40 days',
                NOW()
            )
        ON CONFLICT (id) DO NOTHING;
    END IF;
END;
$$;

-- Seed public.profiles (ensuring admin role is explicitly assigned)
INSERT INTO public.profiles (id, full_name, role, created_at)
VALUES
    ('a0000000-0000-0000-0000-000000000001'::uuid, 'SafeRoute Admin', 'admin', NOW() - INTERVAL '60 days'),
    ('c0000000-0000-0000-0000-000000000001'::uuid, 'Priya Sharma', 'user', NOW() - INTERVAL '50 days'),
    ('c0000000-0000-0000-0000-000000000002'::uuid, 'Rahul Varma', 'user', NOW() - INTERVAL '45 days'),
    ('c0000000-0000-0000-0000-000000000003'::uuid, 'Ananya Reddy', 'user', NOW() - INTERVAL '40 days')
ON CONFLICT (id) DO UPDATE
SET full_name = EXCLUDED.full_name,
    role = EXCLUDED.role;


-- ------------------------------------------------------------------------------
-- 2. Kukatpally Corridor Hazard Reports (20 Realistic Reports)
-- ------------------------------------------------------------------------------

INSERT INTO public.hazard_reports (
    id,
    reporter_id,
    category,
    description,
    location,
    address_text,
    status,
    ai_suggested_category,
    ai_confidence,
    duplicate_of,
    is_seed,
    created_at,
    updated_at,
    escalated_at,
    fixed_at
)
VALUES
    -- 1. Pothole - Kukatpally Y Junction (Primary)
    (
        'b0000000-0000-0000-0000-000000000001'::uuid,
        'c0000000-0000-0000-0000-000000000001'::uuid,
        'pothole',
        '[SEED] Severe crater pothole near Kukatpally Y Junction northbound lane',
        ST_SetSRID(ST_MakePoint(78.41380, 17.48490), 4326)::geography,
        'Kukatpally Y Junction, Mumbai Highway (NH65)',
        'open',
        'pothole',
        0.92,
        NULL,
        TRUE,
        NOW() - INTERVAL '3 days',
        NOW() - INTERVAL '3 days',
        NULL,
        NULL
    ),

    -- 2. Pothole - Kukatpally Y Junction (~15.6m duplicate of #1)
    (
        'b0000000-0000-0000-0000-000000000002'::uuid,
        'c0000000-0000-0000-0000-000000000002'::uuid,
        'pothole',
        '[SEED] Deep pothole causing heavy two-wheeler slow down at Y Junction',
        ST_SetSRID(ST_MakePoint(78.41392, 17.48498), 4326)::geography,
        'Kukatpally Y Junction, NH65 Flyover approach',
        'open',
        'pothole',
        0.88,
        'b0000000-0000-0000-0000-000000000001'::uuid,
        TRUE,
        NOW() - INTERVAL '2 days',
        NOW() - INTERVAL '2 days',
        NULL,
        NULL
    ),

    -- 3. Road Break - KPHB Colony Road No. 1 (Escalated)
    (
        'b0000000-0000-0000-0000-000000000003'::uuid,
        'c0000000-0000-0000-0000-000000000001'::uuid,
        'road_break',
        '[SEED] Major trench-cut asphalt collapse along KPHB Colony Road No. 1',
        ST_SetSRID(ST_MakePoint(78.40120, 17.49150), 4326)::geography,
        'Road No. 1, KPHB Colony, Kukatpally',
        'escalated',
        'road_break',
        0.95,
        NULL,
        TRUE,
        NOW() - INTERVAL '10 days',
        NOW() - INTERVAL '4 days',
        NOW() - INTERVAL '4 days',
        NULL
    ),

    -- 4. Streetlight - KPHB Colony Metro Station (Open)
    (
        'b0000000-0000-0000-0000-000000000004'::uuid,
        'c0000000-0000-0000-0000-000000000002'::uuid,
        'streetlight',
        '[SEED] Three consecutive streetlights completely dead near Metro Pillar 782',
        ST_SetSRID(ST_MakePoint(78.39800, 17.49300), 4326)::geography,
        'KPHB Colony Metro Station, NH65',
        'open',
        'streetlight',
        0.84,
        NULL,
        TRUE,
        NOW() - INTERVAL '5 days',
        NOW() - INTERVAL '5 days',
        NULL,
        NULL
    ),

    -- 5. Footpath - Opposite JNTU Gate 1 (Escalated)
    (
        'b0000000-0000-0000-0000-000000000005'::uuid,
        'c0000000-0000-0000-0000-000000000003'::uuid,
        'footpath',
        '[SEED] Broken pavement slabs and missing drain grating forcing pedestrians into traffic',
        ST_SetSRID(ST_MakePoint(78.38880, 17.49750), 4326)::geography,
        'Opposite JNTU Gate 1, Pragathi Nagar Road',
        'escalated',
        'footpath',
        0.79,
        NULL,
        TRUE,
        NOW() - INTERVAL '14 days',
        NOW() - INTERVAL '8 days',
        NOW() - INTERVAL '8 days',
        NULL
    ),

    -- 6. Other - Balanagar Main Road (Open)
    (
        'b0000000-0000-0000-0000-000000000006'::uuid,
        'c0000000-0000-0000-0000-000000000001'::uuid,
        'other',
        '[SEED] Construction debris dumping blocking left-turn slip road',
        ST_SetSRID(ST_MakePoint(78.44200, 17.46850), 4326)::geography,
        'Balanagar Main Road, Near IDPL Factory Colony',
        'open',
        'other',
        0.72,
        NULL,
        TRUE,
        NOW() - INTERVAL '4 days',
        NOW() - INTERVAL '4 days',
        NULL,
        NULL
    ),

    -- 7. Pothole - Moosapet X Roads (Fixed - 72h elapsed)
    (
        'b0000000-0000-0000-0000-000000000007'::uuid,
        'c0000000-0000-0000-0000-000000000002'::uuid,
        'pothole',
        '[SEED] Multiple small potholes patched with bitumen by GHMC maintenance team',
        ST_SetSRID(ST_MakePoint(78.42650, 17.47120), 4326)::geography,
        'Moosapet X Roads, Kukatpally Link Road',
        'fixed',
        'pothole',
        0.85,
        NULL,
        TRUE,
        NOW() - INTERVAL '12 days',
        NOW() - INTERVAL '9 days',
        NULL,
        NOW() - INTERVAL '9 days'
    ),

    -- 8. Pothole - Stale report (> 30 days old)
    (
        'b0000000-0000-0000-0000-000000000008'::uuid,
        'c0000000-0000-0000-0000-000000000003'::uuid,
        'pothole',
        '[SEED] Worn asphalt surface with recurring water ponding pothole',
        ST_SetSRID(ST_MakePoint(78.44850, 17.46210), 4326)::geography,
        'Fatehnagar Flyover Service Road, Balanagar',
        'open',
        'pothole',
        0.81,
        NULL,
        TRUE,
        NOW() - INTERVAL '40 days',
        NOW() - INTERVAL '40 days',
        NULL,
        NULL
    ),

    -- 9. Road Break - KPHB Phase 3 (Escalated)
    (
        'b0000000-0000-0000-0000-000000000009'::uuid,
        'c0000000-0000-0000-0000-000000000001'::uuid,
        'road_break',
        '[SEED] Deep pipeline excavation ditch left unbarricaded without warning signs',
        ST_SetSRID(ST_MakePoint(78.39050, 17.48800), 4326)::geography,
        'KPHB Phase 3, Near Forum Sujana Mall',
        'escalated',
        'road_break',
        0.91,
        NULL,
        TRUE,
        NOW() - INTERVAL '7 days',
        NOW() - INTERVAL '2 days',
        NOW() - INTERVAL '2 days',
        NULL
    ),

    -- 10. Streetlight - IDL Lake Road (Escalated)
    (
        'b0000000-0000-0000-0000-000000000010'::uuid,
        'c0000000-0000-0000-0000-000000000002'::uuid,
        'streetlight',
        '[SEED] Dark corridor along canal bund road due to faulty transformer cable',
        ST_SetSRID(ST_MakePoint(78.40500, 17.48100), 4326)::geography,
        'IDL Lake Road, Kukatpally',
        'escalated',
        'streetlight',
        0.78,
        NULL,
        TRUE,
        NOW() - INTERVAL '6 days',
        NOW() - INTERVAL '1 day',
        NOW() - INTERVAL '1 day',
        NULL
    ),

    -- 11. Footpath - Sangeeth Nagar (Open)
    (
        'b0000000-0000-0000-0000-000000000011'::uuid,
        'c0000000-0000-0000-0000-000000000003'::uuid,
        'footpath',
        '[SEED] Illegally placed commercial hoarding blocking pedestrian walk space',
        ST_SetSRID(ST_MakePoint(78.41900, 17.48920), 4326)::geography,
        'Sangeeth Nagar, Kukatpally Main Road',
        'open',
        'footpath',
        0.75,
        NULL,
        TRUE,
        NOW() - INTERVAL '3 days',
        NOW() - INTERVAL '3 days',
        NULL,
        NULL
    ),

    -- 12. Road Break - HMT Hills (Fixed - 96h elapsed)
    (
        'b0000000-0000-0000-0000-000000000012'::uuid,
        'c0000000-0000-0000-0000-000000000001'::uuid,
        'road_break',
        '[SEED] Manhole cover re-levelled and concreted flush with the road',
        ST_SetSRID(ST_MakePoint(78.39500, 17.50200), 4326)::geography,
        'HMT Hills Road, KPHB Colony',
        'fixed',
        'road_break',
        0.87,
        NULL,
        TRUE,
        NOW() - INTERVAL '15 days',
        NOW() - INTERVAL '11 days',
        NULL,
        NOW() - INTERVAL '11 days'
    ),

    -- 13. Pothole - Under Balanagar Flyover (Open)
    (
        'b0000000-0000-0000-0000-000000000013'::uuid,
        'c0000000-0000-0000-0000-000000000002'::uuid,
        'pothole',
        '[SEED] Clustered edge potholes under Balanagar Flyover junction',
        ST_SetSRID(ST_MakePoint(78.44620, 17.46780), 4326)::geography,
        'Under Balanagar Flyover, Narsapur X Roads',
        'open',
        'pothole',
        0.89,
        NULL,
        TRUE,
        NOW() - INTERVAL '2 days',
        NOW() - INTERVAL '2 days',
        NULL,
        NULL
    ),

    -- 14. Pothole - Kukatpally Bus Depot Road (Open)
    (
        'b0000000-0000-0000-0000-000000000014'::uuid,
        'c0000000-0000-0000-0000-000000000003'::uuid,
        'pothole',
        '[SEED] Sharp-edged pothole damaging car rims on left lane',
        ST_SetSRID(ST_MakePoint(78.42100, 17.48050), 4326)::geography,
        'Kukatpally Bus Depot Road',
        'open',
        'pothole',
        0.83,
        NULL,
        TRUE,
        NOW() - INTERVAL '1 day',
        NOW() - INTERVAL '1 day',
        NULL,
        NULL
    ),

    -- 15. Streetlight - Pragathi Nagar Main Road (Escalated)
    (
        'b0000000-0000-0000-0000-000000000015'::uuid,
        'c0000000-0000-0000-0000-000000000001'::uuid,
        'streetlight',
        '[SEED] Streetlight pole leaning dangerously after vehicle impact',
        ST_SetSRID(ST_MakePoint(78.38400, 17.50500), 4326)::geography,
        'Pragathi Nagar Main Road, Kukatpally',
        'escalated',
        'streetlight',
        0.80,
        NULL,
        TRUE,
        NOW() - INTERVAL '8 days',
        NOW() - INTERVAL '3 days',
        NOW() - INTERVAL '3 days',
        NULL
    ),

    -- 16. Footpath - KPHB 4th Phase (Fixed - 48h elapsed)
    (
        'b0000000-0000-0000-0000-000000000016'::uuid,
        'c0000000-0000-0000-0000-000000000002'::uuid,
        'footpath',
        '[SEED] Re-laid paver blocks and repaired ramp access for wheelchair users',
        ST_SetSRID(ST_MakePoint(78.40450, 17.49600), 4326)::geography,
        'MIG Colony, KPHB Colony 4th Phase',
        'fixed',
        'footpath',
        0.77,
        NULL,
        TRUE,
        NOW() - INTERVAL '20 days',
        NOW() - INTERVAL '18 days',
        NULL,
        NOW() - INTERVAL '18 days'
    ),

    -- 17. Other - Moosapet Anjaneya Nagar (Open)
    (
        'b0000000-0000-0000-0000-000000000017'::uuid,
        'c0000000-0000-0000-0000-000000000003'::uuid,
        'other',
        '[SEED] Silted open drain overflowing during rains onto road surface',
        ST_SetSRID(ST_MakePoint(78.43100, 17.47400), 4326)::geography,
        'Moosapet Anjaneya Nagar Colony Road',
        'open',
        'other',
        0.71,
        NULL,
        TRUE,
        NOW() - INTERVAL '6 days',
        NOW() - INTERVAL '6 days',
        NULL,
        NULL
    ),

    -- 18. Pothole - JNTU Hi-Tech City Road (Open)
    (
        'b0000000-0000-0000-0000-000000000018'::uuid,
        'c0000000-0000-0000-0000-000000000001'::uuid,
        'pothole',
        '[SEED] Wide shallow depression causing motorcycle skid risk',
        ST_SetSRID(ST_MakePoint(78.38200, 17.49000), 4326)::geography,
        'JNTU Hi-Tech City Road, KPHB 7th Phase',
        'open',
        'pothole',
        0.86,
        NULL,
        TRUE,
        NOW() - INTERVAL '5 days',
        NOW() - INTERVAL '5 days',
        NULL,
        NULL
    ),

    -- 19. Footpath - Vivekananda Nagar (Open)
    (
        'b0000000-0000-0000-0000-000000000019'::uuid,
        'c0000000-0000-0000-0000-000000000002'::uuid,
        'footpath',
        '[SEED] Uprooted kerb stones obstructing footpath near bus shelter',
        ST_SetSRID(ST_MakePoint(78.42450, 17.48700), 4326)::geography,
        'Vivekananda Nagar, Kukatpally',
        'open',
        'footpath',
        0.76,
        NULL,
        TRUE,
        NOW() - INTERVAL '4 days',
        NOW() - INTERVAL '4 days',
        NULL,
        NULL
    ),

    -- 20. Road Break - Bharat Nagar to Moosapet Ramp (Open)
    (
        'b0000000-0000-0000-0000-000000000020'::uuid,
        'c0000000-0000-0000-0000-000000000003'::uuid,
        'road_break',
        '[SEED] Longitudinal road crack along bridge expansion joint',
        ST_SetSRID(ST_MakePoint(78.43500, 17.46600), 4326)::geography,
        'Bharat Nagar Flyover to Moosapet ramp',
        'open',
        'road_break',
        0.82,
        NULL,
        TRUE,
        NOW() - INTERVAL '2 days',
        NOW() - INTERVAL '2 days',
        NULL,
        NULL
    )
ON CONFLICT (id) DO NOTHING;


-- ------------------------------------------------------------------------------
-- 3. Evidence Images for Seed Reports
-- ------------------------------------------------------------------------------

INSERT INTO public.report_images (id, report_id, storage_path, kind, created_at)
VALUES
    ('e0000000-0000-0000-0000-000000000001'::uuid, 'b0000000-0000-0000-0000-000000000001'::uuid, 'seed/pothole_kukatpally_01.jpg', 'evidence', NOW() - INTERVAL '3 days'),
    ('e0000000-0000-0000-0000-000000000002'::uuid, 'b0000000-0000-0000-0000-000000000002'::uuid, 'seed/pothole_kukatpally_02.jpg', 'evidence', NOW() - INTERVAL '2 days'),
    ('e0000000-0000-0000-0000-000000000003'::uuid, 'b0000000-0000-0000-0000-000000000003'::uuid, 'seed/roadbreak_kphb_01.jpg', 'evidence', NOW() - INTERVAL '10 days'),
    ('e0000000-0000-0000-0000-000000000004'::uuid, 'b0000000-0000-0000-0000-000000000004'::uuid, 'seed/streetlight_metro_01.jpg', 'evidence', NOW() - INTERVAL '5 days'),
    ('e0000000-0000-0000-0000-000000000005'::uuid, 'b0000000-0000-0000-0000-000000000005'::uuid, 'seed/footpath_jntu_01.jpg', 'evidence', NOW() - INTERVAL '14 days'),
    ('e0000000-0000-0000-0000-000000000006'::uuid, 'b0000000-0000-0000-0000-000000000006'::uuid, 'seed/debris_balanagar_01.jpg', 'evidence', NOW() - INTERVAL '4 days'),
    ('e0000000-0000-0000-0000-000000000007'::uuid, 'b0000000-0000-0000-0000-000000000007'::uuid, 'seed/pothole_moosapet_fixed.jpg', 'after', NOW() - INTERVAL '9 days'),
    ('e0000000-0000-0000-0000-000000000008'::uuid, 'b0000000-0000-0000-0000-000000000009'::uuid, 'seed/roadbreak_sujana_01.jpg', 'evidence', NOW() - INTERVAL '7 days'),
    ('e0000000-0000-0000-0000-000000000009'::uuid, 'b0000000-0000-0000-0000-000000000010'::uuid, 'seed/dark_idllake_01.jpg', 'evidence', NOW() - INTERVAL '6 days'),
    ('e0000000-0000-0000-0000-000000000010'::uuid, 'b0000000-0000-0000-0000-000000000015'::uuid, 'seed/streetlight_tilt_01.jpg', 'evidence', NOW() - INTERVAL '8 days')
ON CONFLICT (id) DO NOTHING;


-- ------------------------------------------------------------------------------
-- 4. Community Verifications (Voting - Confirm / Dispute / Fixed)
-- Non-reporter votes only (enforcing trg_verifications_prevent_self_vote)
-- ------------------------------------------------------------------------------

INSERT INTO public.verifications (report_id, user_id, vote, created_at)
VALUES
    -- Report #1 (Pothole Y Junction) - 2 Confirms
    ('b0000000-0000-0000-0000-000000000001'::uuid, 'c0000000-0000-0000-0000-000000000002'::uuid, 'confirm', NOW() - INTERVAL '2 days'),
    ('b0000000-0000-0000-0000-000000000001'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'confirm', NOW() - INTERVAL '1 day'),

    -- Report #3 (Road Break KPHB) - 2 Confirms
    ('b0000000-0000-0000-0000-000000000003'::uuid, 'c0000000-0000-0000-0000-000000000002'::uuid, 'confirm', NOW() - INTERVAL '8 days'),
    ('b0000000-0000-0000-0000-000000000003'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'confirm', NOW() - INTERVAL '7 days'),

    -- Report #4 (Streetlight Metro) - 1 Confirm, 1 Dispute
    ('b0000000-0000-0000-0000-000000000004'::uuid, 'c0000000-0000-0000-0000-000000000001'::uuid, 'confirm', NOW() - INTERVAL '4 days'),
    ('b0000000-0000-0000-0000-000000000004'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'dispute', NOW() - INTERVAL '3 days'),

    -- Report #5 (Footpath JNTU) - 2 Confirms
    ('b0000000-0000-0000-0000-000000000005'::uuid, 'c0000000-0000-0000-0000-000000000001'::uuid, 'confirm', NOW() - INTERVAL '12 days'),
    ('b0000000-0000-0000-0000-000000000005'::uuid, 'c0000000-0000-0000-0000-000000000002'::uuid, 'confirm', NOW() - INTERVAL '10 days'),

    -- Report #7 (Fixed Pothole Moosapet) - 2 Fixed Votes
    ('b0000000-0000-0000-0000-000000000007'::uuid, 'c0000000-0000-0000-0000-000000000001'::uuid, 'fixed', NOW() - INTERVAL '9 days'),
    ('b0000000-0000-0000-0000-000000000007'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'fixed', NOW() - INTERVAL '9 days'),

    -- Report #9 (Road Break Sujana) - 2 Confirms
    ('b0000000-0000-0000-0000-000000000009'::uuid, 'c0000000-0000-0000-0000-000000000002'::uuid, 'confirm', NOW() - INTERVAL '5 days'),
    ('b0000000-0000-0000-0000-000000000009'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'confirm', NOW() - INTERVAL '4 days'),

    -- Report #12 (Fixed Manhole HMT Hills) - 2 Fixed Votes
    ('b0000000-0000-0000-0000-000000000012'::uuid, 'c0000000-0000-0000-0000-000000000002'::uuid, 'fixed', NOW() - INTERVAL '11 days'),
    ('b0000000-0000-0000-0000-000000000012'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'fixed', NOW() - INTERVAL '11 days'),

    -- Report #16 (Fixed Footpath KPHB) - 2 Fixed Votes
    ('b0000000-0000-0000-0000-000000000016'::uuid, 'c0000000-0000-0000-0000-000000000001'::uuid, 'fixed', NOW() - INTERVAL '18 days'),
    ('b0000000-0000-0000-0000-000000000016'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'fixed', NOW() - INTERVAL '18 days')
ON CONFLICT (report_id, user_id, vote) DO NOTHING;


-- ------------------------------------------------------------------------------
-- 5. Citizen & Administrative Comments
-- ------------------------------------------------------------------------------

INSERT INTO public.comments (id, report_id, user_id, body, created_at)
VALUES
    ('f0000000-0000-0000-0000-000000000001'::uuid, 'b0000000-0000-0000-0000-000000000001'::uuid, 'c0000000-0000-0000-0000-000000000002'::uuid, 'Hit this on my scooter yesterday night. Waterlogged after rain, please navigate with caution.', NOW() - INTERVAL '2 days'),
    ('f0000000-0000-0000-0000-000000000002'::uuid, 'b0000000-0000-0000-0000-000000000001'::uuid, 'a0000000-0000-0000-0000-000000000001'::uuid, 'Flagged for inspection by Kukatpally GHMC ward engineers.', NOW() - INTERVAL '1 day'),
    ('f0000000-0000-0000-0000-000000000003'::uuid, 'b0000000-0000-0000-0000-000000000003'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'Water pipeline work was completed last week, but the trench was backfilled with loose mud only.', NOW() - INTERVAL '7 days'),
    ('f0000000-0000-0000-0000-000000000004'::uuid, 'b0000000-0000-0000-0000-000000000007'::uuid, 'c0000000-0000-0000-0000-000000000001'::uuid, 'GHMC maintenance truck arrived at 7 AM and completed bituminous patching. Surface is now smooth.', NOW() - INTERVAL '9 days'),
    ('f0000000-0000-0000-0000-000000000005'::uuid, 'b0000000-0000-0000-0000-000000000010'::uuid, 'c0000000-0000-0000-0000-000000000003'::uuid, 'Extremely dark stretch after 8 PM, high safety concern for lone women commuters.', NOW() - INTERVAL '4 days')
ON CONFLICT (id) DO NOTHING;


-- ------------------------------------------------------------------------------
-- 6. Municipal Escalations (for 5 Escalated Reports)
-- ------------------------------------------------------------------------------

INSERT INTO public.escalations (id, report_id, escalated_by, authority_name, note, created_at)
VALUES
    (
        'd0000000-0000-0000-0000-000000000001'::uuid,
        'b0000000-0000-0000-0000-000000000003'::uuid,
        'a0000000-0000-0000-0000-000000000001'::uuid,
        'GHMC Kukatpally Circle (Engineering Wing)',
        'Major road break on prime transit corridor; immediate cold-mix/asphalt restoration requested.',
        NOW() - INTERVAL '4 days'
    ),
    (
        'd0000000-0000-0000-0000-000000000002'::uuid,
        'b0000000-0000-0000-0000-000000000005'::uuid,
        'a0000000-0000-0000-0000-000000000001'::uuid,
        'HMDA / GHMC Urban Infrastructure',
        'Pedestrian walkway broken opposite university gate; high footfall hazard during peak college hours.',
        NOW() - INTERVAL '8 days'
    ),
    (
        'd0000000-0000-0000-0000-000000000003'::uuid,
        'b0000000-0000-0000-0000-000000000009'::uuid,
        'a0000000-0000-0000-0000-000000000001'::uuid,
        'Hyderabad Traffic Police - Cyberabad Division',
        'Unbarricaded excavation ditch near Sujana Mall posing collision risk for nighttime traffic.',
        NOW() - INTERVAL '2 days'
    ),
    (
        'd0000000-0000-0000-0000-000000000004'::uuid,
        'b0000000-0000-0000-0000-000000000010'::uuid,
        'a0000000-0000-0000-0000-000000000001'::uuid,
        'TSSPDCL Kukatpally Electrical Division',
        'Canal bund lighting blacked out; underground cable fault reported by community residents.',
        NOW() - INTERVAL '1 day'
    ),
    (
        'd0000000-0000-0000-0000-000000000005'::uuid,
        'b0000000-0000-0000-0000-000000000015'::uuid,
        'a0000000-0000-0000-0000-000000000001'::uuid,
        'TSSPDCL / GHMC Kukatpally',
        'Damaged streetlight pole leaning towards carriageway; structural collapse hazard.',
        NOW() - INTERVAL '3 days'
    )
ON CONFLICT (id) DO NOTHING;
