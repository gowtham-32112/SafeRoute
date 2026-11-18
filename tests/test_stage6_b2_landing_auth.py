"""Focused tests for Stage 6 Issue #11 (B2 · Landing, login and signup pages)."""

from pathlib import Path
import re
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import get_optional_user, get_settings
from app.main import app
from app.services.auth import AuthSession, AuthenticatedUser, InvalidCredentialsError

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT_DIR / "app" / "templates"
AUTH_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "auth_forms.js"


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
    )


@pytest.fixture(autouse=True)
def isolate_overrides():
    app.dependency_overrides[get_settings] = _test_settings
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Public Landing Page (GET /)
# ==============================================================================


def test_landing_page_renders_hero_three_feature_cards_and_no_protected_content():
    client = TestClient(app)

    # Browser HTML request receives rendered landing page
    resp = client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    html = resp.text

    # Hero section & headline
    assert 'id="hero-section"' in html
    assert "<h1" in html
    assert "SafeRoute" in html

    # Exactly three feature cards
    feature_cards = re.findall(r'data-feature-card="[^"]+"', html)
    assert len(feature_cards) == 3

    # Clear Login & Sign up calls to action
    assert 'href="/login"' in html
    assert 'href="/signup"' in html

    # Awareness & safety messaging (never guarantees routes are safe)
    lowered = html.lower()
    assert "awareness only" in lowered
    assert "never photograph" in lowered
    assert "guarantee" in lowered

    # Logged-out visitors MUST NOT see map, Leaflet, or protected app navigation
    assert 'href="/map"' not in html
    assert 'href="/reports/new"' not in html
    assert 'href="/routes"' not in html
    assert 'href="/analytics"' not in html
    assert 'href="/admin/escalations"' not in html
    assert "leaflet" not in lowered

    # API / non-HTML callers still receive the JSON welcome response cleanly
    api_resp = client.get("/", headers={"Accept": "application/json"})
    assert api_resp.status_code == 200
    assert api_resp.json()["status"] == "active"


# ==============================================================================
# 2. Login Page (GET /login) & Signup Page (GET /signup)
# ==============================================================================


def test_login_page_renders_accessible_form_csrf_cookie_and_confirm_email_notice():
    client = TestClient(app)
    resp = client.get("/login?confirm_email=1", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]

    html = resp.text
    csrf_cookie = resp.cookies.get("sr_csrf_token")
    assert csrf_cookie

    assert 'id="sr-login-form"' in html
    assert 'action="/auth/login"' in html
    assert f'name="csrf_token" value="{csrf_cookie}"' in html
    assert 'name="email"' in html and 'type="email"' in html
    assert 'name="password"' in html
    assert 'required' in html
    assert 'href="/signup"' in html
    assert "Confirm your email first" in html
    assert "/static/js/auth_forms.js" in html


def test_signup_page_renders_accessible_form_matching_existing_backend_contract():
    client = TestClient(app)
    resp = client.get("/signup", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]

    html = resp.text
    csrf_cookie = resp.cookies.get("sr_csrf_token")
    assert csrf_cookie

    assert 'id="sr-signup-form"' in html
    assert 'action="/auth/signup"' in html
    assert f'name="csrf_token" value="{csrf_cookie}"' in html
    # Matches Stage 5 SignupPayload: full_name (optional), email (required), password (min 8)
    assert 'name="full_name"' in html
    assert 'name="email"' in html and 'type="email"' in html
    assert 'name="password"' in html and 'minlength="8"' in html
    # Client-side confirm password field present (without inventing a backend form field name)
    assert 'id="signup-confirm-password"' in html
    assert 'name="confirm_password"' not in html
    assert 'href="/login"' in html
    assert "/static/js/auth_forms.js" in html


def test_authenticated_user_visiting_login_or_signup_redirects_to_map():
    app.dependency_overrides[get_optional_user] = lambda: AuthenticatedUser(
        id="11111111-1111-4111-8111-111111111111",
        email="citizen@example.com",
        full_name="Asha Rao",
        role="user",
    )
    client = TestClient(app)

    r_login = client.get("/login", follow_redirects=False)
    assert r_login.status_code == 303
    assert r_login.headers["location"] == "/map"

    r_signup = client.get("/signup", follow_redirects=False)
    assert r_signup.status_code == 303
    assert r_signup.headers["location"] == "/map"


# ==============================================================================
# 3. End-to-End Compatibility With Existing Stage 5 Auth, CSRF & Cookie Flow
# ==============================================================================


def test_login_page_csrf_works_with_existing_auth_login_endpoint_and_blocks_missing_csrf():
    client = TestClient(app)

    # 1. Load GET /login -> receives double-submit CSRF cookie
    page_resp = client.get("/login", headers={"Accept": "text/html"})
    assert page_resp.status_code == 200
    csrf_token = page_resp.cookies.get("sr_csrf_token")
    assert csrf_token

    # 2. Missing X-CSRF-Token on cookie-bearing browser request is rejected (403)
    r_no_csrf = client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "Password123!"},
        headers={"Origin": "http://testserver"},
    )
    assert r_no_csrf.status_code == 403

    # 3. Invalid input returns clear 422 validation detail
    r_bad_email = client.post(
        "/auth/login",
        json={"email": "not-an-email", "password": "Password123!"},
        headers={"X-CSRF-Token": csrf_token, "Origin": "http://testserver"},
    )
    assert r_bad_email.status_code == 422
    assert "valid email" in r_bad_email.json()["detail"].lower()

    # 4. Invalid credentials return 401 with safe error message
    with patch(
        "app.routers.auth.login_with_email",
        side_effect=InvalidCredentialsError(),
    ):
        r_invalid = client.post(
            "/auth/login",
            json={"email": "user@example.com", "password": "WrongPassword"},
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://testserver"},
        )
    assert r_invalid.status_code == 401
    assert "invalid email or password" in r_invalid.json()["detail"].lower()

    # 5. Valid credentials set HttpOnly cookies and never expose tokens in JSON
    mock_session = AuthSession(
        user=AuthenticatedUser(
            id="11111111-1111-4111-8111-111111111111",
            email="user@example.com",
            full_name="Asha Rao",
            role="user",
        ),
        access_token="secret-jwt-access",
        refresh_token="secret-jwt-refresh",
        expires_in=3600,
    )
    with patch("app.routers.auth.login_with_email", return_value=mock_session):
        r_ok = client.post(
            "/auth/login",
            json={"email": "user@example.com", "password": "Password123!"},
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://testserver"},
        )
    assert r_ok.status_code == 200
    assert "secret-jwt-access" not in r_ok.text
    assert "secret-jwt-refresh" not in r_ok.text
    set_cookies = r_ok.headers.get_list("set-cookie")
    assert any("sr_access_token=" in c and "httponly" in c.lower() for c in set_cookies)
    assert any("sr_refresh_token=" in c and "httponly" in c.lower() for c in set_cookies)


def test_signup_page_csrf_works_with_existing_auth_signup_endpoint():
    client = TestClient(app)

    page_resp = client.get("/signup", headers={"Accept": "text/html"})
    assert page_resp.status_code == 200
    csrf_token = page_resp.cookies.get("sr_csrf_token")
    assert csrf_token

    # Short password rejected by backend validation (422)
    r_short = client.post(
        "/auth/signup",
        json={"full_name": "Asha", "email": "asha@example.com", "password": "short"},
        headers={"X-CSRF-Token": csrf_token, "Origin": "http://testserver"},
    )
    assert r_short.status_code == 422

    # Valid signup requiring email confirmation returns 201 without setting auth cookies
    mock_confirm = AuthSession(
        user=AuthenticatedUser(
            id="22222222-2222-4222-8222-222222222222",
            email="asha@example.com",
            full_name="Asha Rao",
            role="user",
        ),
        email_confirmation_required=True,
    )
    with patch("app.routers.auth.signup_with_email", return_value=mock_confirm):
        r_ok = client.post(
            "/auth/signup",
            json={
                "full_name": "Asha Rao",
                "email": "asha@example.com",
                "password": "StrongPassword123!",
            },
            headers={"X-CSRF-Token": csrf_token, "Origin": "http://testserver"},
        )
    assert r_ok.status_code == 201
    assert r_ok.json()["email_confirmation_required"] is True
    assert "sr_access_token" not in r_ok.cookies


# ==============================================================================
# 4. Security, Token Hygiene & Design Compliance Checks
# ==============================================================================


def test_b2_static_js_and_templates_follow_security_and_design_rules():
    client = TestClient(app)
    r_js = client.get("/static/js/auth_forms.js")
    assert r_js.status_code == 200

    js_text = AUTH_JS_PATH.read_text(encoding="utf-8")
    # Never store or touch auth tokens or browser storage in JS
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "sr_access_token",
        "sr_refresh_token",
        "SUPABASE_SERVICE_KEY",
    ):
        assert forbidden not in js_text

    # Light theme only & strict mint contrast rule across all B2 templates
    for name in ("index.html", "login.html", "signup.html"):
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert "dark:" not in text, f"Unexpected dark: class in {name}"
        assert "text-accent" not in text, f"Forbidden mint text on light surface in {name}"

    # Logged-out users still cannot access protected endpoints
    assert client.get("/api/reports?bounds=17.48,78.41,17.49,78.42").status_code == 401
    r_me_browser = client.get(
        "/auth/me",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r_me_browser.status_code == 303
    assert r_me_browser.headers["location"] == "/login"
