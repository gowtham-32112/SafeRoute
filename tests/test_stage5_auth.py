"""Unit and integration tests for SafeRoute Stage 5 FastAPI Backend Foundation.

All Supabase Auth and PostgREST profile calls are mocked — tests never touch
the live production Supabase project or modify real user accounts.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import get_settings
from app.main import app
from app.services.auth import (
    AuthenticatedUser,
    AuthSession,
    InvalidCredentialsError,
    ProfileNotFoundError,
    SessionExpiredError,
)
from app.services.supabase_client import (
    SupabaseConfigError,
    _get_isolated_service_client,
    clear_supabase_client_cache,
    create_authenticated_supabase_client,
    get_supabase_client,
)


def _test_settings(**overrides) -> Settings:
    """Build deterministic isolated Settings for unit tests (never reads real .env)."""
    defaults = {
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_ANON_KEY": "sb_publishable_test_dummy_key",
        "SUPABASE_SERVICE_KEY": "",
        "SECRET_KEY": "unit-test-secret-key",
        "ENVIRONMENT": "development",
        "COOKIE_SECURE": None,
        "COOKIE_SAMESITE": "lax",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture(autouse=True)
def reset_overrides_and_cache():
    """Ensure FastAPI dependency overrides and client cache are isolated per test."""
    clear_supabase_client_cache()
    app.dependency_overrides[get_settings] = lambda: _test_settings()
    yield
    app.dependency_overrides.clear()
    clear_supabase_client_cache()


def _make_mock_profile_client(profile_data):
    """Build a mocked Supabase client returning profile_data for profiles query."""
    client = MagicMock()
    exec_result = SimpleNamespace(data=profile_data)
    (
        client.table.return_value.select.return_value.eq.return_value
        .maybe_single.return_value.execute.return_value
    ) = exec_result
    return client


# ==============================================================================
# 1. Configuration & Supabase Client Tests
# ==============================================================================


def test_settings_cookie_security_defaults_by_environment():
    strong_secret = "s" * 48
    dev = _test_settings(ENVIRONMENT="development", COOKIE_SECURE=None)
    prod = _test_settings(
        ENVIRONMENT="production",
        COOKIE_SECURE=None,
        SECRET_KEY=strong_secret,
    )
    staging = _test_settings(
        ENVIRONMENT="staging",
        COOKIE_SECURE=None,
        SECRET_KEY=strong_secret,
    )
    forced_true = _test_settings(ENVIRONMENT="development", COOKIE_SECURE=True)

    assert dev.use_secure_cookies is False
    assert prod.use_secure_cookies is True
    assert staging.use_secure_cookies is True
    assert forced_true.use_secure_cookies is True


def test_production_and_staging_reject_default_or_weak_secret_key():
    from pydantic import ValidationError
    from app.config import DEFAULT_DEV_SECRET_KEY

    # Development allows default SECRET_KEY
    dev_cfg = _test_settings(ENVIRONMENT="development", SECRET_KEY=DEFAULT_DEV_SECRET_KEY)
    assert dev_cfg.SECRET_KEY == DEFAULT_DEV_SECRET_KEY

    # Production rejects default development SECRET_KEY without leaking it in error
    with pytest.raises(ValidationError) as exc_prod:
        _test_settings(ENVIRONMENT="production", SECRET_KEY=DEFAULT_DEV_SECRET_KEY)
    assert DEFAULT_DEV_SECRET_KEY not in str(exc_prod.value)

    # Staging rejects default development SECRET_KEY
    with pytest.raises(ValidationError):
        _test_settings(ENVIRONMENT="staging", SECRET_KEY=DEFAULT_DEV_SECRET_KEY)

    # Production rejects short/weak secret keys
    with pytest.raises(ValidationError) as exc_short:
        _test_settings(ENVIRONMENT="production", SECRET_KEY="too-short-key")
    assert "too-short-key" not in str(exc_short.value)

    # Production and staging accept a strong >= 32-char non-default secret
    strong = "a9F4kL2mN8pQ1rS6tU3vW7xY0zB5cD8eF2gH9jK4"
    prod_ok = _test_settings(ENVIRONMENT="production", SECRET_KEY=strong)
    staging_ok = _test_settings(ENVIRONMENT="staging", SECRET_KEY=strong)
    assert prod_ok.is_production is True
    assert staging_ok.is_production is True


def test_supabase_client_rejects_missing_or_placeholder_env():
    with pytest.raises(SupabaseConfigError):
        get_supabase_client(_test_settings(SUPABASE_URL=""))

    with pytest.raises(SupabaseConfigError):
        get_supabase_client(_test_settings(SUPABASE_URL="https://placeholder.supabase.co"))

    with pytest.raises(SupabaseConfigError):
        get_supabase_client(_test_settings(SUPABASE_ANON_KEY=""))

    with pytest.raises(SupabaseConfigError):
        _get_isolated_service_client(_test_settings(SUPABASE_SERVICE_KEY=""))


def test_supabase_client_reuses_cached_public_client_and_isolates_auth_client():
    cfg = _test_settings()
    with patch("app.services.supabase_client.create_client") as mock_create:
        mock_create.side_effect = [MagicMock(name="base"), MagicMock(name="authed")]

        c1 = get_supabase_client(cfg)
        c2 = get_supabase_client(cfg)
        assert c1 is c2
        assert mock_create.call_count == 1
        _, kwargs = mock_create.call_args
        assert kwargs["options"].persist_session is False
        assert kwargs["options"].auto_refresh_token is False

        c_authed = create_authenticated_supabase_client("user-jwt-123", cfg)
        assert c_authed is not c1
        assert mock_create.call_count == 2
        _, authed_kwargs = mock_create.call_args
        assert authed_kwargs["options"].headers["Authorization"] == "Bearer user-jwt-123"


def test_missing_supabase_config_returns_redacted_503_on_login():
    app.dependency_overrides[get_settings] = lambda: _test_settings(
        SUPABASE_URL="", SUPABASE_ANON_KEY=""
    )
    client = TestClient(app)
    resp = client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "password123"},
    )
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Authentication service is temporarily unavailable."}


# ==============================================================================
# 2. Signup & Login Workflow Tests
# ==============================================================================


def test_signup_immediate_session_sets_httponly_cookies_and_hides_tokens():
    auth_client = MagicMock()
    auth_client.auth.sign_up.return_value = SimpleNamespace(
        user=SimpleNamespace(id="user-1", email="alice@example.com"),
        session=SimpleNamespace(
            access_token="secret-access-jwt",
            refresh_token="secret-refresh-jwt",
            expires_in=3600,
        ),
    )
    profile_client = _make_mock_profile_client(
        {"id": "user-1", "full_name": "Alice", "role": "user"}
    )

    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch("app.services.auth.create_authenticated_supabase_client", return_value=profile_client),
    ):
        client = TestClient(app)
        resp = client.post(
            "/auth/signup",
            json={
                "email": "Alice@Example.com",
                "password": "StrongPassword123",
                "full_name": "Alice",
            },
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["email_confirmation_required"] is False
    assert body["user"] == {
        "id": "user-1",
        "email": "alice@example.com",
        "full_name": "Alice",
        "role": "user",
    }
    # Never expose raw tokens in response body
    assert "secret-access-jwt" not in resp.text
    assert "secret-refresh-jwt" not in resp.text

    # Verify cookies set with HttpOnly on auth tokens
    set_cookies = resp.headers.get_list("set-cookie")
    access_header = next(h for h in set_cookies if h.startswith("sr_access_token="))
    refresh_header = next(h for h in set_cookies if h.startswith("sr_refresh_token="))
    assert "httponly" in access_header.lower()
    assert "samesite=lax" in access_header.lower()
    assert "httponly" in refresh_header.lower()

    # Verify signup never passes 'role' in user metadata
    signup_args = auth_client.auth.sign_up.call_args[0][0]
    assert "role" not in signup_args["options"]["data"]


def test_signup_email_confirmation_required_returns_201_without_auth_cookies():
    auth_client = MagicMock()
    auth_client.auth.sign_up.return_value = SimpleNamespace(
        user=SimpleNamespace(id="user-2", email="bob@example.com"),
        session=None,
    )

    with patch("app.services.auth.get_supabase_client", return_value=auth_client):
        client = TestClient(app)
        resp = client.post(
            "/auth/signup",
            json={
                "email": "bob@example.com",
                "password": "StrongPassword123",
                "full_name": "Bob",
            },
        )

    assert resp.status_code == 201
    assert resp.json()["email_confirmation_required"] is True
    assert "sr_access_token" not in resp.cookies


def test_signup_validation_rejects_bad_email_and_short_password():
    client = TestClient(app)
    r_bad_email = client.post(
        "/auth/signup",
        json={"email": "not-an-email", "password": "StrongPassword123"},
    )
    assert r_bad_email.status_code == 422

    r_short_pw = client.post(
        "/auth/signup",
        json={"email": "valid@example.com", "password": "short"},
    )
    assert r_short_pw.status_code == 422


def test_login_success_sets_cookies_and_browser_form_redirects_to_map():
    auth_client = MagicMock()
    auth_client.auth.sign_in_with_password.return_value = SimpleNamespace(
        user=SimpleNamespace(id="user-1", email="alice@example.com"),
        session=SimpleNamespace(
            access_token="login-access-jwt",
            refresh_token="login-refresh-jwt",
            expires_in=3600,
        ),
    )
    profile_client = _make_mock_profile_client(
        {"id": "user-1", "full_name": "Alice", "role": "user"}
    )

    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch("app.services.auth.create_authenticated_supabase_client", return_value=profile_client),
    ):
        client = TestClient(app)
        # JSON login
        resp_json = client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "StrongPassword123"},
        )
        assert resp_json.status_code == 200
        assert resp_json.json()["user"]["role"] == "user"
        assert "login-access-jwt" not in resp_json.text

        # Browser HTML form login with valid pre-auth CSRF cookie redirects to /map
        browser_client = TestClient(app)
        csrf_resp = browser_client.get("/auth/csrf")
        csrf_token = csrf_resp.json()["csrf_token"]

        resp_form = browser_client.post(
            "/auth/login",
            data={
                "email": "alice@example.com",
                "password": "StrongPassword123",
                "csrf_token": csrf_token,
            },
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert resp_form.status_code == 303
        assert resp_form.headers["location"] == "/map"


def test_login_invalid_credentials_returns_401():
    auth_client = MagicMock()
    auth_client.auth.sign_in_with_password.side_effect = RuntimeError("Invalid login credentials")

    with patch("app.services.auth.get_supabase_client", return_value=auth_client):
        client = TestClient(app)
        resp = client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "wrong-password"},
        )

    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid email or password."}


# ==============================================================================
# 3. Authentication Dependencies, Expiration, Refresh & RBAC Tests
# ==============================================================================


def test_unauthenticated_access_returns_401_for_api_and_redirects_for_browser():
    client = TestClient(app)

    # API / JSON caller -> 401
    r_api = client.get("/auth/me", headers={"Accept": "application/json"})
    assert r_api.status_code == 401
    assert r_api.json() == {"detail": "Authentication required."}

    # Spoofed headers must never grant access
    r_spoof = client.get(
        "/auth/me",
        headers={
            "Accept": "application/json",
            "X-User-Id": "admin-1",
            "X-User-Role": "admin",
        },
    )
    assert r_spoof.status_code == 401

    # Browser HTML caller -> 303 redirect to /login (A7 acceptance criterion)
    r_html = client.get("/auth/me", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r_html.status_code == 303
    assert r_html.headers["location"] == "/login"

    # HTMX caller -> 303 with HX-Redirect header
    r_htmx = client.get("/auth/me", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r_htmx.status_code == 303
    assert r_htmx.headers["hx-redirect"] == "/login"


def test_authenticated_ordinary_user_access_and_admin_rejection():
    auth_client = MagicMock()
    auth_client.auth.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(id="user-1", email="alice@example.com")
    )
    profile_client = _make_mock_profile_client(
        {"id": "user-1", "full_name": "Alice", "role": "user"}
    )

    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch("app.services.auth.create_authenticated_supabase_client", return_value=profile_client),
    ):
        client = TestClient(app)
        client.cookies.set("sr_access_token", "valid-user-jwt")

        r_me = client.get("/auth/me", headers={"Accept": "application/json"})
        assert r_me.status_code == 200
        assert r_me.json() == {
            "id": "user-1",
            "email": "alice@example.com",
            "full_name": "Alice",
            "role": "user",
        }

        # Ordinary user rejected from admin route with 403 Forbidden
        r_admin = client.get("/auth/admin-check", headers={"Accept": "application/json"})
        assert r_admin.status_code == 403
        assert r_admin.json() == {"detail": "Administrator privileges required."}


def test_admin_user_authorized_on_admin_route():
    auth_client = MagicMock()
    auth_client.auth.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(id="admin-1", email="admin@saferoute.demo")
    )
    profile_client = _make_mock_profile_client(
        {"id": "admin-1", "full_name": "Demo Admin", "role": "admin"}
    )

    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch("app.services.auth.create_authenticated_supabase_client", return_value=profile_client),
    ):
        client = TestClient(app)
        resp = client.get(
            "/auth/admin-check",
            headers={"Authorization": "Bearer valid-admin-jwt"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "authorized",
        "role": "admin",
        "user_id": "admin-1",
    }


def test_invalid_and_expired_tokens_and_cookie_refresh_behavior():
    client = TestClient(app)

    # 1. Invalid Bearer token -> 401
    with patch(
        "app.deps.get_authenticated_user_from_token",
        side_effect=InvalidCredentialsError("Invalid authentication token."),
    ):
        r_invalid = client.get(
            "/auth/me",
            headers={"Authorization": "Bearer tampered-token"},
        )
        assert r_invalid.status_code == 401

    # 2. Expired access cookie + valid refresh cookie -> refreshes & rotates cookies
    refreshed_session = AuthSession(
        user=AuthenticatedUser(
            id="user-1",
            email="alice@example.com",
            full_name="Alice",
            role="user",
        ),
        access_token="fresh-access-jwt",
        refresh_token="fresh-refresh-jwt",
        expires_in=3600,
    )
    with (
        patch("app.deps.get_authenticated_user_from_token", side_effect=SessionExpiredError()),
        patch("app.deps.refresh_user_session", return_value=refreshed_session),
    ):
        client.cookies.set("sr_access_token", "expired-jwt")
        client.cookies.set("sr_refresh_token", "valid-refresh-jwt")
        r_refreshed = client.get("/auth/me", headers={"Accept": "application/json"})
        assert r_refreshed.status_code == 200
        assert r_refreshed.json()["id"] == "user-1"
        set_cookies = r_refreshed.headers.get_list("set-cookie")
        assert any(h.startswith("sr_access_token=fresh-access-jwt") for h in set_cookies)

    # 3. Expired access cookie + expired refresh cookie -> 401 and clears cookies
    with (
        patch("app.deps.get_authenticated_user_from_token", side_effect=SessionExpiredError()),
        patch("app.deps.refresh_user_session", side_effect=SessionExpiredError()),
    ):
        client.cookies.set("sr_access_token", "expired-jwt")
        client.cookies.set("sr_refresh_token", "expired-refresh-jwt")
        r_dead = client.get("/auth/me", headers={"Accept": "application/json"})
        assert r_dead.status_code == 401


def test_missing_or_invalid_profile_record_returns_403_safely():
    auth_client = MagicMock()
    auth_client.auth.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(id="orphan-1", email="orphan@example.com")
    )

    # Missing profile row (data=None)
    missing_profile_client = _make_mock_profile_client(None)
    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch(
            "app.services.auth.create_authenticated_supabase_client",
            return_value=missing_profile_client,
        ),
    ):
        client = TestClient(app)
        resp = client.get(
            "/auth/me",
            headers={"Authorization": "Bearer valid-orphan-jwt"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "User profile is unavailable. Please contact support."}

    # Unexpected role value -> rejected safely
    bad_role_client = _make_mock_profile_client(
        {"id": "orphan-1", "full_name": "Bad Role", "role": "superadmin"}
    )
    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch(
            "app.services.auth.create_authenticated_supabase_client",
            return_value=bad_role_client,
        ),
    ):
        client = TestClient(app)
        resp_bad = client.get(
            "/auth/admin-check",
            headers={"Authorization": "Bearer valid-orphan-jwt"},
        )
        assert resp_bad.status_code == 403


# ==============================================================================
# 4. Logout, CSRF Protection & Error Handling Tests
# ==============================================================================


def test_logout_enforces_csrf_for_cookies_and_clears_session():
    client = TestClient(app)
    client.cookies.set("sr_access_token", "active-jwt")
    client.cookies.set("sr_refresh_token", "active-refresh")
    client.cookies.set("sr_csrf_token", "valid-csrf-token")

    # Missing CSRF header -> 403
    r_no_csrf = client.post("/auth/logout")
    assert r_no_csrf.status_code == 403
    assert r_no_csrf.json() == {"detail": "CSRF validation failed."}

    # Wrong CSRF header -> 403
    r_bad_csrf = client.post("/auth/logout", headers={"X-CSRF-Token": "wrong-token"})
    assert r_bad_csrf.status_code == 403

    # Matching CSRF header -> 200, revokes remote session & clears cookies
    with patch("app.routers.auth.logout_session") as mock_logout:
        r_ok = client.post("/auth/logout", headers={"X-CSRF-Token": "valid-csrf-token"})
        assert r_ok.status_code == 200
        assert r_ok.json() == {"message": "Logged out successfully."}
        mock_logout.assert_called_once()
        cleared = " ".join(r_ok.headers.get_list("set-cookie")).lower()
        assert "sr_access_token=" in cleared
        assert "sr_refresh_token=" in cleared
        assert "sr_csrf_token=" in cleared
        assert "max-age=0" in cleared


def test_logout_form_submission_with_csrf_redirects_to_login():
    client = TestClient(app)
    client.cookies.set("sr_access_token", "active-jwt")
    client.cookies.set("sr_csrf_token", "form-csrf-123")

    with patch("app.routers.auth.logout_session"):
        resp = client.post(
            "/auth/logout",
            data={"csrf_token": "form-csrf-123"},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_consistent_404_and_redacted_500_error_responses():
    client = TestClient(app, raise_server_exceptions=False)

    # Consistent 404
    r_404 = client.get("/nonexistent-route-stage5")
    assert r_404.status_code == 404
    assert r_404.json() == {"detail": "Not Found"}

    # Redacted 500 (never exposes internal message or stack trace)
    with patch(
        "app.routers.auth.login_with_email",
        side_effect=RuntimeError("SENSITIVE_DB_CONNECTION_STRING_LEAK"),
    ):
        r_500 = client.post(
            "/auth/login",
            json={"email": "alice@example.com", "password": "password123"},
        )
    assert r_500.status_code == 500
    assert r_500.json() == {"detail": "An internal server error occurred."}
    assert "SENSITIVE_DB_CONNECTION_STRING_LEAK" not in r_500.text


# ==============================================================================
# 5. Pre-Auth Login/Signup CSRF, Origin/Referer & Bearer API Tests
# ==============================================================================


def _mock_auth_success():
    """Return mocked auth_client and profile_client for login/signup CSRF tests."""
    auth_client = MagicMock()
    session_obj = SimpleNamespace(
        access_token="jwt-csrf-test-access",
        refresh_token="jwt-csrf-test-refresh",
        expires_in=3600,
    )
    user_obj = SimpleNamespace(id="user-csrf-1", email="csrf@example.com")
    auth_client.auth.sign_in_with_password.return_value = SimpleNamespace(
        user=user_obj, session=session_obj
    )
    auth_client.auth.sign_up.return_value = SimpleNamespace(
        user=user_obj, session=session_obj
    )
    profile_client = _make_mock_profile_client(
        {"id": "user-csrf-1", "full_name": "CSRF User", "role": "user"}
    )
    return auth_client, profile_client


def test_login_form_csrf_missing_invalid_and_valid():
    auth_client, profile_client = _mock_auth_success()

    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch("app.services.auth.create_authenticated_supabase_client", return_value=profile_client),
    ):
        client = TestClient(app)

        # 1. Login form WITHOUT CSRF token (unauthenticated session) -> 403
        r_missing = client.post(
            "/auth/login",
            data={"email": "csrf@example.com", "password": "StrongPassword123"},
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r_missing.status_code == 403
        assert r_missing.json() == {"detail": "CSRF validation failed."}

        # Obtain pre-auth CSRF cookie (still unauthenticated — no sr_access_token!)
        csrf_token = client.get("/auth/csrf").json()["csrf_token"]
        assert "sr_access_token" not in client.cookies

        # 2. Login form with INVALID CSRF token -> 403
        r_invalid = client.post(
            "/auth/login",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "csrf_token": "tampered-token",
            },
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r_invalid.status_code == 403
        assert r_invalid.json() == {"detail": "CSRF validation failed."}

        # 3. Login form with VALID CSRF token -> 303 -> /map and rotates CSRF cookie
        r_valid = client.post(
            "/auth/login",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "csrf_token": csrf_token,
            },
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r_valid.status_code == 303
        assert r_valid.headers["location"] == "/map"
        # Post-login CSRF cookie must be rotated to prevent fixation
        assert client.cookies.get("sr_csrf_token") != csrf_token


def test_signup_form_csrf_missing_invalid_and_valid():
    auth_client, profile_client = _mock_auth_success()

    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch("app.services.auth.create_authenticated_supabase_client", return_value=profile_client),
    ):
        client = TestClient(app)

        # 1. Signup form WITHOUT CSRF token -> 403
        r_missing = client.post(
            "/auth/signup",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "full_name": "CSRF User",
            },
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r_missing.status_code == 403
        assert r_missing.json() == {"detail": "CSRF validation failed."}

        # Obtain pre-auth CSRF cookie (unauthenticated)
        csrf_token = client.get("/auth/csrf").json()["csrf_token"]

        # 2. Signup form with INVALID CSRF token -> 403
        r_invalid = client.post(
            "/auth/signup",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "full_name": "CSRF User",
                "csrf_token": "wrong-csrf-token",
            },
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r_invalid.status_code == 403

        # 3. Signup form with VALID CSRF token -> 303 -> /map
        r_valid = client.post(
            "/auth/signup",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "full_name": "CSRF User",
                "csrf_token": csrf_token,
            },
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r_valid.status_code == 303
        assert r_valid.headers["location"] == "/map"


def test_same_origin_and_cross_origin_browser_requests():
    auth_client, profile_client = _mock_auth_success()

    with (
        patch("app.services.auth.get_supabase_client", return_value=auth_client),
        patch("app.services.auth.create_authenticated_supabase_client", return_value=profile_client),
    ):
        client = TestClient(app, base_url="http://testserver")
        csrf_token = client.get("/auth/csrf").json()["csrf_token"]

        # Same-origin browser HTMX/fetch request with valid Origin + CSRF -> allowed
        r_same_origin = client.post(
            "/auth/login",
            json={"email": "csrf@example.com", "password": "StrongPassword123"},
            headers={
                "Origin": "http://testserver",
                "HX-Request": "true",
                "X-CSRF-Token": csrf_token,
            },
        )
        assert r_same_origin.status_code == 200

        # Refresh CSRF token after login rotation
        csrf_after = client.cookies.get("sr_csrf_token")

        # Same-origin Referer on logout with valid CSRF -> allowed
        with patch("app.routers.auth.logout_session"):
            r_same_ref = client.post(
                "/auth/logout",
                headers={
                    "Referer": "http://testserver/map",
                    "X-CSRF-Token": csrf_after,
                },
            )
            assert r_same_ref.status_code == 200

        # Cross-origin Origin header rejected even if valid CSRF token is supplied
        csrf_new = client.get("/auth/csrf").json()["csrf_token"]
        r_cross_origin = client.post(
            "/auth/login",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "csrf_token": csrf_new,
            },
            headers={"Origin": "https://evil.example"},
        )
        assert r_cross_origin.status_code == 403
        assert r_cross_origin.json() == {"detail": "Cross-origin request rejected."}

        # Cross-origin Referer header rejected
        r_cross_ref = client.post(
            "/auth/signup",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "csrf_token": csrf_new,
            },
            headers={"Referer": "https://attacker.example/form"},
        )
        assert r_cross_ref.status_code == 403
        assert r_cross_ref.json() == {"detail": "Cross-origin request rejected."}

        # Explicit "null" Origin (sandboxed iframe CSRF) rejected
        r_null_origin = client.post(
            "/auth/login",
            data={
                "email": "csrf@example.com",
                "password": "StrongPassword123",
                "csrf_token": csrf_new,
            },
            headers={"Origin": "null"},
        )
        assert r_null_origin.status_code == 403
        assert r_null_origin.json() == {"detail": "Cross-origin request rejected."}


def test_bearer_api_logout_without_csrf_and_csrf_token_cannot_authenticate():
    client = TestClient(app)

    # 1. Pure Bearer-token API logout works without cookie-based CSRF
    with patch("app.routers.auth.logout_session") as mock_logout:
        r_bearer_logout = client.post(
            "/auth/logout",
            headers={"Authorization": "Bearer api-client-jwt"},
        )
        assert r_bearer_logout.status_code == 200
        mock_logout.assert_called_once()

    # 2. A CSRF token cookie alone must NEVER authenticate a user
    csrf_token = client.get("/auth/csrf").json()["csrf_token"]
    r_csrf_only = client.get("/auth/me", headers={"Accept": "application/json"})
    assert r_csrf_only.status_code == 401

    # Passing CSRF token in X-CSRF-Token or Bearer header still fails auth
    with patch(
        "app.deps.get_authenticated_user_from_token",
        side_effect=InvalidCredentialsError("Invalid authentication token."),
    ):
        r_csrf_as_bearer = client.get(
            "/auth/me",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {csrf_token}",
            },
        )
        assert r_csrf_as_bearer.status_code == 401


def test_token_cache_and_logout_eviction():
    from app.services.auth import (
        AuthenticatedUser,
        _TOKEN_CACHE,
        clear_token_cache,
        get_authenticated_user_from_token,
        logout_session,
    )

    clear_token_cache()
    assert len(_TOKEN_CACHE) == 0

    dummy_user = AuthenticatedUser(
        id="user-123",
        email="test@example.com",
        role="user",
        full_name="Tester",
        is_admin=False,
    )

    with patch("app.services.auth.get_supabase_client") as mock_get_client, patch(
        "app.services.auth._fetch_trusted_profile", return_value=dummy_user
    ) as mock_fetch:
        mock_client = MagicMock()
        mock_user = MagicMock()
        mock_user.id = "user-123"
        mock_user.email = "test@example.com"
        mock_user_resp = MagicMock(user=mock_user)
        mock_client.auth.get_user.return_value = mock_user_resp
        mock_get_client.return_value = mock_client

        # First lookup hits the remote client and populates cache
        u1 = get_authenticated_user_from_token("jwt-cache-test-token")
        assert u1.id == "user-123"
        assert mock_fetch.call_count == 1
        assert "jwt-cache-test-token" in _TOKEN_CACHE

        # Second lookup returns cached object immediately without remote calls
        u2 = get_authenticated_user_from_token("jwt-cache-test-token")
        assert u2 is u1
        assert mock_fetch.call_count == 1

        # Logout evicts from cache
        logout_session("jwt-cache-test-token")
        assert "jwt-cache-test-token" not in _TOKEN_CACHE
