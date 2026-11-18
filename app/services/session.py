"""Secure HTTP-only cookie and CSRF session management helpers."""

import hmac
import secrets
from typing import Optional
from urllib.parse import urlparse

from fastapi import Request, Response

from app.config import Settings, settings


CSRF_HEADER_NAME = "X-CSRF-Token"


def generate_csrf_token() -> str:
    """Generate a cryptographically secure URL-safe CSRF token."""
    return secrets.token_urlsafe(32)


def set_session_cookies(
    response: Response,
    access_token: str,
    refresh_token: Optional[str],
    app_settings: Optional[Settings] = None,
    expires_in: Optional[int] = None,
) -> str:
    """Attach HTTP-only auth session cookies and a CSRF token cookie to a response.

    Returns the generated CSRF token value (non-secret double-submit value);
    access and refresh tokens are strictly confined to HttpOnly cookies.
    """
    cfg = app_settings or settings
    access_max_age = (
        int(expires_in)
        if expires_in is not None and int(expires_in) > 0
        else cfg.ACCESS_TOKEN_MAX_AGE_SECONDS
    )

    response.set_cookie(
        key=cfg.ACCESS_COOKIE_NAME,
        value=access_token,
        max_age=access_max_age,
        httponly=True,
        secure=cfg.use_secure_cookies,
        samesite=cfg.COOKIE_SAMESITE,
        path="/",
    )

    if refresh_token:
        response.set_cookie(
            key=cfg.REFRESH_COOKIE_NAME,
            value=refresh_token,
            max_age=cfg.REFRESH_TOKEN_MAX_AGE_SECONDS,
            httponly=True,
            secure=cfg.use_secure_cookies,
            samesite=cfg.COOKIE_SAMESITE,
            path="/",
        )

    csrf_token = generate_csrf_token()
    response.set_cookie(
        key=cfg.CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=cfg.REFRESH_TOKEN_MAX_AGE_SECONDS,
        httponly=False,
        secure=cfg.use_secure_cookies,
        samesite=cfg.COOKIE_SAMESITE,
        path="/",
    )
    return csrf_token


def issue_csrf_cookie(
    response: Response,
    app_settings: Optional[Settings] = None,
) -> str:
    """Issue a CSRF double-submit cookie without setting any auth cookies."""
    cfg = app_settings or settings
    csrf_token = generate_csrf_token()
    response.set_cookie(
        key=cfg.CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=cfg.REFRESH_TOKEN_MAX_AGE_SECONDS,
        httponly=False,
        secure=cfg.use_secure_cookies,
        samesite=cfg.COOKIE_SAMESITE,
        path="/",
    )
    return csrf_token


def clear_session_cookies(
    response: Response,
    app_settings: Optional[Settings] = None,
) -> None:
    """Invalidate all SafeRoute authentication and CSRF cookies on the client."""
    cfg = app_settings or settings
    for cookie_name in (
        cfg.ACCESS_COOKIE_NAME,
        cfg.REFRESH_COOKIE_NAME,
        cfg.CSRF_COOKIE_NAME,
    ):
        response.delete_cookie(
            key=cookie_name,
            path="/",
            secure=cfg.use_secure_cookies,
            samesite=cfg.COOKIE_SAMESITE,
        )


def extract_bearer_token(request: Request) -> Optional[str]:
    """Extract a Bearer access token from the Authorization header if present."""
    auth_header = request.headers.get("Authorization", "").strip()
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def extract_session_tokens(
    request: Request,
    app_settings: Optional[Settings] = None,
) -> tuple[Optional[str], Optional[str], bool]:
    """Extract (access_token, refresh_token, via_cookie) from a request.

    Prefers explicit Authorization Bearer header over cookie so stateless API
    clients do not collide with browser cookies.
    """
    cfg = app_settings or settings
    bearer = extract_bearer_token(request)
    if bearer:
        return bearer, None, False

    access_cookie = request.cookies.get(cfg.ACCESS_COOKIE_NAME)
    refresh_cookie = request.cookies.get(cfg.REFRESH_COOKIE_NAME)
    via_cookie = bool(access_cookie or refresh_cookie)
    return (
        access_cookie.strip() if access_cookie else None,
        refresh_cookie.strip() if refresh_cookie else None,
        via_cookie,
    )


def verify_csrf_double_submit(
    request: Request,
    submitted_token: Optional[str] = None,
    app_settings: Optional[Settings] = None,
) -> bool:
    """Validate double-submit CSRF token in constant time.

    Returns True when valid; False when missing or mismatched.
    """
    cfg = app_settings or settings
    cookie_token = request.cookies.get(cfg.CSRF_COOKIE_NAME, "").strip()
    candidate = (
        submitted_token.strip()
        if submitted_token
        else request.headers.get(CSRF_HEADER_NAME, "").strip()
    )
    if not cookie_token or not candidate:
        return False
    return hmac.compare_digest(cookie_token, candidate)


def _normalize_origin(raw_value: str) -> Optional[tuple[str, str, int]]:
    """Parse an Origin/Referer URL into normalized (scheme, host, port)."""
    cleaned = raw_value.strip()
    if not cleaned or cleaned.lower() == "null":
        return None
    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, host, port


def verify_same_origin(request: Request) -> bool:
    """Validate Origin or Referer header against the request's target origin.

    - Rejects explicit 'null' origins or cross-origin Origin/Referer headers.
    - Returns True when both Origin and Referer headers are absent so callers
      rely on the double-submit CSRF token rather than breaking privacy-stripped
      or non-browser clients.
    """
    origin_header = request.headers.get("origin")
    referer_header = request.headers.get("referer")
    source_header = origin_header if origin_header is not None else referer_header
    if source_header is None:
        return True

    source_origin = _normalize_origin(source_header)
    if source_origin is None:
        return False

    host_header = request.headers.get("host", "").strip()
    target_url = (
        f"{request.url.scheme}://{host_header}"
        if host_header
        else f"{request.url.scheme}://{request.url.netloc}"
    )
    target_origin = _normalize_origin(target_url)
    if target_origin is None:
        return False

    return source_origin == target_origin


def is_browser_auth_csrf_subject(
    request: Request,
    app_settings: Optional[Settings] = None,
) -> bool:
    """Return True when a pre-auth request (login/signup) must pass CSRF token checks.

    Applies to:
    - HTML form submissions (application/x-www-form-urlencoded, multipart/form-data)
    - HTMX browser requests (HX-Request: true)
    - Requests carrying browser Origin/Referer headers
    - Requests carrying any SafeRoute CSRF or auth cookies

    Pure non-browser JSON API requests without cookies or browser origin headers
    are excluded from cookie-based double-submit CSRF requirements.
    """
    cfg = app_settings or settings
    content_type = request.headers.get("content-type", "").lower()
    if (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        return True

    if request.headers.get("hx-request", "").lower() == "true":
        return True

    if request.headers.get("origin") is not None or request.headers.get("referer") is not None:
        return True

    return any(
        name in request.cookies
        for name in (
            cfg.CSRF_COOKIE_NAME,
            cfg.ACCESS_COOKIE_NAME,
            cfg.REFRESH_COOKIE_NAME,
        )
    )
