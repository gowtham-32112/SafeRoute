"""FastAPI dependency injection utilities."""

from typing import Generator
from app.config import Settings, settings


def get_settings() -> Settings:
    """Dependency provider for application settings."""
    return settings


# ---------------------------------------------------------------------------
# Placeholder dependencies for subsequent development stages:
# - Stage 2: Authentication & Admin guards (Issue A7)
# - Stage 2: Supabase client injection (Issue A1)
# ---------------------------------------------------------------------------

async def get_current_user():
    """Placeholder dependency for authenticated user retrieval.

    Will be implemented in Issue A7 (Member A) using Supabase Auth
    and session cookies.
    """
    raise NotImplementedError("Authentication dependency will be implemented in Stage 2 (Issue A7).")


async def require_admin():
    """Placeholder dependency for admin-only endpoint authorization.

    Will be implemented in Issue A7 (Member A).
    """
    raise NotImplementedError("Admin authorization dependency will be implemented in Stage 2 (Issue A7).")
