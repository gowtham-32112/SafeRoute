"""Reusable, stateless Supabase client initialization layer.

Security guarantees:
- Normal user-facing authentication and RLS-enforced database operations use
  only SUPABASE_URL and SUPABASE_ANON_KEY (publishable/anon key).
- Clients disable local session persistence (persist_session=False) so
  concurrent FastAPI requests never share mutable auth session state.
- Authenticated database queries bind the caller's validated JWT in a
  per-request client so PostgreSQL Row-Level Security (RLS) enforces identity.
- Service-role access is strictly isolated in a private helper unavailable to
  ordinary request handlers and never exposed to client code.
"""

from functools import lru_cache
import hashlib
from typing import Optional

from supabase import Client, ClientOptions, create_client

from app.config import Settings, settings


class SupabaseConfigError(RuntimeError):
    """Raised when required Supabase configuration is missing or invalid."""


def _validate_public_config(app_settings: Settings) -> tuple[str, str]:
    """Validate and return (SUPABASE_URL, SUPABASE_ANON_KEY) without logging secrets."""
    url = app_settings.SUPABASE_URL.strip().rstrip("/")
    anon_key = app_settings.SUPABASE_ANON_KEY.strip()

    if not url or not url.startswith(("https://", "http://")) or "placeholder" in url.lower():
        raise SupabaseConfigError("SUPABASE_URL is missing or invalid.")

    if not anon_key:
        raise SupabaseConfigError("SUPABASE_ANON_KEY is missing.")

    return url, anon_key


def _fingerprint(secret: str) -> str:
    """Return a short non-reversible hash for safe cache keying."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=4)
def _cached_public_client(url: str, key_fingerprint: str, anon_key: str) -> Client:
    """Create and cache a stateless public/anon Supabase client."""
    _ = key_fingerprint
    options = ClientOptions(
        auto_refresh_token=False,
        persist_session=False,
    )
    return create_client(url, anon_key, options=options)


def get_supabase_client(app_settings: Optional[Settings] = None) -> Client:
    """Return a reusable stateless Supabase client using the publishable/anon key.

    Suitable for Supabase Auth operations (sign_up, sign_in_with_password,
    get_user, refresh_session). Never holds mutable per-user session state.
    """
    cfg = app_settings or settings
    url, anon_key = _validate_public_config(cfg)
    return _cached_public_client(url, _fingerprint(anon_key), anon_key)


def create_authenticated_supabase_client(
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> Client:
    """Create a per-request Supabase client authenticated with a user's JWT.

    Uses SUPABASE_ANON_KEY + Authorization: Bearer <access_token> so all
    PostgreSQL and Storage operations execute strictly under the user's RLS
    context without mutating shared client state across concurrent requests.
    """
    token = (access_token or "").strip()
    if not token:
        raise SupabaseConfigError("A non-empty access token is required.")

    cfg = app_settings or settings
    url, anon_key = _validate_public_config(cfg)
    options = ClientOptions(
        auto_refresh_token=False,
        persist_session=False,
        headers={"Authorization": f"Bearer {token}"},
    )
    return create_client(url, anon_key, options=options)


def _get_isolated_service_client(app_settings: Optional[Settings] = None) -> Client:
    """Internal-only service-role Supabase client for privileged backend tasks.

    MUST NOT be injected into ordinary FastAPI request handlers or exposed to
    templates/frontend code.
    """
    cfg = app_settings or settings
    url = cfg.SUPABASE_URL.strip().rstrip("/")
    service_key = cfg.SUPABASE_SERVICE_KEY.strip()

    if not url or not url.startswith(("https://", "http://")) or "placeholder" in url.lower():
        raise SupabaseConfigError("SUPABASE_URL is missing or invalid.")

    if not service_key:
        raise SupabaseConfigError("SUPABASE_SERVICE_KEY is not configured.")

    options = ClientOptions(
        auto_refresh_token=False,
        persist_session=False,
    )
    return create_client(url, service_key, options=options)


def clear_supabase_client_cache() -> None:
    """Clear cached Supabase clients (used on app shutdown and in tests)."""
    _cached_public_client.cache_clear()
