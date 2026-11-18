"""Secure HTTP-only cookie and CSRF session management helpers."""

import hmac
import secrets
from typing import Optional

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
