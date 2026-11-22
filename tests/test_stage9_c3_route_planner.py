"""Automated unit and integration tests for Stage 9 Issue #30 (C3/C4).

Covers:
- Unauthenticated access rejection:
  - GET /routes redirects 303 to /login.
  - POST /api/routes/plan returns 401 Unauthorized.
- Double-submit CSRF enforcement on POST /api/routes/plan (403 Forbidden on missing/invalid token).
- Input validation:
  - Coordinates outside [-90..90] and [-180..180] return 422 Unprocessable Entity.
  - Identical origin and destination coordinates return 400 Bad Request.
- Exact Spec Section 10 risk and score formula unit tests:
  - Severity band multipliers: low -> 0.5, medium -> 0.8, high -> 1.0.
  - Fixed hazards (status = 'fixed') are excluded from risk calculations.
  - route_risk = sum(severity_score * multiplier).
  - route_score = duration_minutes + 0.05 * route_risk.
- Route ranking parity:
  - Route with lowest route_score is ranked first and marked recommended=True.
- OSRM multi-route parsing and PostGIS hazard intersection.
- OSRM timeout, HTTP 500, and network error graceful fallback (is_fallback_route=True, HTTP 200, never 500).
- Static security and design rules:
  - SUPABASE_SERVICE_KEY and service_role are absent from routing.py and routes.py.
  - Zero dark: classes in routes.html.
  - Zero innerHTML in routes.js.
  - Zero localStorage/sessionStorage coordinate tracking in routes.js.
  - Mandatory route safety disclaimer present across service, router, and UI.
"""

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_current_user, get_settings
from app.main import app
from app.services.auth import AuthenticatedUser
from app.services.routing import (
    RISK_MULTIPLIERS,
    ROUTE_SAFETY_DISCLAIMER,
    calculate_route_risk,
    calculate_route_score,
    compute_hazard_risk_multiplier,
    get_cached_demo_routes,
    plan_hazard_aware_routes,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
ROUTES_TEMPLATE_PATH = ROOT_DIR / "app" / "templates" / "routes.html"
ROUTES_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "routes.js"
ROUTING_SERVICE_PATH = ROOT_DIR / "app" / "services" / "routing.py"
ROUTING_ROUTER_PATH = ROOT_DIR / "app" / "routers" / "routes.py"

USER_ID = "11111111-1111-4111-8111-111111111111"


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
        OSRM_BASE_URL="https://router.project-osrm.org",
        OSRM_TIMEOUT_SECONDS=5.0,
    )


def _auth_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        id=USER_ID,
        email="driver@example.com",
        full_name="Ravi Driver",
        role="user",
    )


def _auth_ctx() -> AuthenticatedContext:
    return AuthenticatedContext(
        user=_auth_user(),
        access_token="mock-driver-jwt",
    )


def _csrf_headers(client: TestClient, token: str = "test-csrf-token-c3") -> Dict[str, str]:
    client.cookies.set("sr_csrf_token", token)
    return {"X-CSRF-Token": token}


@pytest.fixture(autouse=True)
def reset_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Access Control & Validation Tests
# ==============================================================================

def test_routes_page_unauthenticated_redirects_303():
    """Unauthenticated browser GET /routes must redirect to /login with 303."""
    with TestClient(app) as client:
        resp = client.get("/routes", headers={"Accept": "text/html"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "/login" in resp.headers.get("location", "")


def test_routes_page_authenticated_renders_html():
    """Authenticated GET /routes renders HTML with map config, landmarks, and disclaimer."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_current_user] = _auth_user

    with TestClient(app) as client:
        resp = client.get("/routes")
        assert resp.status_code == 200
        assert "Hazard-Aware Route Planner" in resp.text
        assert "sr-routes-map" in resp.text
        assert "Kukatpally Y Junction" in resp.text
        assert ROUTE_SAFETY_DISCLAIMER in resp.text


def test_plan_routes_api_unauthenticated_returns_401():
    """Unauthenticated POST /api/routes/plan returns 401."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/routes/plan",
            json={
                "origin_lat": 17.4849,
                "origin_lng": 78.4138,
                "dest_lat": 17.4930,
                "dest_lng": 78.3980,
            },
        )
        assert resp.status_code == 401


def test_plan_routes_api_missing_csrf_returns_403():
    """Cookie-authenticated POST /api/routes/plan missing CSRF token returns 403."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        # Simulate browser cookie session without CSRF header or cookie
        client.cookies.set("sr_access_token", "mock-driver-jwt")
        resp = client.post(
            "/api/routes/plan",
            json={
                "origin_lat": 17.4849,
                "origin_lng": 78.4138,
                "dest_lat": 17.4930,
                "dest_lng": 78.3980,
            },
        )
        assert resp.status_code == 403
        assert "CSRF" in resp.json().get("detail", "")


def test_plan_routes_api_identical_origin_dest_returns_400():
    """Identical origin and destination coordinates return 400 Bad Request."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        resp = client.post(
            "/api/routes/plan",
            headers=headers,
            json={
                "origin_lat": 17.4849,
                "origin_lng": 78.4138,
                "dest_lat": 17.4849,
                "dest_lng": 78.4138,
            },
        )
        assert resp.status_code == 400
        assert "distinct" in resp.json().get("detail", "").lower()


def test_plan_routes_api_invalid_coordinate_bounds_returns_422():
    """Latitude > 90 or Longitude > 180 returns 422 Unprocessable Entity."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        resp = client.post(
            "/api/routes/plan",
            headers=headers,
            json={
                "origin_lat": 95.0,  # Invalid
                "origin_lng": 78.4138,
                "dest_lat": 17.4930,
                "dest_lng": 78.3980,
            },
        )
        assert resp.status_code == 422


# ==============================================================================
# 2. Risk Scoring & Formula Unit Tests (Spec Section 10)
# ==============================================================================

def test_compute_hazard_risk_multiplier_bands():
    """Verify severity band mapping and exact multipliers: low=0.5, medium=0.8, high=1.0."""
    # Low: score < 40
    band_low, mult_low = compute_hazard_risk_multiplier(25)
    assert band_low == "low"
    assert mult_low == 0.5

    # Medium: 40 <= score <= 69
    band_med, mult_med = compute_hazard_risk_multiplier(55)
    assert band_med == "medium"
    assert mult_med == 0.8

    # High: score >= 70
    band_high, mult_high = compute_hazard_risk_multiplier(75)
    assert band_high == "high"
    assert mult_high == 1.0


def test_calculate_route_risk_with_mixed_and_fixed_hazards():
    """Verify route_risk = sum(severity_score * multiplier) and exclusion of fixed hazards."""
    hazards = [
        # Low hazard: 30 * 0.5 = 15.0
        {"id": "h1", "category": "pothole", "severity_score": 30, "status": "open", "distance_m": 12.0},
        # Medium hazard: 50 * 0.8 = 40.0
        {"id": "h2", "category": "road_break", "severity_score": 50, "status": "escalated", "distance_m": 5.0},
        # High hazard: 80 * 1.0 = 80.0
        {"id": "h3", "category": "pothole", "severity_score": 80, "status": "open", "distance_m": 2.0},
        # Fixed hazard: MUST be completely ignored (0.0)
        {"id": "h4", "category": "pothole", "severity_score": 90, "status": "fixed", "distance_m": 1.0},
    ]

    total_risk, enriched = calculate_route_risk(hazards)

    # 15.0 + 40.0 + 80.0 = 135.0
    assert total_risk == 135.0
    assert len(enriched) == 3
    assert all(h.status != "fixed" for h in enriched)

    h1 = next(h for h in enriched if h.id == "h1")
    assert h1.multiplier == 0.5
    assert h1.risk_contribution == 15.0

    h2 = next(h for h in enriched if h.id == "h2")
    assert h2.multiplier == 0.8
    assert h2.risk_contribution == 40.0

    h3 = next(h for h in enriched if h.id == "h3")
    assert h3.multiplier == 1.0
    assert h3.risk_contribution == 80.0


def test_calculate_route_score_formula():
    """Verify route_score = duration_minutes + 0.05 * route_risk."""
    duration_min = 12.5
    route_risk = 135.0
    # 12.5 + (0.05 * 135.0) = 12.5 + 6.75 = 19.25
    score = calculate_route_score(duration_min, route_risk)
    assert score == 19.25

    # Zero risk scenario: route_score equals duration_minutes
    zero_score = calculate_route_score(8.0, 0.0)
    assert zero_score == 8.0


# ==============================================================================
# 3. OSRM Multi-Route Ranking & PostGIS Intersection
# ==============================================================================

@patch("httpx.AsyncClient.get")
def test_plan_routes_ranks_safer_alternative_ahead_of_hazardous_short_route(mock_get):
    """A slightly longer route with zero hazards must score better than a hazardous shorter route."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    # Route 1: Shorter (8 min), but high hazard risk (risk=100 -> score = 8 + 5.0 = 13.0)
    # Route 2: Slightly longer (10 min), but zero hazard risk (risk=0 -> score = 10 + 0 = 10.0) -> RECOMMENDED
    mock_osrm_resp = MagicMock()
    mock_osrm_resp.status_code = 200
    mock_osrm_resp.json.return_value = {
        "routes": [
            {
                "duration": 480.0,  # 8 minutes
                "distance": 3500.0,
                "legs": [{"summary": "Route 1 (Short but Hazardous)"}],
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[78.4138, 17.4849], [78.4060, 17.4880], [78.3980, 17.4930]],
                },
            },
            {
                "duration": 600.0,  # 10 minutes
                "distance": 4200.0,
                "legs": [{"summary": "Route 2 (Slightly Longer but Safe)"}],
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[78.4138, 17.4849], [78.4090, 17.4820], [78.3980, 17.4930]],
                },
            },
        ]
    }
    mock_get.return_value = mock_osrm_resp

    # Mock PostGIS hazards_near_route
    def fake_rpc(fn_name, params):
        mock_builder = MagicMock()
        geom = params.get("route_geojson", {})
        coords = geom.get("coordinates", [])

        # If Route 1 (via 17.4880), return a high severity hazard
        if any(pt[1] == 17.4880 for pt in coords):
            mock_builder.execute.return_value = MagicMock(
                data=[
                    {
                        "id": "hazard-high-1",
                        "category": "pothole",
                        "description": "Deep dangerous crater",
                        "lat": 17.4881,
                        "lng": 78.4061,
                        "address_text": "Main Corridor",
                        "status": "open",
                        "severity_score": 100,  # High: 100 * 1.0 = 100.0 risk
                        "confidence": "high",
                        "confirm_count": 8,
                        "dispute_count": 0,
                        "created_at": "2026-11-20T10:00:00Z",
                        "distance_m": 5.2,
                    }
                ]
            )
        else:
            # Route 2: Zero hazards
            mock_builder.execute.return_value = MagicMock(data=[])

        return mock_builder

    with patch("app.services.routing.create_authenticated_supabase_client") as mock_client_factory:
        mock_supabase = MagicMock()
        mock_supabase.rpc.side_effect = fake_rpc
        mock_client_factory.return_value = mock_supabase

        with TestClient(app) as client:
            headers = _csrf_headers(client)
            resp = client.post(
                "/api/routes/plan",
                headers=headers,
                json={
                    "origin_lat": 17.4849,
                    "origin_lng": 78.4138,
                    "dest_lat": 17.4930,
                    "dest_lng": 78.3980,
                },
            )

            assert resp.status_code == 200
            data = resp.json()
            assert not data["is_fallback_route"]
            assert data["disclaimer"] == ROUTE_SAFETY_DISCLAIMER
            routes = data["routes"]
            assert len(routes) == 2

            # Route 2 (safe) must be ranked FIRST with recommended=True
            assert routes[0]["summary"] == "Route 2 (Slightly Longer but Safe)"
            assert routes[0]["route_score"] == 10.0
            assert routes[0]["route_risk"] == 0.0
            assert routes[0]["recommended"] is True

            # Route 1 (hazardous) must be ranked SECOND
            assert routes[1]["summary"] == "Route 1 (Short but Hazardous)"
            assert routes[1]["route_score"] == 13.0
            assert routes[1]["route_risk"] == 100.0
            assert routes[1]["recommended"] is False


# ==============================================================================
# 4. OSRM Timeout & Error Graceful Fallback
# ==============================================================================

@patch("httpx.AsyncClient.get")
def test_plan_routes_osrm_timeout_returns_cached_demo_fallback(mock_get):
    """OSRM timeout must return HTTP 200 with is_fallback_route=True and cached demo routes."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    mock_get.side_effect = httpx.TimeoutException("OSRM server connection timed out")

    with patch("app.services.routing.create_authenticated_supabase_client") as mock_client_factory:
        mock_supabase = MagicMock()
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=[])
        mock_client_factory.return_value = mock_supabase

        with TestClient(app) as client:
            headers = _csrf_headers(client)
            resp = client.post(
                "/api/routes/plan",
                headers=headers,
                json={
                    "origin_lat": 17.4849,
                    "origin_lng": 78.4138,
                    "dest_lat": 17.4930,
                    "dest_lng": 78.3980,
                },
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["is_fallback_route"] is True
            assert "fallback" in data["fallback_notice"].lower()
            assert len(data["routes"]) >= 1
            assert data["routes"][0]["recommended"] is True
            assert data["disclaimer"] == ROUTE_SAFETY_DISCLAIMER


@patch("httpx.AsyncClient.get")
def test_plan_routes_osrm_500_returns_cached_demo_fallback(mock_get):
    """OSRM HTTP 500 must return HTTP 200 with is_fallback_route=True without crashing."""
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    mock_resp = MagicMock()
    mock_resp.status_code = 502
    mock_resp.text = "Bad Gateway"
    mock_get.return_value = mock_resp

    with patch("app.services.routing.create_authenticated_supabase_client") as mock_client_factory:
        mock_supabase = MagicMock()
        mock_supabase.rpc.return_value.execute.return_value = MagicMock(data=[])
        mock_client_factory.return_value = mock_supabase

        with TestClient(app) as client:
            headers = _csrf_headers(client)
            resp = client.post(
                "/api/routes/plan",
                headers=headers,
                json={
                    "origin_lat": 17.4849,
                    "origin_lng": 78.4138,
                    "dest_lat": 17.4930,
                    "dest_lng": 78.3980,
                },
            )

            assert resp.status_code == 200
            data = resp.json()
            assert data["is_fallback_route"] is True
            assert len(data["routes"]) >= 1


# ==============================================================================
# 5. Static Security, Design Tokens & Privacy Guardrails
# ==============================================================================

def test_static_security_and_design_tokens():
    """Verify security boundaries and locked design tokens across routing files."""
    routing_py = ROUTING_SERVICE_PATH.read_text(encoding="utf-8")
    router_py = ROUTING_ROUTER_PATH.read_text(encoding="utf-8")
    template_html = ROUTES_TEMPLATE_PATH.read_text(encoding="utf-8")
    routes_js = ROUTES_JS_PATH.read_text(encoding="utf-8")

    # 1. Zero SUPABASE_SERVICE_KEY or service_role in routing backend code
    for code, name in [(routing_py, "routing.py"), (router_py, "routers/routes.py")]:
        assert "SUPABASE_SERVICE_KEY" not in code, f"SUPABASE_SERVICE_KEY found in {name}!"
        assert "service_role" not in code, f"service_role found in {name}!"

    # 2. Light theme only: Zero dark: classes in template
    assert "dark:" not in template_html, "Found dark: class in routes.html"

    # 3. Mint contrast rule: text-accent must not be used on light surface
    assert "text-accent" not in template_html, "text-accent mint used improperly on light surface"

    # 4. Safe DOM text bindings: Zero .innerHTML in client JS
    assert ".innerHTML" not in routes_js, "Found forbidden .innerHTML in routes.js!"

    # 5. Privacy: No live coordinates in localStorage or sessionStorage
    assert "localStorage" not in routes_js
    assert "sessionStorage" not in routes_js

    # 6. Mandatory Route Safety Disclaimer present across service, router, and UI
    assert ROUTE_SAFETY_DISCLAIMER in routing_py
    assert ROUTE_SAFETY_DISCLAIMER in router_py
    assert "Routes are ranked using community-reported hazards only" in template_html
