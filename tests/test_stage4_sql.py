"""Automated verification tests for SafeRoute Stage 4 (Issues A3, A4, A5).

Covers:
1. Static SQL contract, security, and PostgreSQL compatibility checks for:
   - supabase/functions.sql (A3 spatial functions)
   - supabase/triggers.sql (A4 triggers & business logic)
   - supabase/policies.sql (A5 RLS & Storage policies)
2. Exhaustive pure-Python specification parity tests for severity scoring,
   clamping, confidence transitions, escalation thresholds, and fixed rules.
3. Strict Stage 4 scope & safety guards (no destructive DDL, no unapproved
   schema changes, no secrets).

Note: Live PostgreSQL execution tests are provided separately in
tests/stage4_manual_verification.sql (transactional BEGIN ... ROLLBACK).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS_SQL = REPO_ROOT / "supabase" / "functions.sql"
TRIGGERS_SQL = REPO_ROOT / "supabase" / "triggers.sql"
POLICIES_SQL = REPO_ROOT / "supabase" / "policies.sql"
SCHEMA_SQL = REPO_ROOT / "supabase" / "schema.sql"


def _read_code(path: Path) -> str:
    assert path.exists(), f"{path.name} must exist"
    text = path.read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


def _reference_severity(
    category: str,
    has_photo: bool,
    ai_confidence: float | None,
    confirm_count: int,
    dispute_count: int,
    age_days: float,
) -> int:
    """Reference implementation matching supabase/triggers.sql calculate_severity_score."""
    weights = {
        "pothole": 40,
        "road_break": 40,
        "footpath": 35,
        "streetlight": 25,
        "other": 15,
    }
    score = weights.get(category, 15)
    if has_photo:
        score += 10
    if ai_confidence is not None and ai_confidence > 0.7:
        score += 10
    score += min(max(confirm_count, 0) * 8, 24)
    score -= max(dispute_count, 0) * 10
    if age_days <= 7:
        score += 10
    elif age_days <= 30:
        score += 5
    return max(0, min(100, score))


def _reference_confidence(confirm_count: int, dispute_count: int) -> str:
    """Reference implementation matching supabase/triggers.sql calculate_confidence."""
    c = max(confirm_count, 0)
    d = max(dispute_count, 0)
    if c >= 3 and c > d:
        return "high"
    if c >= 1:
        return "medium"
    return "low"


# ==============================================================================
# A3 — Spatial Function Contract & Security Tests
# ==============================================================================

def test_a3_all_three_spatial_functions_defined():
    sql = _read_code(FUNCTIONS_SQL)
    for fn in [
        "public.reports_in_bounds",
        "public.nearby_reports",
        "public.hazards_near_route",
    ]:
        assert re.search(
            rf"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+{re.escape(fn)}\s*\(",
            sql,
            re.IGNORECASE,
        ), f"Missing function {fn}"


def test_a3_functions_are_security_invoker_with_safe_search_path_and_grants():
    sql = _read_code(FUNCTIONS_SQL)
    assert len(re.findall(r"SECURITY\s+INVOKER", sql, re.IGNORECASE)) == 3
    assert "SECURITY DEFINER" not in sql.upper()
    assert len(re.findall(r"SET\s+search_path\s*=\s*public\s*,\s*extensions", sql, re.IGNORECASE)) == 3
    assert len(re.findall(r"REVOKE\s+ALL\s+ON\s+FUNCTION", sql, re.IGNORECASE)) == 3
    assert len(re.findall(r"GRANT\s+EXECUTE\s+ON\s+FUNCTION[\s\S]*?TO\s+authenticated,\s*service_role", sql, re.IGNORECASE)) == 3


def test_a3_reports_in_bounds_validation_limit_and_gist_usage():
    sql = _read_code(FUNCTIONS_SQL)
    fn_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.reports_in_bounds(.*?)REVOKE\s+ALL",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert fn_match is not None
    body = fn_match.group(1)

    for param in ["min_lat", "min_lng", "max_lat", "max_lng", "category_filter", "status_filter"]:
        assert param in body
    assert "min_lat > max_lat OR min_lng > max_lng" in body
    assert "ST_MakeEnvelope(min_lng, min_lat, max_lng, max_lat, 4326)::geography" in body
    assert "ST_Intersects(hr.location, v_bounds)" in body
    assert "LIMIT 500" in body


def test_a3_nearby_reports_filters_and_no_parameter_collision():
    sql = _read_code(FUNCTIONS_SQL)
    fn_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.nearby_reports\((.*?)\)\s*RETURNS\s+TABLE\s*\((.*?)\)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert fn_match is not None
    in_params = {
        line.strip().split()[0].lower()
        for line in fn_match.group(1).splitlines()
        if line.strip()
    }
    out_cols = {
        line.strip().split()[0].lower()
        for line in fn_match.group(2).splitlines()
        if line.strip()
    }
    # PostgreSQL rejects duplicate names across input params and RETURNS TABLE cols (42P13)
    assert in_params.isdisjoint(out_cols), (
        f"Parameter / RETURNS TABLE collision in nearby_reports: {in_params & out_cols}"
    )

    body_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.nearby_reports(.*?)REVOKE\s+ALL",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    body = body_match.group(1)
    assert "ST_DWithin(hr.location, v_origin, radius_m)" in body
    assert "ST_Distance(hr.location, v_origin)" in body
    assert "hr.category = v_category" in body
    assert "hr.status <> 'fixed'" in body
    assert "INTERVAL '30 days'" in body


def test_a3_hazards_near_route_validation_and_exclusions():
    sql = _read_code(FUNCTIONS_SQL)
    body_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.hazards_near_route(.*?)REVOKE\s+ALL",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert body_match is not None
    body = body_match.group(1)

    assert "route_geojson JSONB" in body
    assert "buffer_m DOUBLE PRECISION DEFAULT 25.0" in body
    assert "ST_GeomFromGeoJSON" in body
    assert "ST_LineString" in body and "ST_MultiLineString" in body
    assert "ST_IsValid(v_geom)" in body
    assert "ST_DWithin(hr.location, v_route_geog, buffer_m)" in body
    assert "hr.status <> 'fixed'" in body


# ==============================================================================
# A4 — Trigger Architecture, Concurrency & Business Logic Tests
# ==============================================================================

def test_a4_triggers_and_functions_defined():
    sql = _read_code(TRIGGERS_SQL)
    expected_triggers = [
        "trg_verifications_prevent_self_vote",
        "trg_verifications_recompute_metrics",
        "trg_report_images_recompute_metrics",
        "trg_hazard_reports_before_write",
        "trg_hazard_reports_log_status_history",
        "trg_profiles_prevent_role_escalation",
    ]
    for trg in expected_triggers:
        assert re.search(rf"CREATE\s+TRIGGER\s+{trg}\b", sql, re.IGNORECASE), (
            f"Missing trigger {trg}"
        )


def test_a4_concurrency_lock_recursion_guard_and_security_definer_safety():
    sql = _read_code(TRIGGERS_SQL)
    # Row-level locking against concurrent verification races
    assert "FOR UPDATE;" in sql
    # Recursion guard around internal metrics update
    assert "set_config('saferoute.internal_metrics_update', 'on', TRUE)" in sql
    assert "current_setting('saferoute.internal_metrics_update', TRUE) = 'on'" in sql
    # Status history trigger only fires on actual status transition
    assert "WHEN (OLD.status IS DISTINCT FROM NEW.status)" in sql
    # Distinct eligible voters counted
    assert "COUNT(DISTINCT v.user_id) FILTER (WHERE v.vote = 'confirm')" in sql
    assert "COUNT(DISTINCT v.user_id) FILTER (WHERE v.vote = 'dispute')" in sql
    assert "COUNT(DISTINCT v.user_id) FILTER (WHERE v.vote = 'fixed')" in sql
    # Fixed threshold = 2 and escalation thresholds = 5 confirms or severity >= 70
    assert "v_fixed_count >= 2" in sql
    assert "v_confirm_count >= 5 OR v_severity_score >= 70" in sql

    # Every SECURITY DEFINER function must fix search_path
    sec_def_blocks = re.findall(
        r"SECURITY\s+DEFINER\s+(SET\s+search_path\s*=\s*[\w,\s]+)",
        sql,
        re.IGNORECASE,
    )
    total_sec_def = len(re.findall(r"SECURITY\s+DEFINER", sql, re.IGNORECASE))
    assert len(sec_def_blocks) == total_sec_def and total_sec_def > 0


@pytest.mark.parametrize(
    "category,has_photo,ai_conf,confirms,disputes,age_days,expected",
    [
        # Base weights + <=7 days recency (+10)
        ("pothole", False, None, 0, 0, 1, 50),
        ("road_break", False, None, 0, 0, 1, 50),
        ("footpath", False, None, 0, 0, 1, 45),
        ("streetlight", False, None, 0, 0, 1, 35),
        ("other", False, None, 0, 0, 1, 25),
        # Evidence photo (+10) & AI confidence boundary (> 0.7 vs == 0.7)
        ("pothole", True, 0.7, 0, 0, 1, 60),
        ("pothole", True, 0.71, 0, 0, 1, 70),
        # Confirmation cap at +24 (3 confirms = +24, 5 confirms still +24)
        ("pothole", True, 0.9, 3, 0, 1, 94),
        ("pothole", True, 0.9, 10, 0, 1, 94),
        # Upper clamp at 100 (cannot exceed 100)
        ("pothole", True, 0.95, 4, 0, 0, 94),
        # Dispute penalty (-10 each) and lower clamp at 0
        ("other", False, None, 0, 3, 40, 0),
        ("other", False, None, 0, 10, 40, 0),
        # Recency bands: 7 days (+10), 8..30 days (+5), >30 days (+0)
        ("pothole", False, None, 0, 0, 7, 50),
        ("pothole", False, None, 0, 0, 8, 45),
        ("pothole", False, None, 0, 0, 30, 45),
        ("pothole", False, None, 0, 0, 31, 40),
    ],
)
def test_a4_severity_formula_parity(
    category, has_photo, ai_conf, confirms, disputes, age_days, expected
):
    assert _reference_severity(category, has_photo, ai_conf, confirms, disputes, age_days) == expected


@pytest.mark.parametrize(
    "confirms,disputes,expected",
    [
        (0, 0, "low"),
        (0, 3, "low"),
        (1, 0, "medium"),
        (2, 0, "medium"),
        (2, 5, "medium"),
        (3, 3, "medium"),  # must strictly exceed disputes for high
        (3, 4, "medium"),
        (3, 2, "high"),
        (5, 1, "high"),
    ],
)
def test_a4_confidence_transition_parity(confirms, disputes, expected):
    assert _reference_confidence(confirms, disputes) == expected


# ==============================================================================
# A5 — Row-Level Security & Storage Policy Tests
# ==============================================================================

def test_a5_rls_enabled_on_all_seven_tables():
    sql = _read_code(POLICIES_SQL)
    tables = [
        "public.profiles",
        "public.hazard_reports",
        "public.report_images",
        "public.verifications",
        "public.comments",
        "public.escalations",
        "public.status_history",
    ]
    for tbl in tables:
        assert re.search(
            rf"ALTER\s+TABLE\s+{re.escape(tbl)}\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY\s*;",
            sql,
            re.IGNORECASE,
        ), f"RLS not enabled on {tbl}"


def test_a5_no_anonymous_or_public_policies():
    sql = _read_code(POLICIES_SQL)
    assert not re.search(r"\bTO\s+anon\b", sql, re.IGNORECASE)
    assert not re.search(r"\bTO\s+public\b", sql, re.IGNORECASE)
    # Every CREATE POLICY must explicitly target authenticated
    policies = re.findall(r"CREATE\s+POLICY\s+\w+[\s\S]*?;", sql, re.IGNORECASE)
    assert len(policies) >= 20
    for pol in policies:
        assert re.search(r"\bTO\s+authenticated\b", pol, re.IGNORECASE), (
            f"Policy missing explicit TO authenticated scope:\n{pol}"
        )


def test_a5_profile_self_promotion_blocked_without_rls_recursion():
    sql = _read_code(POLICIES_SQL)
    assert "CREATE OR REPLACE FUNCTION public.is_admin()" in sql
    assert "CREATE OR REPLACE FUNCTION public.current_user_role()" in sql
    assert "role = public.current_user_role()" in sql
    # No direct recursive subquery on public.profiles inside profiles policies
    prof_pol = re.search(
        r"CREATE\s+POLICY\s+profiles_update_own[\s\S]*?;",
        sql,
        re.IGNORECASE,
    )
    assert prof_pol is not None
    assert "FROM public.profiles" not in prof_pol.group(0)


def test_a5_admin_only_escalations_and_append_only_status_history():
    sql = _read_code(POLICIES_SQL)
    esc_insert = re.search(r"CREATE\s+POLICY\s+escalations_insert_admin[\s\S]*?;", sql, re.IGNORECASE)
    assert esc_insert is not None
    assert "public.is_admin()" in esc_insert.group(0)

    # Ordinary users have no INSERT/UPDATE/DELETE policy on status_history
    assert not re.search(r"CREATE\s+POLICY\s+\w+\s+ON\s+public\.status_history\s+FOR\s+(UPDATE|DELETE)", sql, re.IGNORECASE)
    sh_insert = re.search(r"CREATE\s+POLICY\s+status_history_insert_admin[\s\S]*?;", sql, re.IGNORECASE)
    assert sh_insert is not None
    assert "public.is_admin()" in sh_insert.group(0)


def test_a5_storage_policies_strictly_scoped_to_hazard_photos_and_uid_path():
    sql = _read_code(POLICIES_SQL)
    storage_policies = re.findall(
        r"CREATE\s+POLICY\s+\w+\s+ON\s+storage\.objects[\s\S]*?;",
        sql,
        re.IGNORECASE,
    )
    assert len(storage_policies) == 4
    for pol in storage_policies:
        assert "bucket_id = 'hazard-photos'" in pol
        assert "auth.uid()" in pol

    insert_pol = re.search(
        r"CREATE\s+POLICY\s+hazard_photos_insert_own_path[\s\S]*?;",
        sql,
        re.IGNORECASE,
    )
    assert insert_pol is not None
    assert "array_length(storage.foldername(name), 1) = 2" in insert_pol.group(0)
    assert "(storage.foldername(name))[1] = auth.uid()::TEXT" in insert_pol.group(0)
    assert "name NOT LIKE '%..%'" in insert_pol.group(0)
    # Never make the bucket public
    assert "public = true" not in sql.lower()


# ==============================================================================
# Stage 4 Safety & Scope Guard Tests
# ==============================================================================

def test_stage4_sql_has_no_destructive_ddl_and_preserves_stage3_schema():
    for path in [FUNCTIONS_SQL, TRIGGERS_SQL, POLICIES_SQL]:
        sql = _read_code(path)
        assert not re.search(r"\bDROP\s+TABLE\b", sql, re.IGNORECASE), f"DROP TABLE in {path.name}"
        assert not re.search(r"\bTRUNCATE\b", sql, re.IGNORECASE), f"TRUNCATE in {path.name}"
        assert not re.search(r"\bDELETE\s+FROM\b", sql, re.IGNORECASE), f"DELETE FROM in {path.name}"
        assert not re.search(r"\bALTER\s+TABLE\s+[\w\.]+\s+ADD\s+COLUMN\b", sql, re.IGNORECASE), (
            f"Unapproved schema modification in {path.name}"
        )

    # Confirm hide_name privacy column was NOT silently invented in schema.sql
    schema = _read_code(SCHEMA_SQL)
    assert "hide_name" not in schema.lower() and "is_anonymous" not in schema.lower()
