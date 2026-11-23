"""SafeRoute authentication and profile-role authorization service.

Pure business logic module (no FastAPI Request/Response coupling):
- Integrates with Supabase Auth (email/password only; Google OAuth disabled).
- Validates access tokens directly against Supabase Auth (never trusts
  browser-supplied user IDs or roles).
- Loads user roles exclusively from trusted public.profiles database records
  using per-request RLS-enforced Supabase clients.
- Never prints or exposes tokens, passwords, or service-role credentials.
"""

import logging
import re
import time
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

from app.config import Settings, settings
from app.services.supabase_client import (
    SupabaseConfigError,
    create_authenticated_supabase_client,
    get_supabase_client,
)

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_ROLES = ("user", "admin")


# ------------------------------------------------------------------------------
# Domain Exceptions (Redacted, Safe User-Facing Messages)
# ------------------------------------------------------------------------------


class AuthError(Exception):
    """Base authentication/authorization exception with safe client message."""

    def __init__(self, user_message: str, status_code: int = 401) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code


class InvalidCredentialsError(AuthError):
    """Raised when login credentials or tokens are invalid."""

    def __init__(self, user_message: str = "Invalid email or password.") -> None:
        super().__init__(user_message=user_message, status_code=401)


class SessionExpiredError(AuthError):
    """Raised when an authentication session or access token has expired."""

    def __init__(self, user_message: str = "Your session has expired. Please log in again.") -> None:
        super().__init__(user_message=user_message, status_code=401)


class SignupConflictError(AuthError):
    """Raised when attempting to register an email that already exists."""

    def __init__(
        self,
        user_message: str = "Unable to complete registration with the provided details.",
    ) -> None:
        super().__init__(user_message=user_message, status_code=400)


class ProfileNotFoundError(AuthError):
    """Raised when an authenticated user lacks a valid public.profiles row."""

    def __init__(
        self,
        user_message: str = "User profile is unavailable. Please contact support.",
    ) -> None:
        super().__init__(user_message=user_message, status_code=403)


class AuthServiceUnavailableError(AuthError):
    """Raised when Supabase Auth or database backend cannot be reached."""

    def __init__(
        self,
        user_message: str = "Authentication service is temporarily unavailable.",
    ) -> None:
        super().__init__(user_message=user_message, status_code=503)


# ------------------------------------------------------------------------------
# Pydantic Schemas
# ------------------------------------------------------------------------------


class AuthenticatedUser(BaseModel):
    """Validated user identity with trusted role loaded from public.profiles."""

    id: str
    email: str = ""
    full_name: str = ""
    role: Literal["user", "admin"] = "user"

    @property
    def is_admin(self) -> bool:
        """Return True iff the user's trusted database role is 'admin'."""
        return self.role == "admin"


class AuthSession(BaseModel):
    """Result of a login, signup, or session refresh operation."""

    user: AuthenticatedUser
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    email_confirmation_required: bool = False


class SignupPayload(BaseModel):
    """Validated email/password registration input."""

    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=120)

    @field_validator("email", mode="before")
    @classmethod
    def _validate_email(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if not EMAIL_REGEX.match(normalized):
            raise ValueError("Please provide a valid email address.")
        return normalized

    @field_validator("full_name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> str:
        return str(value or "").strip()


class LoginPayload(BaseModel):
    """Validated email/password login input."""

    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def _validate_email(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if not EMAIL_REGEX.match(normalized):
            raise ValueError("Please provide a valid email address.")
        return normalized


class UserPublicResponse(BaseModel):
    """Safe public representation of the authenticated user (zero tokens)."""

    id: str
    email: str
    full_name: str
    role: Literal["user", "admin"]


# ------------------------------------------------------------------------------
# Internal Helpers
# ------------------------------------------------------------------------------


def _extract_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Safely read an attribute or dict key from Supabase SDK response objects."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_expired_error(exc: Exception) -> bool:
    """Detect token expiration errors without relying on raw error exposure."""
    msg = str(exc).lower()
    return "expired" in msg or "jwt expired" in msg or "token is expired" in msg


def _is_invalid_credentials_error(exc: Exception) -> bool:
    """Detect invalid credentials / unconfirmed email / invalid token errors."""
    msg = str(exc).lower()
    markers = (
        "invalid login",
        "invalid credentials",
        "email not confirmed",
        "invalid jwt",
        "invalid token",
        "bad jwt",
        "user not found",
        "invalid refresh",
    )
    return any(m in msg for m in markers)


def _fetch_trusted_profile(
    user_id: str,
    email: str,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> AuthenticatedUser:
    """Load the user's trusted role and full_name from public.profiles under RLS.

    Never trusts client-supplied roles and never silently promotes missing
    profiles to admin.
    """
    try:
        authed_client = create_authenticated_supabase_client(access_token, app_settings)
        response = (
            authed_client.table("profiles")
            .select("id, full_name, role")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
    except SupabaseConfigError as exc:
        logger.warning("Supabase configuration error during profile lookup: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc
    except Exception as exc:
        logger.warning("Profile lookup failed: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc

    profile_data = _extract_attr(response, "data")
    if not profile_data or not isinstance(profile_data, dict):
        raise ProfileNotFoundError()

    role = profile_data.get("role")
    if role not in VALID_ROLES:
        logger.warning("Unexpected profile role value encountered for user.")
        raise ProfileNotFoundError()

    return AuthenticatedUser(
        id=str(profile_data.get("id", user_id)),
        email=email,
        full_name=str(profile_data.get("full_name") or ""),
        role=role,
    )


# ------------------------------------------------------------------------------
# Public Authentication Workflows
# ------------------------------------------------------------------------------


def signup_with_email(
    payload: SignupPayload,
    app_settings: Optional[Settings] = None,
) -> AuthSession:
    """Register a new user via Supabase email/password Auth.

    Never sends role metadata (database trigger strictly assigns 'user').
    Handles both immediate sessions and email-confirmation-required flows.
    """
    try:
        client = get_supabase_client(app_settings)
        auth_resp = client.auth.sign_up(
            {
                "email": payload.email,
                "password": payload.password,
                "options": {
                    "data": {
                        "full_name": payload.full_name,
                    }
                },
            }
        )
    except SupabaseConfigError as exc:
        logger.warning("Supabase config error on signup: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc
    except Exception as exc:
        msg = str(exc).lower()
        if "already registered" in msg or "already exists" in msg:
            raise SignupConflictError() from exc
        logger.warning("Supabase signup failed: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc

    user_obj = _extract_attr(auth_resp, "user")
    session_obj = _extract_attr(auth_resp, "session")

    user_id = _extract_attr(user_obj, "id")
    if not user_id:
        raise SignupConflictError()

    email = str(_extract_attr(user_obj, "email") or payload.email)
    access_token = _extract_attr(session_obj, "access_token")
    refresh_token = _extract_attr(session_obj, "refresh_token")
    expires_in = _extract_attr(session_obj, "expires_in")

    if not access_token:
        # Email confirmation is enabled on Supabase (mailer_autoconfirm = false)
        return AuthSession(
            user=AuthenticatedUser(
                id=str(user_id),
                email=email,
                full_name=payload.full_name,
                role="user",
            ),
            access_token=None,
            refresh_token=None,
            expires_in=None,
            email_confirmation_required=True,
        )

    profile_user = _fetch_trusted_profile(
        user_id=str(user_id),
        email=email,
        access_token=str(access_token),
        app_settings=app_settings,
    )
    return AuthSession(
        user=profile_user,
        access_token=str(access_token),
        refresh_token=str(refresh_token) if refresh_token else None,
        expires_in=int(expires_in) if expires_in is not None else None,
        email_confirmation_required=False,
    )


def login_with_email(
    payload: LoginPayload,
    app_settings: Optional[Settings] = None,
) -> AuthSession:
    """Authenticate an existing user with Supabase email/password Auth."""
    try:
        client = get_supabase_client(app_settings)
        auth_resp = client.auth.sign_in_with_password(
            {
                "email": payload.email,
                "password": payload.password,
            }
        )
    except SupabaseConfigError as exc:
        logger.warning("Supabase config error on login: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc
    except Exception as exc:
        if _is_invalid_credentials_error(exc):
            raise InvalidCredentialsError() from exc
        logger.warning("Supabase login failed: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc

    user_obj = _extract_attr(auth_resp, "user")
    session_obj = _extract_attr(auth_resp, "session")
    user_id = _extract_attr(user_obj, "id")
    access_token = _extract_attr(session_obj, "access_token")

    if not user_id or not access_token:
        raise InvalidCredentialsError()

    email = str(_extract_attr(user_obj, "email") or payload.email)
    refresh_token = _extract_attr(session_obj, "refresh_token")
    expires_in = _extract_attr(session_obj, "expires_in")

    profile_user = _fetch_trusted_profile(
        user_id=str(user_id),
        email=email,
        access_token=str(access_token),
        app_settings=app_settings,
    )
    return AuthSession(
        user=profile_user,
        access_token=str(access_token),
        refresh_token=str(refresh_token) if refresh_token else None,
        expires_in=int(expires_in) if expires_in is not None else None,
        email_confirmation_required=False,
    )


_TOKEN_CACHE: dict[str, tuple[AuthenticatedUser, float]] = {}
_CACHE_TTL_SECONDS = 60.0


def clear_token_cache() -> None:
    """Clear the in-memory validated token cache."""
    global _TOKEN_CACHE
    _TOKEN_CACHE.clear()


def get_authenticated_user_from_token(
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> AuthenticatedUser:
    """Validate a Supabase access token and load the trusted profile role."""
    global _TOKEN_CACHE
    token = (access_token or "").strip()
    if not token:
        raise InvalidCredentialsError("Authentication required.")

    now = time.time()
    cached = _TOKEN_CACHE.get(token)
    if cached is not None:
        cached_user, expiry = cached
        if now < expiry:
            return cached_user

    try:
        client = get_supabase_client(app_settings)
        user_resp = client.auth.get_user(token)
    except SupabaseConfigError as exc:
        logger.warning("Supabase config error during token validation: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc
    except Exception as exc:
        if _is_expired_error(exc):
            raise SessionExpiredError() from exc
        if _is_invalid_credentials_error(exc):
            raise InvalidCredentialsError("Invalid authentication token.") from exc
        logger.warning("Token validation failed: %s", type(exc).__name__)
        raise InvalidCredentialsError("Invalid authentication token.") from exc

    user_obj = _extract_attr(user_resp, "user")
    user_id = _extract_attr(user_obj, "id")
    if not user_id:
        raise InvalidCredentialsError("Invalid authentication token.")

    email = str(_extract_attr(user_obj, "email") or "")
    user = _fetch_trusted_profile(
        user_id=str(user_id),
        email=email,
        access_token=token,
        app_settings=app_settings,
    )
    _TOKEN_CACHE[token] = (user, now + _CACHE_TTL_SECONDS)
    if len(_TOKEN_CACHE) > 500:
        _TOKEN_CACHE = {k: v for k, v in _TOKEN_CACHE.items() if v[1] > now}
    return user


def refresh_user_session(
    refresh_token: str,
    app_settings: Optional[Settings] = None,
) -> AuthSession:
    """Refresh an expired session using Supabase Auth refresh_session."""
    token = (refresh_token or "").strip()
    if not token:
        raise SessionExpiredError()

    try:
        client = get_supabase_client(app_settings)
        refresh_resp = client.auth.refresh_session(token)
    except SupabaseConfigError as exc:
        logger.warning("Supabase config error on session refresh: %s", type(exc).__name__)
        raise AuthServiceUnavailableError() from exc
    except Exception as exc:
        logger.info("Session refresh rejected: %s", type(exc).__name__)
        raise SessionExpiredError() from exc

    user_obj = _extract_attr(refresh_resp, "user")
    session_obj = _extract_attr(refresh_resp, "session")
    user_id = _extract_attr(user_obj, "id")
    new_access = _extract_attr(session_obj, "access_token")
    new_refresh = _extract_attr(session_obj, "refresh_token")
    expires_in = _extract_attr(session_obj, "expires_in")

    if not user_id or not new_access:
        raise SessionExpiredError()

    email = str(_extract_attr(user_obj, "email") or "")
    profile_user = _fetch_trusted_profile(
        user_id=str(user_id),
        email=email,
        access_token=str(new_access),
        app_settings=app_settings,
    )
    return AuthSession(
        user=profile_user,
        access_token=str(new_access),
        refresh_token=str(new_refresh) if new_refresh else None,
        expires_in=int(expires_in) if expires_in is not None else None,
        email_confirmation_required=False,
    )


def logout_session(
    access_token: Optional[str],
    app_settings: Optional[Settings] = None,
) -> None:
    """Best-effort stateless revocation of a Supabase Auth session.

    Uses GoTrue's POST /auth/v1/logout with the user's Bearer token so shared
    client instances remain completely stateless. Never raises on already-expired
    tokens so callers can always clear local cookies safely.
    """
    token = (access_token or "").strip()
    if not token:
        return

    _TOKEN_CACHE.pop(token, None)

    cfg = app_settings or settings
    if not cfg.is_supabase_configured:
        return

    try:
        httpx.post(
            f"{cfg.SUPABASE_URL}/auth/v1/logout",
            headers={
                "apikey": cfg.SUPABASE_ANON_KEY.strip(),
                "Authorization": f"Bearer {token}",
            },
            timeout=5.0,
        )
    except Exception as exc:
        # Best-effort remote revocation; local cookie deletion still proceeds.
        logger.info("Remote Supabase logout skipped: %s", type(exc).__name__)
