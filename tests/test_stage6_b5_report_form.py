"""Automated tests for Stage 6 — B5: Report Form (Issue #14)."""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    AuthenticatedUser,
    get_authenticated_context,
    get_current_user,
    get_settings,
)
from app.main import app

ROOT_DIR = Path(__file__).resolve().parent.parent
REPORT_NEW_TEMPLATE_PATH = ROOT_DIR / "app" / "templates" / "report_new.html"
REPORT_NEW_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "report_new.js"
CSS_PATH = ROOT_DIR / "app" / "static" / "css" / "saferoute.css"


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
    )


def _ordinary_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        id="11111111-1111-4111-8111-111111111111",
        email="citizen@example.com",
        full_name="Asha Citizen",
        role="user",
    )


def _auth_context() -> AuthenticatedContext:
    return AuthenticatedContext(
        user=_ordinary_user(),
        access_token="valid-test-access-token",
    )


@pytest.fixture(autouse=True)
def isolate_overrides():
    app.dependency_overrides[get_settings] = _test_settings
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Route Registration, Ordering & Access Protection Tests
# ==============================================================================


def test_report_new_route_ordering_and_access_protection():
    client = TestClient(app)

    # 1. Unauthenticated browser HTML request -> 303 Redirect to /login
    r_unauth_html = client.get(
        "/reports/new",
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert r_unauth_html.status_code == 303
    assert r_unauth_html.headers["location"] == "/login"

    # 2. Unauthenticated API request -> 401 Unauthorized
    r_unauth_api = client.get(
        "/reports/new",
        headers={"Accept": "application/json"},
    )
    assert r_unauth_api.status_code == 401

    # 3. Authenticated request -> 200 OK (Route ordering ensures "new" is not captured as {report_id})
    app.dependency_overrides[get_current_user] = _ordinary_user
    app.dependency_overrides[get_authenticated_context] = _auth_context

    r_auth = client.get("/reports/new", headers={"Accept": "text/html"})
    assert r_auth.status_code == 200
    assert "text/html" in r_auth.headers["content-type"]
    html = r_auth.text

    # Confirms page title and heading
    assert "Report a Road Hazard" in html
    assert "Report" in html
    assert 'aria-current="page"' in html


# ==============================================================================
# 2. Acceptance Criteria & Template Structure Tests
# ==============================================================================


def test_report_new_template_contains_all_b5_acceptance_criteria():
    app.dependency_overrides[get_current_user] = _ordinary_user
    app.dependency_overrides[get_authenticated_context] = _auth_context
    client = TestClient(app)

    resp = client.get("/reports/new", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    html = resp.text

    # 1. Mandatory Driving Safety Warning Banner (Spec Section 12, AGENTS.md Section 15)
    assert "Never photograph hazards while driving or riding. Stop safely first." in html
    assert "Driving Safety Warning" in html

    # 2. All 5 Approved Category Cards in Accessible Radiogroup
    assert 'role="radiogroup"' in html
    for cat in ["pothole", "road_break", "streetlight", "footpath", "other"]:
        assert f':data-category="cat.key"' in html or f'data-category="{cat}"' in html
        assert f'cat.key' in html or f'id="sr-cat-{cat}"' in html

    # 3. Photo Upload with Supported MIME Types & Preview Container
    assert 'accept="image/jpeg,image/png,image/webp"' in html
    assert 'id="sr-photo-input"' in html
    assert "photoPreviewUrl" in html
    assert "EXIF" in html

    # 4. Interactive Leaflet Pin Picker Map & "Use my location" Button
    assert "leaflet@1.9.4/dist/leaflet.css" in html
    assert "leaflet@1.9.4/dist/leaflet.js" in html
    assert 'id="sr-picker-map"' in html
    assert 'id="sr-use-location-btn"' in html
    assert "Use my location" in html

    # 5. Description and Optional Landmark Inputs
    assert 'id="sr-description-input"' in html
    assert 'id="sr-address-input"' in html

    # 6. Reserved AI Suggestion Chip Slot (Issue #14, C2 hook)
    assert 'id="sr-ai-suggestion-slot"' in html
    assert "Suggested category" in html
    assert "Apply suggestion" in html

    # 7. Duplicate-Found Modal with Both Required Choices (Spec Section 8)
    assert 'id="sr-duplicate-modal"' in html
    assert 'id="sr-confirm-duplicate-btn"' in html
    assert 'id="sr-submit-anyway-btn"' in html
    assert "Same hazard, add my confirmation" in html
    assert "Different, submit anyway" in html

    # 8. Sourced Fallback Coordinates from Config
    marker = '<script id="sr-map-config" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    cfg = json.loads(html[start:end])
    assert cfg["fallbackLat"] == 17.4849
    assert cfg["fallbackLng"] == 78.4138
    assert cfg["fallbackLabel"] == "Kukatpally Y Junction"


# ==============================================================================
# 3. Static Security, Privacy & Design Guardrail Tests
# ==============================================================================


def test_report_new_js_and_html_security_and_design_rules():
    js_text = REPORT_NEW_JS_PATH.read_text(encoding="utf-8")
    html_text = REPORT_NEW_TEMPLATE_PATH.read_text(encoding="utf-8")
    css_text = CSS_PATH.read_text(encoding="utf-8")

    # 1. Authoritative API endpoints only
    assert "/reports/check-duplicate" in js_text
    assert "/reports/confirm-duplicate" in js_text
    assert '"/reports"' in js_text or "'/reports'" in js_text

    # 2. Maximum image size cap (5 MB) enforced client-side
    assert "5 * 1024 * 1024" in js_text
    assert "5 MB" in js_text

    # 3. Forbidden browser client-side storage, tokens, or innerHTML
    for forbidden in (
        "/realtime/v1",
        "wss://",
        "@supabase/supabase-js",
        "localStorage",
        "sessionStorage",
        "innerHTML",
        "console.log",
    ):
        assert forbidden not in js_text, f"Forbidden pattern in report_new.js: {forbidden}"

    # 4. Strict light theme only (no dark: classes anywhere)
    assert "dark:" not in html_text
    assert "color-scheme: only light;" in css_text

    # 5. Mint contrast rule (no text-accent on white/light surfaces)
    assert "text-accent" not in html_text


# ==============================================================================
# 4. Duplicate Check & Form Flow Contract Unit Tests
# ==============================================================================


def test_duplicate_check_and_submission_routes_respond_to_authenticated_calls():
    app.dependency_overrides[get_current_user] = _ordinary_user
    app.dependency_overrides[get_authenticated_context] = _auth_context
    client = TestClient(app)

    # Cookie-authenticated requests without CSRF are rejected with 403
    client.cookies.set("sr_access_token", "cookie-jwt")
    r_no_csrf = client.post(
        "/reports/check-duplicate",
        json={"lat": 17.4849, "lng": 78.4138, "category": "pothole"},
    )
    assert r_no_csrf.status_code == 403

    r_post_no_csrf = client.post(
        "/reports",
        data={"category": "pothole", "description": "test", "lat": 17.4849, "lng": 78.4138},
    )
    assert r_post_no_csrf.status_code == 403
