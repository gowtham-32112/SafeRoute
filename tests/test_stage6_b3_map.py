"""Backend, access, config, and privacy tests for Stage 6 Issue #12 (B3 · Map page)."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import get_current_user, get_settings
from app.main import app
from app.services.auth import AuthenticatedUser

ROOT_DIR = Path(__file__).resolve().parent.parent
MAP_TEMPLATE_PATH = ROOT_DIR / "app" / "templates" / "map.html"
MAP_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "map.js"
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
        full_name="Asha Rao",
        role="user",
    )


def _admin_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        id="22222222-2222-4222-8222-222222222222",
        email="admin@saferoute.demo",
        full_name="Demo Admin",
        role="admin",
    )


@pytest.fixture(autouse=True)
def isolate_overrides():
    app.dependency_overrides[get_settings] = _test_settings
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Authentication & Authorization Access Tests
# ==============================================================================


def test_map_redirects_unauthenticated_browser_and_rejects_unauthenticated_api():
    client = TestClient(app)

    # Browser HTML request -> 303 redirect to /login
    r_html = client.get(
        "/map",
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert r_html.status_code == 303
    assert r_html.headers["location"] == "/login"

    # API / JSON request -> 401 Unauthorized
    r_json = client.get("/map", headers={"Accept": "application/json"})
    assert r_json.status_code == 401


def test_map_renders_for_authenticated_user_with_approved_config_and_controls():
    app.dependency_overrides[get_current_user] = _ordinary_user
    client = TestClient(app)

    resp = client.get("/map", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text

    # Reuses B1 base layout & marks Map active without exposing admin links
    assert "SafeRoute" in html
    assert 'href="/map"\n              aria-current="page"' in html
    assert 'href="/admin/escalations"' not in html
    assert "Escalations" not in html

    # Leaflet, map.js, map container, controls, and mobile bottom sheet present
    assert "leaflet@1.9.4/dist/leaflet.css" in html
    assert "leaflet@1.9.4/dist/leaflet.js" in html
    assert "/static/js/map.js" in html
    assert 'id="saferoute-map"' in html
    assert 'id="sr-recenter-btn"' in html
    assert 'id="sr-sidebar-recenter-btn"' in html
    assert "Re-centre on me" in html
    assert "You are here" in html
    assert 'id="sr-sheet-toggle"' in html
    assert 'id="sr-map-sheet-body"' in html

    # Approved fallback coordinates and 5-second timeout sourced from app/config.py
    marker = '<script id="sr-map-config" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    cfg = json.loads(html[start:end])

    assert cfg["fallbackLat"] == 17.4849
    assert cfg["fallbackLng"] == 78.4138
    assert cfg["fallbackLabel"] == "Kukatpally Y Junction"
    assert cfg["defaultZoom"] == 16
    assert cfg["geolocationTimeoutMs"] == 5000
    assert cfg["boundsDebounceMs"] == 400


def test_map_renders_admin_navigation_for_admin_user():
    app.dependency_overrides[get_current_user] = _admin_user
    client = TestClient(app)

    resp = client.get("/map", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert 'href="/admin/escalations"' in resp.text
    assert "Escalations" in resp.text


# ==============================================================================
# 2. Privacy, Security & Design System Guardrails
# ==============================================================================


def test_map_js_serves_cleanly_and_enforces_strict_location_privacy():
    client = TestClient(app)
    r_js = client.get("/static/js/map.js")
    assert r_js.status_code == 200
    assert "SafeRouteMap" in r_js.text

    js_text = MAP_JS_PATH.read_text(encoding="utf-8")

    # Never persist, transmit, log, or expose live location or auth tokens
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "fetch(",
        "XMLHttpRequest",
        "sendBeacon",
        "console.log",
        "console.info",
        "console.debug",
        "history.pushState",
        "history.replaceState",
        "sr_access_token",
        "sr_refresh_token",
        "SUPABASE_SERVICE_KEY",
    ):
        assert forbidden not in js_text, f"Forbidden privacy/security token in map.js: {forbidden}"

    # Must not duplicate hardcoded fallback coordinates in map.js (reads #sr-map-config)
    assert "17.4849" not in js_text
    assert "78.4138" not in js_text
    assert "sr-map-config" in js_text
    assert "isValidCoordinates" in js_text
    assert 'LIVE_MARKER_TITLE = "You are here"' in js_text
    assert 'FALLBACK_MARKER_TITLE = "Default map location"' in js_text
    assert "not your confirmed physical location" in js_text
    assert "userMarker.setIcon(" in js_text


def test_map_template_distinguishes_live_and_fallback_marker_labels():
    app.dependency_overrides[get_current_user] = _ordinary_user
    client = TestClient(app)

    resp = client.get("/map", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    html = resp.text

    assert 'id="sr-marker-legend-title"' in html
    assert ">Default map location</p>" in html
    assert "locationMode === 'live' ? 'You are here' : 'Default map location'" in html
    assert "not your confirmed physical location" in html


def test_map_template_and_css_follow_b1_light_theme_and_mint_contrast_rules():
    html_text = MAP_TEMPLATE_PATH.read_text(encoding="utf-8")
    css_text = CSS_PATH.read_text(encoding="utf-8")

    assert "dark:" not in html_text
    assert "text-accent" not in html_text
    assert "color-scheme: only light;" in css_text
    assert ".sr-map-workspace" in css_text
    assert ".sr-user-location-marker" in css_text
