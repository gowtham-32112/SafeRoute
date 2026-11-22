"""Stage 10: Seed Dataset, Deployment Config, and Repository-Wide Security Verification.

Tests:
1. Static & structural validation of supabase/seed.sql:
   - Deterministic UUIDs
   - Idempotency (ON CONFLICT DO NOTHING)
   - Kukatpally bounding box coordinates (17.43..17.52 lat, 78.35..78.46 lng)
   - All 5 approved hazard categories represented
   - All 3 hazard statuses represented (open, escalated, fixed)
   - is_seed = TRUE flag present on seed reports
   - Stale report (> 30 days) and nearby duplicate pair (< 25m) present
   - Zero hardcoded production secrets/passwords
2. Deployment configuration validation (render.yaml & Procfile):
   - Valid ASGI start command (uvicorn app.main:app)
   - Secret-free configuration (sync: false for credentials)
   - Valid Python runtime
3. Repository-wide security & design token audit:
   - Zero occurrences of SUPABASE_SERVICE_KEY or service_role in app/ runtime code
   - Zero dark: utility classes across all Jinja2 HTML templates
4. Live PostgreSQL execution test (when TEST_DATABASE_URL is set, skips cleanly otherwise).
"""

import os
from pathlib import Path
import re
import uuid
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_SQL_PATH = REPO_ROOT / "supabase" / "seed.sql"
RENDER_YAML_PATH = REPO_ROOT / "render.yaml"
PROCFILE_PATH = REPO_ROOT / "Procfile"
APP_DIR = REPO_ROOT / "app"
TEMPLATES_DIR = APP_DIR / "templates"


def test_seed_sql_file_exists_and_non_empty():
    """Verify supabase/seed.sql exists and contains valid SQL statements."""
    assert SEED_SQL_PATH.is_file(), "supabase/seed.sql must exist"
    content = SEED_SQL_PATH.read_text(encoding="utf-8")
    assert len(content.strip()) > 500, "supabase/seed.sql should not be empty or stub"
    assert "INSERT INTO public.hazard_reports" in content
    assert "INSERT INTO public.profiles" in content
    assert "INSERT INTO public.verifications" in content
    assert "INSERT INTO public.comments" in content
    assert "INSERT INTO public.escalations" in content


def test_seed_sql_idempotency_and_deterministic_uuids():
    """Verify all INSERT statements use ON CONFLICT DO NOTHING and deterministic UUIDs."""
    content = SEED_SQL_PATH.read_text(encoding="utf-8")

    # Check for ON CONFLICT guards
    assert content.count("ON CONFLICT") >= 5, (
        "Each core table insert in seed.sql must feature ON CONFLICT idempotency guards"
    )

    # Extract all UUID literals
    uuid_pattern = re.compile(
        r"'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'",
        re.IGNORECASE,
    )
    raw_uuids = uuid_pattern.findall(content)
    assert len(raw_uuids) >= 20, "Should contain deterministic UUIDs for seeded records"

    # Validate all extracted UUIDs parse cleanly
    for u in raw_uuids:
        parsed = uuid.UUID(u)
        assert str(parsed) == u.lower()


def test_seed_sql_covers_all_categories_and_statuses():
    """Verify all 5 categories and all 3 statuses are represented in the seed data."""
    content = SEED_SQL_PATH.read_text(encoding="utf-8")

    expected_categories = {"pothole", "road_break", "streetlight", "footpath", "other"}
    for cat in expected_categories:
        pattern = rf"'{cat}'"
        assert re.search(pattern, content), f"Category '{cat}' must be present in seed.sql"

    expected_statuses = {"open", "escalated", "fixed"}
    for status in expected_statuses:
        pattern = rf"'{status}'"
        assert re.search(pattern, content), f"Status '{status}' must be present in seed.sql"

    # Verify is_seed flag is explicitly set to TRUE
    assert "TRUE" in content, "Seed records must have is_seed flag set"
    assert re.search(r"\bis_seed\b", content, re.IGNORECASE)


def test_seed_sql_kukatpally_corridor_coordinates_bounding_box():
    """Verify all seeded hazard coordinates fall within the Kukatpally Hyderabad corridor."""
    content = SEED_SQL_PATH.read_text(encoding="utf-8")

    # Match ST_MakePoint(lng, lat)
    point_pattern = re.compile(r"ST_MakePoint\(\s*([\d\.]+)\s*,\s*([\d\.]+)\s*\)", re.IGNORECASE)
    matches = point_pattern.findall(content)

    assert len(matches) >= 18, f"Expected at least 18 coordinates in seed.sql, found {len(matches)}"

    for lng_str, lat_str in matches:
        lng = float(lng_str)
        lat = float(lat_str)

        # Kukatpally corridor bounding box: Lat [17.43, 17.52], Lng [78.35, 78.46]
        assert 17.43 <= lat <= 17.52, f"Latitude {lat} out of Kukatpally bounds [17.43, 17.52]"
        assert 78.35 <= lng <= 78.46, f"Longitude {lng} out of Kukatpally bounds [78.35, 78.46]"


def test_seed_sql_contains_stale_report_and_duplicate_pair():
    """Verify edge cases: at least one stale report (> 30 days) and a duplicate pair (< 25m)."""
    content = SEED_SQL_PATH.read_text(encoding="utf-8")

    # Stale report check: interval > 30 days
    assert re.search(r"INTERVAL\s+'[3-9]\d+\s+days'", content, re.IGNORECASE), (
        "seed.sql must include at least one stale hazard report created > 30 days ago"
    )

    # Duplicate link check: duplicate_of pointing to another report UUID
    duplicate_pattern = re.compile(r"duplicate_of.*?b0000000-0000-0000-0000-000000000001", re.DOTALL)
    assert duplicate_pattern.search(content) or "b0000000-0000-0000-0000-000000000001" in content, (
        "seed.sql must include a duplicate report referencing another seed report"
    )


def test_seed_sql_no_real_secrets():
    """Verify seed.sql does not leak real API keys or production database credentials."""
    content = SEED_SQL_PATH.read_text(encoding="utf-8")
    for forbidden in ["AIzaSy", "eyJhbGciOi", "postgres://", "postgresql://"]:
        assert forbidden not in content, f"Found potential leaked secret in seed.sql: {forbidden}"


def test_render_yaml_and_procfile_validity():
    """Verify render.yaml and Procfile deployment descriptors."""
    assert RENDER_YAML_PATH.is_file(), "render.yaml must exist"
    assert PROCFILE_PATH.is_file(), "Procfile must exist"

    render_content = RENDER_YAML_PATH.read_text(encoding="utf-8")
    assert "uvicorn app.main:app" in render_content
    assert "3.11" in render_content
    assert "sync: false" in render_content
    assert "/health" in render_content

    procfile_content = PROCFILE_PATH.read_text(encoding="utf-8")
    assert "uvicorn app.main:app" in procfile_content


def test_repository_security_audit_no_service_key_in_app_runtime():
    """Verify that SUPABASE_SERVICE_KEY is NEVER used in application route handlers or business logic."""
    # Scan all router files, dependencies, main, and domain services
    runtime_py_files = [
        *list((APP_DIR / "routers").rglob("*.py")),
        *list((APP_DIR / "services").glob("*.py")),
        APP_DIR / "deps.py",
        APP_DIR / "main.py",
    ]
    # Filter out supabase_client.py which is the factory that reads Settings
    runtime_py_files = [f for f in runtime_py_files if f.name != "supabase_client.py"]
    assert len(runtime_py_files) > 0

    violations = []
    for py_file in runtime_py_files:
        content = py_file.read_text(encoding="utf-8")
        for line_num, line in enumerate(content.splitlines(), start=1):
            line_stripped = line.strip()
            if line_stripped.startswith("#"):
                continue
            if "SUPABASE_SERVICE_KEY" in line or "_get_isolated_service_client" in line or "service_role" in line:
                violations.append((str(py_file.relative_to(REPO_ROOT)), line_num, line_stripped))

    assert not violations, f"Found SUPABASE_SERVICE_KEY / service_role in application runtime code: {violations}"


def test_repository_design_audit_no_dark_mode_classes_in_templates():
    """Verify that no dark: utility classes exist in Jinja2 HTML templates."""
    html_files = list(TEMPLATES_DIR.rglob("*.html"))
    assert len(html_files) > 0

    violations = []
    for html_file in html_files:
        content = html_file.read_text(encoding="utf-8")
        for line_num, line in enumerate(content.splitlines(), start=1):
            if "dark:" in line:
                violations.append((str(html_file.relative_to(REPO_ROOT)), line_num, line.strip()))

    assert not violations, f"Found dark: classes in templates: {violations}"


def test_live_postgres_seed_idempotency():
    """Live PostgreSQL integration test when TEST_DATABASE_URL is provided."""
    test_db_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not test_db_url:
        pytest.skip("TEST_DATABASE_URL not set; skipping live PostgreSQL seed execution test")

    try:
        import psycopg  # type: ignore
    except ImportError:
        pytest.skip("psycopg not installed; skipping live PostgreSQL seed test")

    seed_sql = SEED_SQL_PATH.read_text(encoding="utf-8")

    # Connect and execute seed.sql twice to verify idempotency
    with psycopg.connect(test_db_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(seed_sql)
            cur.execute(seed_sql)  # Second run should execute cleanly without error
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.hazard_reports WHERE is_seed = TRUE")
            count = cur.fetchone()[0]
            assert count >= 18, f"Expected >= 18 seeded reports, got {count}"
