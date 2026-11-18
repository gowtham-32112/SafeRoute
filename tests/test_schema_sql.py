"""Focused static verification tests for Stage 3 supabase/schema.sql.

These tests validate structural alignment with docs/SPEC.md Section 7,
security invariants for the signup trigger, idempotency of Realtime setup,
and strict Stage 3 scope boundaries. They do NOT replace actual execution
against Supabase PostgreSQL.
"""

from pathlib import Path
import re


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "supabase" / "schema.sql"


def _read_schema() -> str:
    assert SCHEMA_PATH.exists(), "supabase/schema.sql must exist"
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _strip_sql_comments(sql: str) -> str:
    """Remove single-line SQL comments for strict code-only assertions."""
    return "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )


def test_all_seven_required_tables_defined_in_dependency_order():
    """All seven Section 7 tables must be defined in valid FK dependency order."""
    sql = _strip_sql_comments(_read_schema())
    required_tables = [
        "public.profiles",
        "public.hazard_reports",
        "public.report_images",
        "public.verifications",
        "public.comments",
        "public.escalations",
        "public.status_history",
    ]
    positions = []
    for table in required_tables:
        match = re.search(
            rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\b",
            sql,
            re.IGNORECASE,
        )
        assert match is not None, f"Missing CREATE TABLE for {table}"
        positions.append((table, match.start()))

    pos_map = dict(positions)
    # profiles must precede tables referencing it; hazard_reports must precede child tables
    assert pos_map["public.profiles"] < pos_map["public.hazard_reports"]
    for child in [
        "public.report_images",
        "public.verifications",
        "public.comments",
        "public.escalations",
        "public.status_history",
    ]:
        assert pos_map["public.hazard_reports"] < pos_map[child]


def test_postgis_extension_and_geography_column():
    """PostGIS extension and geography(Point, 4326) location column must be defined."""
    sql = _strip_sql_comments(_read_schema())
    assert re.search(r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+postgis\s*;", sql, re.IGNORECASE)
    assert re.search(r"location\s+geography\s*\(\s*Point\s*,\s*4326\s*\)\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_hazard_reports_contains_all_spec_columns():
    """hazard_reports must include all 21 columns defined in Spec Section 7."""
    sql = _strip_sql_comments(_read_schema())
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.hazard_reports\s*\((.*?)\)\s*;",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None
    body = match.group(1)

    expected_columns = [
        "id",
        "reporter_id",
        "category",
        "description",
        "location",
        "address_text",
        "status",
        "escalation_recommended",
        "severity_score",
        "confidence",
        "confirm_count",
        "dispute_count",
        "fixed_count",
        "ai_suggested_category",
        "ai_confidence",
        "duplicate_of",
        "is_seed",
        "created_at",
        "updated_at",
        "escalated_at",
        "fixed_at",
    ]
    for col in expected_columns:
        assert re.search(rf"^\s*{col}\s+", body, re.MULTILINE), (
            f"Missing column '{col}' in hazard_reports"
        )


def test_required_domain_constraints_and_uniqueness():
    """All domain CHECK constraints, bounds, self-duplicate guard, and unique vote key exist."""
    sql = _strip_sql_comments(_read_schema())

    # Enums / value checks
    assert "('pothole', 'road_break', 'streetlight', 'footpath', 'other')" in sql
    assert "('open', 'escalated', 'fixed')" in sql
    assert "('confirm', 'dispute', 'fixed')" in sql
    assert "('user', 'admin')" in sql
    assert "('low', 'medium', 'high')" in sql
    assert "('evidence', 'after')" in sql

    # Numeric bounds & self-reference guard
    assert "severity_score >= 0 AND severity_score <= 100" in sql
    assert "ai_confidence >= 0.0 AND ai_confidence <= 1.0" in sql
    assert "duplicate_of IS NULL OR duplicate_of <> id" in sql

    # Unique (report_id, user_id, vote) via composite primary key
    assert re.search(
        r"PRIMARY\s+KEY\s*\(\s*report_id\s*,\s*user_id\s*,\s*vote\s*\)",
        sql,
        re.IGNORECASE,
    )


def test_required_indexes_present():
    """GIST location index, composite B-tree index, and FK indexes must exist."""
    sql = _strip_sql_comments(_read_schema())
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+\w+\s+ON\s+public\.hazard_reports\s+USING\s+GIST\s*\(\s*location\s*\)",
        sql,
        re.IGNORECASE,
    )
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+\w+\s+ON\s+public\.hazard_reports\s*\(\s*status\s*,\s*category\s*,\s*created_at",
        sql,
        re.IGNORECASE,
    )
    assert "idx_hazard_reports_reporter_id" in sql
    assert "idx_report_images_report_id" in sql
    assert "idx_verifications_user_id" in sql
    assert "idx_comments_report_id_created" in sql
    assert "idx_escalations_report_id" in sql
    assert "idx_status_history_report_id_created" in sql


def test_signup_trigger_is_secure_and_prevents_role_escalation():
    """Auth signup trigger must use SECURITY DEFINER, fixed search_path, and hardcoded 'user' role."""
    sql = _strip_sql_comments(_read_schema())
    fn_match = re.search(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.handle_new_user\s*\(\)(.*?)\$\$\s*;",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert fn_match is not None, "Missing public.handle_new_user() function"
    fn_body = fn_match.group(1)

    assert re.search(r"SECURITY\s+DEFINER", fn_body, re.IGNORECASE)
    assert re.search(r"SET\s+search_path\s*=\s*public", fn_body, re.IGNORECASE)
    assert re.search(r"VALUES\s*\(\s*NEW\.id\s*,\s*v_full_name\s*,\s*'user'\s*\)", fn_body)
    assert re.search(r"ON\s+CONFLICT\s*\(\s*id\s*\)\s+DO\s+NOTHING", fn_body, re.IGNORECASE)
    # Never extract 'role' from user-supplied metadata
    assert "'role'" not in fn_body
    assert re.search(
        r"CREATE\s+TRIGGER\s+on_auth_user_created\s+AFTER\s+INSERT\s+ON\s+auth\.users",
        sql,
        re.IGNORECASE,
    )


def test_realtime_publication_adds_only_hazard_reports():
    """Realtime publication block must idempotently add only public.hazard_reports."""
    sql = _strip_sql_comments(_read_schema())
    alter_matches = re.findall(
        r"ALTER\s+PUBLICATION\s+supabase_realtime\s+ADD\s+TABLE\s+([\w\.]+)",
        sql,
        re.IGNORECASE,
    )
    assert alter_matches == ["public.hazard_reports"]
    assert "pg_publication_tables" in sql


def test_strict_stage_3_scope_boundaries():
    """schema.sql must not contain Stage 4 functions, RLS policies, seed data, or destructive DDL."""
    sql = _strip_sql_comments(_read_schema())
    forbidden_patterns = [
        r"\breports_in_bounds\b",
        r"\bnearby_reports\b",
        r"\bhazards_near_route\b",
        r"\bCREATE\s+POLICY\b",
        r"\bENABLE\s+ROW\s+LEVEL\s+SECURITY\b",
        r"\bDROP\s+TABLE\b",
        r"\bTRUNCATE\b",
        r"INSERT\s+INTO\s+public\.hazard_reports",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, sql, re.IGNORECASE), (
            f"Stage 3 boundary violated: found forbidden pattern '{pattern}'"
        )
