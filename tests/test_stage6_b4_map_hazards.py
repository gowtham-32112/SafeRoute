"""Automated backend, security, and static contract tests for Stage 6 Issue #13 (B4)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_current_user, get_settings
from app.main import app
from app.services.auth import AuthenticatedUser

ROOT_DIR = Path(__file__).resolve().parent.parent
MAP_TEMPLATE_PATH = ROOT_DIR / "app" / "templates" / "map.html"
HAZARDS_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "map_hazards.js"
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


def _auth_ctx() -> AuthenticatedContext:
    return AuthenticatedContext(user=_ordinary_user(), access_token="rls-user-jwt-secret")


@pytest.fixture(autouse=True)
def isolate_overrides():
    app.dependency_overrides[get_settings] = _test_settings
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Map Page B4 UI, Filter Chips, Clustering & Asset Wiring
# ==============================================================================


def test_map_page_includes_b4_clustering_filters_live_badge_and_side_list():
    app.dependency_overrides[get_current_user] = _ordinary_user
    client = TestClient(app)

    resp = client.get("/map", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    html = resp.text

    # Clustering plugin & B4 module loaded alongside B3 map.js
    assert "leaflet.markercluster@1.5.3" in html
    assert "/static/js/map.js" in html
    assert "/static/js/map_hazards.js" in html

    # Filter controls, live badge, UX states & synchronized list
    assert 'id="sr-hazards-panel"' in html
    assert 'id="sr-category-filters"' in html
    assert 'id="sr-status-filters"' in html
    assert 'id="sr-show-fixed-toggle"' in html
    assert 'id="sr-reset-filters-btn"' in html
    assert 'id="sr-live-status-badge"' in html
    assert 'id="sr-hazards-loading"' in html
    assert 'id="sr-hazards-error"' in html
    assert 'id="sr-hazards-empty"' in html
    assert 'id="sr-hazards-list"' in html
    assert "Show fixed" in html
    assert "Stale – may no longer be accurate" in html

    # Static asset serves cleanly
    r_js = client.get("/static/js/map_hazards.js")
    assert r_js.status_code == 200
    assert "SafeRouteHazards" in r_js.text


# ==============================================================================
# 2. Option R1 SSE & Bounds API Security + Contract Verification
# ==============================================================================


def test_unauthenticated_sse_and_bounds_endpoints_are_rejected():
    client = TestClient(app)
    assert client.get("/api/reports/stream").status_code == 401
    assert client.get("/api/reports?bounds=17.48,78.41,17.49,78.42").status_code == 401
    # Unbounded query without bounds parameter rejected
    app.dependency_overrides[get_authenticated_context] = _auth_ctx
    assert client.get("/api/reports").status_code == 422


def test_sse_stream_endpoint_headers_and_redacted_events():
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    async def _fake_stream(**kwargs):
        # Verify caller's RLS JWT is passed server-side only, never emitted
        assert kwargs["access_token"] == "rls-user-jwt-secret"
        yield 'event: connected\ndata: {"status":"subscribed"}\n\n'
        yield (
            'event: hazard_change\n'
            'data: {"event_type":"INSERT","id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",'
            '"report":{"id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","category":"pothole",'
            '"description":"Live hole","lat":17.4849,"lng":78.4138,"status":"open",'
            '"severity_score":72,"severity_band":"high","confidence":"medium"}}\n\n'
        )

    with patch("app.routers.reports.stream_hazard_updates", side_effect=_fake_stream):
        client = TestClient(app)
        resp = client.get("/api/reports/stream")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "no-cache" in resp.headers["cache-control"]
    assert "event: connected" in resp.text
    assert "event: hazard_change" in resp.text
    assert "rls-user-jwt-secret" not in resp.text


def test_bounds_api_supports_default_single_category_and_single_status_filters():
    app.dependency_overrides[get_authenticated_context] = _auth_ctx
    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value = SimpleNamespace(data=[])
    client = TestClient(app)

    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_client,
    ):
        r_all = client.get("/api/reports?bounds=17.48,78.41,17.49,78.42")
        assert r_all.status_code == 200
        r_cat = client.get("/api/reports?bounds=17.48,78.41,17.49,78.42&category=road_break")
        assert r_cat.status_code == 200
        r_fixed = client.get("/api/reports?bounds=17.48,78.41,17.49,78.42&status=fixed")
        assert r_fixed.status_code == 200

    assert mock_client.rpc.call_count == 3


# ==============================================================================
# 3. Frontend Module Security, Privacy & Design System Guardrails
# ==============================================================================


def test_map_hazards_js_enforces_r1_sse_safe_dom_and_zero_token_exposure():
    js_text = HAZARDS_JS_PATH.read_text(encoding="utf-8")

    # Approved Option R1 SSE endpoint only
    assert 'new window.EventSource("/api/reports/stream")' in js_text
    assert "boundsDebounceMs" in js_text
    assert "AbortController" in js_text
    assert "pagehide" in js_text
    assert "beforeunload" in js_text
    assert "markerClusterGroup" in js_text

    # Exact Spec Section 5 severity hex colours
    assert "#2E9E5B" in js_text
    assert "#F5A524" in js_text
    assert "#D92D20" in js_text

    # Forbidden browser-side Supabase Realtime, geolocation, storage, tokens, or unsafe innerHTML
    for forbidden in (
        "/realtime/v1",
        "wss://",
        "@supabase/supabase-js",
        "navigator.geolocation",
        "localStorage",
        "sessionStorage",
        "sr_access_token",
        "sr_refresh_token",
        "SUPABASE_SERVICE_KEY",
        "innerHTML",
        "console.log",
    ):
        assert forbidden not in js_text, f"Forbidden pattern in map_hazards.js: {forbidden}"

    # Light theme & mint contrast rules preserved in map.html and saferoute.css
    html_text = MAP_TEMPLATE_PATH.read_text(encoding="utf-8")
    css_text = CSS_PATH.read_text(encoding="utf-8")
    assert "dark:" not in html_text
    assert "text-accent" not in html_text
    assert "color-scheme: only light;" in css_text
