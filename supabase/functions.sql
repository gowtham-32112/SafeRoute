-- ==============================================================================
-- SafeRoute — Stage 4: PostGIS Spatial Database Functions (Issue A3)
-- Reference: docs/SPEC.md Section 7, Section 8 & Section 10
--
-- Prerequisites:
--   - supabase/schema.sql (Stage 3) already applied
--   - PostGIS extension enabled (Stage 2)
--
-- Security model:
--   - All functions are SECURITY INVOKER so PostgreSQL Row-Level Security (RLS)
--     is enforced for every caller.
--   - EXECUTE is revoked from PUBLIC/anon and granted only to authenticated
--     and service_role.
--   - search_path is explicitly fixed to 'public, extensions'.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- 1. reports_in_bounds
-- Returns up to 500 hazard reports inside the requested geographic bounding box
-- with optional category and status filters.
-- Uses geography GIST index idx_hazard_reports_location_gist via ST_Intersects.
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.reports_in_bounds(
    min_lat DOUBLE PRECISION,
    min_lng DOUBLE PRECISION,
    max_lat DOUBLE PRECISION,
    max_lng DOUBLE PRECISION,
    category_filter TEXT DEFAULT NULL,
    status_filter TEXT DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    reporter_id UUID,
    category TEXT,
    description TEXT,
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    address_text TEXT,
    status TEXT,
    escalation_recommended BOOLEAN,
    severity_score INTEGER,
    confidence TEXT,
    confirm_count INTEGER,
    dispute_count INTEGER,
    fixed_count INTEGER,
    ai_suggested_category TEXT,
    ai_confidence DOUBLE PRECISION,
    duplicate_of UUID,
    is_seed BOOLEAN,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    escalated_at TIMESTAMPTZ,
    fixed_at TIMESTAMPTZ
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, extensions
AS $$
DECLARE
    v_category TEXT;
    v_status TEXT;
    v_bounds geography;
BEGIN
    -- Validate bounding-box coordinates
    IF min_lat IS NULL OR min_lng IS NULL OR max_lat IS NULL OR max_lng IS NULL THEN
        RAISE EXCEPTION 'Bounding box coordinates must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    IF min_lat < -90.0 OR min_lat > 90.0 OR max_lat < -90.0 OR max_lat > 90.0 THEN
        RAISE EXCEPTION 'Latitude values must be between -90 and 90'
            USING ERRCODE = '22023';
    END IF;

    IF min_lng < -180.0 OR min_lng > 180.0 OR max_lng < -180.0 OR max_lng > 180.0 THEN
        RAISE EXCEPTION 'Longitude values must be between -180 and 180'
            USING ERRCODE = '22023';
    END IF;

    IF min_lat > max_lat OR min_lng > max_lng THEN
        RAISE EXCEPTION 'Invalid bounding box: min values must be <= max values'
            USING ERRCODE = '22023';
    END IF;

    -- Normalize and validate optional filters
    v_category := NULLIF(BTRIM(category_filter), '');
    IF v_category IS NOT NULL
       AND v_category NOT IN ('pothole', 'road_break', 'streetlight', 'footpath', 'other') THEN
        RAISE EXCEPTION 'Invalid category_filter: %', v_category
            USING ERRCODE = '22023';
    END IF;

    v_status := NULLIF(BTRIM(status_filter), '');
    IF v_status IS NOT NULL
       AND v_status NOT IN ('open', 'escalated', 'fixed') THEN
        RAISE EXCEPTION 'Invalid status_filter: %', v_status
            USING ERRCODE = '22023';
    END IF;

    v_bounds := ST_MakeEnvelope(min_lng, min_lat, max_lng, max_lat, 4326)::geography;

    RETURN QUERY
    SELECT
        hr.id,
        hr.reporter_id,
        hr.category,
        hr.description,
        ST_Y(hr.location::geometry) AS lat,
        ST_X(hr.location::geometry) AS lng,
        hr.address_text,
        hr.status,
        hr.escalation_recommended,
        hr.severity_score,
        hr.confidence,
        hr.confirm_count,
        hr.dispute_count,
        hr.fixed_count,
        hr.ai_suggested_category,
        hr.ai_confidence,
        hr.duplicate_of,
        hr.is_seed,
        hr.created_at,
        hr.updated_at,
        hr.escalated_at,
        hr.fixed_at
    FROM public.hazard_reports AS hr
    WHERE ST_Intersects(hr.location, v_bounds)
      AND (v_category IS NULL OR hr.category = v_category)
      AND (v_status IS NULL OR hr.status = v_status)
    ORDER BY hr.severity_score DESC, hr.created_at DESC
    LIMIT 500;
END;
$$;

REVOKE ALL ON FUNCTION public.reports_in_bounds(
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.reports_in_bounds(
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT, TEXT
) TO authenticated, service_role;

-- ------------------------------------------------------------------------------
-- 2. nearby_reports
-- Duplicate-detection lookup: returns non-fixed reports of the same category
-- within radius_m metres (spec default: 30m) created within the last 30 days.
-- Uses geography GIST index idx_hazard_reports_location_gist via ST_DWithin.
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.nearby_reports(
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    radius_m DOUBLE PRECISION DEFAULT 30.0,
    category TEXT DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    reporter_id UUID,
    report_category TEXT,
    description TEXT,
    report_lat DOUBLE PRECISION,
    report_lng DOUBLE PRECISION,
    address_text TEXT,
    status TEXT,
    severity_score INTEGER,
    confidence TEXT,
    confirm_count INTEGER,
    dispute_count INTEGER,
    created_at TIMESTAMPTZ,
    distance_m DOUBLE PRECISION
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, extensions
AS $$
DECLARE
    v_category TEXT;
    v_origin geography;
BEGIN
    IF lat IS NULL OR lng IS NULL THEN
        RAISE EXCEPTION 'Latitude and longitude must not be NULL'
            USING ERRCODE = '22023';
    END IF;

    IF lat < -90.0 OR lat > 90.0 THEN
        RAISE EXCEPTION 'Latitude must be between -90 and 90'
            USING ERRCODE = '22023';
    END IF;

    IF lng < -180.0 OR lng > 180.0 THEN
        RAISE EXCEPTION 'Longitude must be between -180 and 180'
            USING ERRCODE = '22023';
    END IF;

    IF radius_m IS NULL OR radius_m <= 0.0 OR radius_m > 50000.0 THEN
        RAISE EXCEPTION 'radius_m must be greater than 0 and at most 50000 metres'
            USING ERRCODE = '22023';
    END IF;

    v_category := NULLIF(BTRIM(nearby_reports.category), '');
    IF v_category IS NULL
       OR v_category NOT IN ('pothole', 'road_break', 'streetlight', 'footpath', 'other') THEN
        RAISE EXCEPTION 'category must be one of pothole, road_break, streetlight, footpath, other'
            USING ERRCODE = '22023';
    END IF;

    v_origin := ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography;

    RETURN QUERY
    SELECT
        hr.id,
        hr.reporter_id,
        hr.category AS report_category,
        hr.description,
        ST_Y(hr.location::geometry) AS report_lat,
        ST_X(hr.location::geometry) AS report_lng,
        hr.address_text,
        hr.status,
        hr.severity_score,
        hr.confidence,
        hr.confirm_count,
        hr.dispute_count,
        hr.created_at,
        ST_Distance(hr.location, v_origin) AS distance_m
    FROM public.hazard_reports AS hr
    WHERE ST_DWithin(hr.location, v_origin, radius_m)
      AND hr.category = v_category
      AND hr.status <> 'fixed'
      AND hr.created_at >= (NOW() - INTERVAL '30 days')
    ORDER BY distance_m ASC, hr.created_at DESC
    LIMIT 50;
END;
$$;

REVOKE ALL ON FUNCTION public.nearby_reports(
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.nearby_reports(
    DOUBLE PRECISION, DOUBLE PRECISION, DOUBLE PRECISION, TEXT
) TO authenticated, service_role;

-- ------------------------------------------------------------------------------
-- 3. hazards_near_route
-- Route-scoring lookup: accepts a GeoJSON LineString/MultiLineString (or Feature
-- wrapping one) and returns active (non-fixed) hazards within buffer_m metres
-- (spec default: 25m). Uses geography GIST index via ST_DWithin.
-- ------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.hazards_near_route(
    route_geojson JSONB,
    buffer_m DOUBLE PRECISION DEFAULT 25.0
)
RETURNS TABLE (
    id UUID,
    category TEXT,
    description TEXT,
    lat DOUBLE PRECISION,
    lng DOUBLE PRECISION,
    address_text TEXT,
    status TEXT,
    severity_score INTEGER,
    confidence TEXT,
    confirm_count INTEGER,
    dispute_count INTEGER,
    created_at TIMESTAMPTZ,
    distance_m DOUBLE PRECISION
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = public, extensions
AS $$
DECLARE
    v_geom_json JSONB;
    v_geom geometry;
    v_route_geog geography;
BEGIN
    IF route_geojson IS NULL OR jsonb_typeof(route_geojson) <> 'object' THEN
        RAISE EXCEPTION 'route_geojson must be a valid GeoJSON object'
            USING ERRCODE = '22023';
    END IF;

    IF buffer_m IS NULL OR buffer_m <= 0.0 OR buffer_m > 5000.0 THEN
        RAISE EXCEPTION 'buffer_m must be greater than 0 and at most 5000 metres'
            USING ERRCODE = '22023';
    END IF;

    -- Support either raw GeoJSON Geometry or a GeoJSON Feature wrapping a Geometry
    IF route_geojson ->> 'type' = 'Feature' THEN
        v_geom_json := route_geojson -> 'geometry';
    ELSE
        v_geom_json := route_geojson;
    END IF;

    IF v_geom_json IS NULL OR jsonb_typeof(v_geom_json) <> 'object' THEN
        RAISE EXCEPTION 'route_geojson does not contain a valid geometry object'
            USING ERRCODE = '22023';
    END IF;

    BEGIN
        v_geom := ST_SetSRID(ST_GeomFromGeoJSON(v_geom_json::TEXT), 4326);
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'Invalid GeoJSON route geometry'
            USING ERRCODE = '22023';
    END;

    IF v_geom IS NULL OR ST_IsEmpty(v_geom) THEN
        RAISE EXCEPTION 'Route geometry must not be empty'
            USING ERRCODE = '22023';
    END IF;

    IF ST_GeometryType(v_geom) NOT IN ('ST_LineString', 'ST_MultiLineString') THEN
        RAISE EXCEPTION 'Route geometry must be a LineString or MultiLineString'
            USING ERRCODE = '22023';
    END IF;

    IF NOT ST_IsValid(v_geom) THEN
        RAISE EXCEPTION 'Route geometry is not spatially valid'
            USING ERRCODE = '22023';
    END IF;

    v_route_geog := v_geom::geography;

    RETURN QUERY
    SELECT
        hr.id,
        hr.category,
        hr.description,
        ST_Y(hr.location::geometry) AS lat,
        ST_X(hr.location::geometry) AS lng,
        hr.address_text,
        hr.status,
        hr.severity_score,
        hr.confidence,
        hr.confirm_count,
        hr.dispute_count,
        hr.created_at,
        ST_Distance(hr.location, v_route_geog) AS distance_m
    FROM public.hazard_reports AS hr
    WHERE ST_DWithin(hr.location, v_route_geog, buffer_m)
      AND hr.status <> 'fixed'
    ORDER BY distance_m ASC, hr.severity_score DESC
    LIMIT 500;
END;
$$;

REVOKE ALL ON FUNCTION public.hazards_near_route(JSONB, DOUBLE PRECISION) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.hazards_near_route(JSONB, DOUBLE PRECISION)
    TO authenticated, service_role;
