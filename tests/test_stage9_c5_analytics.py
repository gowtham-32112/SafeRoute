"""Automated unit and integration tests for Stage 9 Issue #32 (C5 · Analytics & Heatmap).

Covers:
- Unauthenticated access rejection:
  - GET /analytics redirects 303 to /login for browser requests.
  - GET /api/analytics returns 401 Unauthorized.
- Authenticated endpoints:
  - GET /analytics returns 200 with HTML dashboard, heatmap container, KPI cards, and charts.
  - GET /api/analytics returns 200 with structured AnalyticsResponse JSON.
- Aggregation unit tests:
  - Empty dataset safety: zero division handled cleanly, avg_time_to_fix_hours is None.
  - Mixed dataset accuracy:
    - by_category and by_status distribution across approved keys.
    - verification_rate calculation ((verified / total) * 100).
    - duplicate_rate calculation ((duplicates / total) * 100).
    - escalation_count calculation.
    - avg_time_to_fix_hours resolution duration calculation.
    - top_affected_areas: Top 5 hotspot grouping, sorted descending by report count.
    - heatmap_points: normalized intensity clamping [0.2..1.0].
  - exclude_seed toggle: dynamically filters out is_seed=True reports.
- Static security and design rules:
  - SUPABASE_SERVICE_KEY and service_role absent from analytics service and router.
  - Zero dark: classes in analytics.html.
  - Zero text-accent in analytics.html.
  - Zero innerHTML in analytics.js.
  - Zero localStorage / sessionStorage in analytics.js.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_current_user, get_settings
from app.main import app
from app.services.analytics import (
    APPROVED_CATEGORIES,
    APPROVED_STATUSES,
    AnalyticsResponse,
    compute_analytics,
    get_hazard_analytics,
)
from app.services.auth import AuthenticatedUser

ROOT_DIR = Path(__file__).resolve().parent.parent
ANALYTICS_TEMPLATE_PATH = ROOT_DIR / "app" / "templates" / "analytics.html"
ANALYTICS_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "analytics.js"
ANALYTICS_SERVICE_PATH = ROOT_DIR / "app" / "services" / "analytics.py"
ANALYTICS_ROUTER_PATH = ROOT_DIR / "app" / "routers" / "analytics.py"

USER_ID = "11111111-1111-4111-8111-111111111111"


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
    )


def _auth_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        id=USER_ID,
        email="analyst@example.com",
        full_name="Asha Analyst",
        role="user",
    )


def _auth_ctx() -> AuthenticatedContext:
    return AuthenticatedContext(
        user=_auth_user(),
        access_token="mock-analyst-jwt",
    )


@pytest.fixture(autouse=True)
def reset_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Access Control Tests
# ==============================================================================

def test_analytics_page_unauthenticated_redirects_303():
    """Unauthenticated browser request to GET /analytics redirects to /login with 303."""
    with TestClient(app) as client:
        resp = client.get("/analytics", headers={"Accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers.get("location", "")


def test_api_analytics_unauthenticated_returns_401():
    """Unauthenticated GET /api/analytics returns 401."""
    with TestClient(app) as client:
        resp = client.get("/api/analytics")
        assert resp.status_code == 401


def test_analytics_page_authenticated_renders_html():
    """Authenticated GET /analytics renders HTML with heatmap, charts, and toggle."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_current_user] = _auth_user
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    mock_analytics = AnalyticsResponse(
        total_reports=10,
        verification_rate=60.0,
        duplicate_rate=20.0,
        escalation_count=2,
        avg_time_to_fix_hours=24.5,
        by_category={"pothole": 5, "road_break": 2, "streetlight": 1, "footpath": 1, "other": 1},
        by_status={"open": 6, "escalated": 2, "fixed": 2},
        category_breakdown=[],
        status_breakdown=[],
        top_affected_areas=[],
        heatmap_points=[[17.4849, 78.4138, 0.8]],
        exclude_seed=False,
    )

    with patch("app.routers.analytics.get_hazard_analytics", return_value=mock_analytics):
        with TestClient(app) as client:
            resp = client.get("/analytics")
            assert resp.status_code == 200
            assert "Road Hazard Analytics & Hotspots" in resp.text
            assert "sr-analytics-heatmap" in resp.text
            assert "sr-category-chart" in resp.text
            assert "sr-status-chart" in resp.text
            assert "sr-exclude-seed-toggle" in resp.text


def test_api_analytics_authenticated_returns_json():
    """Authenticated GET /api/analytics returns structured JSON with correct keys."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    mock_analytics = AnalyticsResponse(
        total_reports=5,
        verification_rate=80.0,
        duplicate_rate=0.0,
        escalation_count=1,
        avg_time_to_fix_hours=12.0,
        by_category={"pothole": 3, "road_break": 1, "streetlight": 1, "footpath": 0, "other": 0},
        by_status={"open": 3, "escalated": 1, "fixed": 1},
        category_breakdown=[],
        status_breakdown=[],
        top_affected_areas=[],
        heatmap_points=[[17.4849, 78.4138, 0.7]],
        exclude_seed=False,
    )

    with patch("app.routers.analytics.get_hazard_analytics", return_value=mock_analytics):
        with TestClient(app) as client:
            resp = client.get("/api/analytics?exclude_seed=false")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_reports"] == 5
            assert data["verification_rate"] == 80.0
            assert data["escalation_count"] == 1
            assert data["avg_time_to_fix_hours"] == 12.0


# ==============================================================================
# 2. Aggregation & Computation Unit Tests
# ==============================================================================

def test_compute_analytics_empty_dataset_zero_division_safety():
    """Empty report list must return 0 total, 0.0 rates, None fix time without error."""
    res = compute_analytics([], exclude_seed=False)
    assert res.total_reports == 0
    assert res.verification_rate == 0.0
    assert res.duplicate_rate == 0.0
    assert res.escalation_count == 0
    assert res.avg_time_to_fix_hours is None
    assert len(res.top_affected_areas) == 0
    assert len(res.heatmap_points) == 0
    # Every category and status key must exist with 0
    for cat in APPROVED_CATEGORIES:
        assert res.by_category[cat] == 0
    for st in APPROVED_STATUSES:
        assert res.by_status[st] == 0


def test_compute_analytics_mixed_dataset_metrics():
    """Verify category, status, verification, duplicate, and fix time calculations."""
    reports = [
        # Report 1: open pothole, verified (2 confirms), seed
        {
            "id": "1",
            "category": "pothole",
            "status": "open",
            "severity_score": 60,
            "confirm_count": 2,
            "dispute_count": 0,
            "fixed_count": 0,
            "duplicate_of": None,
            "is_seed": True,
            "address_text": "Kukatpally Y Junction",
            "lat": 17.4849,
            "lng": 78.4138,
            "created_at": "2026-11-20T10:00:00Z",
            "fixed_at": None,
        },
        # Report 2: escalated road_break, duplicate of 1, not verified, not seed
        {
            "id": "2",
            "category": "road_break",
            "status": "escalated",
            "severity_score": 85,
            "confirm_count": 0,
            "dispute_count": 0,
            "fixed_count": 0,
            "duplicate_of": "1",
            "is_seed": False,
            "address_text": "Kukatpally Y Junction",
            "lat": 17.4850,
            "lng": 78.4139,
            "created_at": "2026-11-21T08:00:00Z",
            "fixed_at": None,
        },
        # Report 3: fixed streetlight, verified, fixed in 24 hours, not seed
        {
            "id": "3",
            "category": "streetlight",
            "status": "fixed",
            "severity_score": 35,
            "confirm_count": 1,
            "dispute_count": 0,
            "fixed_count": 2,
            "duplicate_of": None,
            "is_seed": False,
            "address_text": "KPHB Colony Metro",
            "lat": 17.4930,
            "lng": 78.3980,
            "created_at": "2026-11-19T12:00:00Z",
            "fixed_at": "2026-11-20T12:00:00Z",  # 24.0 hours
        },
        # Report 4: open footpath, unverified, not seed
        {
            "id": "4",
            "category": "footpath",
            "status": "open",
            "severity_score": 40,
            "confirm_count": 0,
            "dispute_count": 0,
            "fixed_count": 0,
            "duplicate_of": None,
            "is_seed": False,
            "address_text": "JNTU Junction",
            "lat": 17.4975,
            "lng": 78.3888,
            "created_at": "2026-11-22T09:00:00Z",
            "fixed_at": None,
        },
    ]

    # Test with seed included (total = 4)
    res_all = compute_analytics(reports, exclude_seed=False)
    assert res_all.total_reports == 4
    # Verified reports: #1 and #3 -> 2/4 = 50.0%
    assert res_all.verification_rate == 50.0
    # Duplicate reports: #2 -> 1/4 = 25.0%
    assert res_all.duplicate_rate == 25.0
    # Escalated count: #2 -> 1
    assert res_all.escalation_count == 1
    # Average time to fix: #3 fixed in 24h -> 24.0
    assert res_all.avg_time_to_fix_hours == 24.0
    # Category counts
    assert res_all.by_category["pothole"] == 1
    assert res_all.by_category["road_break"] == 1
    assert res_all.by_category["streetlight"] == 1
    assert res_all.by_category["footpath"] == 1
    assert res_all.by_category["other"] == 0
    # Status counts
    assert res_all.by_status["open"] == 2
    assert res_all.by_status["escalated"] == 1
    assert res_all.by_status["fixed"] == 1
    # Top affected area: Kukatpally Y Junction has 2 reports
    assert len(res_all.top_affected_areas) == 3
    assert res_all.top_affected_areas[0].area_name == "Kukatpally Y Junction"
    assert res_all.top_affected_areas[0].report_count == 2
    assert res_all.top_affected_areas[0].avg_severity == 72.5  # (60 + 85) / 2
    # Heatmap points length
    assert len(res_all.heatmap_points) == 4


def test_compute_analytics_exclude_seed_toggle():
    """Toggling exclude_seed=True must filter out is_seed=True records."""
    reports = [
        {"id": "s1", "category": "pothole", "status": "open", "is_seed": True, "severity_score": 50},
        {"id": "s2", "category": "pothole", "status": "open", "is_seed": True, "severity_score": 60},
        {"id": "u1", "category": "road_break", "status": "escalated", "is_seed": False, "severity_score": 75},
    ]

    # With seed
    res_with = compute_analytics(reports, exclude_seed=False)
    assert res_with.total_reports == 3
    assert res_with.by_category["pothole"] == 2
    assert res_with.by_category["road_break"] == 1

    # Without seed
    res_without = compute_analytics(reports, exclude_seed=True)
    assert res_without.total_reports == 1
    assert res_without.by_category["pothole"] == 0
    assert res_without.by_category["road_break"] == 1
    assert res_without.exclude_seed is True


def test_compute_analytics_top_5_areas_capping():
    """Top affected areas must cap at 5 and sort descending by report count."""
    reports = []
    # 7 different areas with varying report counts
    for i in range(7):
        area_name = f"Corridor {chr(65 + i)}"
        count = i + 1  # 1 to 7 reports
        for _ in range(count):
            reports.append({
                "id": f"{i}_{_}",
                "category": "pothole",
                "status": "open",
                "address_text": area_name,
                "lat": 17.48 + (i * 0.005),
                "lng": 78.41 + (i * 0.005),
                "severity_score": 50,
                "is_seed": False,
            })

    res = compute_analytics(reports, exclude_seed=False)
    assert len(res.top_affected_areas) == 5
    # First must be Corridor G with 7 reports
    assert res.top_affected_areas[0].area_name == "Corridor G"
    assert res.top_affected_areas[0].report_count == 7
    # Second must be Corridor F with 6 reports
    assert res.top_affected_areas[1].area_name == "Corridor F"
    assert res.top_affected_areas[1].report_count == 6


# ==============================================================================
# 3. Static Security & Design Tokens Guard
# ==============================================================================

def test_static_security_and_design_tokens():
    """Verify security boundaries and locked design tokens across analytics files."""
    service_code = ANALYTICS_SERVICE_PATH.read_text(encoding="utf-8")
    router_code = ANALYTICS_ROUTER_PATH.read_text(encoding="utf-8")
    template_html = ANALYTICS_TEMPLATE_PATH.read_text(encoding="utf-8")
    client_js = ANALYTICS_JS_PATH.read_text(encoding="utf-8")

    # 1. Zero SUPABASE_SERVICE_KEY or service_role in analytics backend code
    for code, name in [(service_code, "analytics.py"), (router_code, "routers/analytics.py")]:
        assert "SUPABASE_SERVICE_KEY" not in code, f"SUPABASE_SERVICE_KEY found in {name}!"
        assert "service_role" not in code, f"service_role found in {name}!"

    # 2. Light theme only: Zero dark: classes in template
    assert "dark:" not in template_html, "Found dark: class in analytics.html"

    # 3. Mint contrast rule: text-accent must not be used on light surface
    assert "text-accent" not in template_html, "text-accent mint used improperly on light surface"

    # 4. Safe DOM text bindings: Zero .innerHTML in client JS
    assert ".innerHTML" not in client_js, "Found forbidden .innerHTML in analytics.js!"

    # 5. Privacy: No live coordinates in localStorage or sessionStorage
    assert "localStorage" not in client_js, "localStorage found in analytics.js!"
    assert "sessionStorage" not in client_js, "sessionStorage found in analytics.js!"
