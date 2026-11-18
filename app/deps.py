"""FastAPI dependency injection utilities for configuration, auth, and RBAC."""

from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, status
from supabase import Client

from app.config import Settings, settings
from app.services.auth import (
    AuthenticatedUser,
    AuthError,
    ProfileNotFoundError,
    SessionExpiredError,
    get_authenticated_user_from_token,
    refresh_user_session,
)
from app.services.session import (
    clear_session_cookies,
    extract_bearer_token,
    extract_session_tokens,
    set_session_cookies,
    verify_csrf_double_submit,
)
from app.services.supabase_client import get_supabase_client


class AuthenticationRedirectRequired(Exception):
    """Signals that an unauthenticated browser request must redirect to /login."""

    def __init__(self, login_url: str = "/login", clear_cookies: bool = False) -> None:
        super().__init__(login_url)
        self.login_url = login_url
        self.clear_cookies = clear_cookies


def get_settings() -> Settings:
    """Dependency provider for application settings."""
    return settings


def get_supabase(app_settings: Settings = Depends(get_settings)) -> Client:
    """Dependency provider for the stateless public/anon Supabase client.

    Never exposes the service-role client to request handlers.
    """
    return get_supabase_client(app_settings)


def _wants_html_redirect(request: Request) -> bool:
    """Return True when the caller is a browser expecting an HTML redirect."""
    path = request.url.path
    if path.startswith("/api/"):
        return False
    if extract_bearer_token(request) is not None:
        return False

    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return False
    return "text/html" in accept or request.headers.get("hx-request", "").lower() == "true"


def _resolve_user_from_request(
    request: Request,
    response: Response,
    app_settings: Settings,
) -> tuple[Optional[AuthenticatedUser], bool]:
    """Validate request credentials and return (user_or_none, had_stale_cookie).

    Transparently refreshes an expired cookie session when a valid refresh
    cookie is available and rotates cookies on the response.
    """
    access_token, refresh_token, via_cookie = extract_session_tokens(request, app_settings)

    if not access_token and not refresh_token:
        return None, False

    if access_token:
        try:
            user = get_authenticated_user_from_token(access_token, app_settings)
            return user, False
        except ProfileNotFoundError:
            raise
        except SessionExpiredError:
            if not (via_cookie and refresh_token):
                return None, via_cookie
        except AuthError:
            return None, via_cookie

    # Attempt cookie-based refresh when access token is missing or expired
    if via_cookie and refresh_token:
        try:
            refreshed = refresh_user_session(refresh_token, app_settings)
            if refreshed.access_token:
                set_session_cookies(
                    response=response,
                    access_token=refreshed.access_token,
                    refresh_token=refreshed.refresh_token or refresh_token,
                    app_settings=app_settings,
                    expires_in=refreshed.expires_in,
                )
            return refreshed.user, False
        except ProfileNotFoundError:
            raise
        except AuthError:
            return None, True

    return None, via_cookie


async def get_optional_user(
    request: Request,
    response: Response,
    app_settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """Return the authenticated user if valid credentials exist, else None."""
    try:
        user, stale_cookie = _resolve_user_from_request(request, response, app_settings)
    except ProfileNotFoundError:
        return None

    if user is None and stale_cookie:
        clear_session_cookies(response, app_settings)
    return user


async def get_current_user(
    request: Request,
    response: Response,
    app_settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """Require a valid Supabase session and return the trusted AuthenticatedUser.

    - Browser / HTMX requests without valid authentication redirect to /login.
    - API / JSON / Bearer-token requests receive HTTP 401 Unauthorized.
    - Authenticated users missing a valid public.profiles record receive HTTP 403.
    """
    user, stale_cookie = _resolve_user_from_request(request, response, app_settings)

    if user is not None:
        return user

    if stale_cookie:
        clear_session_cookies(response, app_settings)

    if _wants_html_redirect(request):
        raise AuthenticationRedirectRequired(login_url="/login", clear_cookies=stale_cookie)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require an authenticated user whose trusted public.profiles.role is 'admin'.

    Rejects authenticated non-admin users with HTTP 403 Forbidden.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required.",
        )
    return current_user


async def require_csrf(
    request: Request,
    app_settings: Settings = Depends(get_settings),
) -> None:
    """Enforce double-submit CSRF protection on cookie-authenticated state changes.

    Pure Bearer-header API calls without auth cookies are exempt from CSRF.
    """
    if request.method.upper() in ("GET", "HEAD", "OPTIONS"):
        return

    _, _, via_cookie = extract_session_tokens(request, app_settings)
    if not via_cookie:
        return

    submitted_token: Optional[str] = None
    content_type = request.headers.get("content-type", "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        raw_val = form.get("csrf_token")
        if isinstance(raw_val, str):
            submitted_token = raw_val

    if not verify_csrf_double_submit(request, submitted_token, app_settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed.",
        )
