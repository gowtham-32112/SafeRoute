"""SafeRoute application configuration settings."""

from typing import Final, Literal, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Authoritative, immutable SafeRoute duplicate business rules (SPEC.md).
# Not deployment-configurable: environment variables and Settings overrides
# cannot change these values.
APPROVED_DUPLICATE_RADIUS_METERS: Final[float] = 30.0
APPROVED_DUPLICATE_WINDOW_DAYS: Final[int] = 30

DEFAULT_DEV_SECRET_KEY = "dev-secret-key-change-in-production"
MIN_PRODUCTION_SECRET_KEY_LENGTH = 32
_WEAK_SECRET_MARKERS = (
    "dev-secret",
    "change-in-production",
    "changeme",
    "placeholder",
    "your-random",
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Supabase Configuration
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    # Vision AI Configuration
    VISION_API_KEY: str = ""

    # Application Security & Environment
    SECRET_KEY: str = DEFAULT_DEV_SECRET_KEY
    ENVIRONMENT: str = "development"

    # Session & Cookie Security
    COOKIE_SECURE: Optional[bool] = None
    COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    ACCESS_COOKIE_NAME: str = "sr_access_token"
    REFRESH_COOKIE_NAME: str = "sr_refresh_token"
    CSRF_COOKIE_NAME: str = "sr_csrf_token"
    ACCESS_TOKEN_MAX_AGE_SECONDS: int = 3600
    REFRESH_TOKEN_MAX_AGE_SECONDS: int = 604800

    # Application Metadata
    APP_NAME: str = "SafeRoute"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # Geospatial & Map Defaults (Kukatpally Y Junction, Hyderabad)
    DEFAULT_MAP_LAT: float = 17.4849
    DEFAULT_MAP_LNG: float = 78.4138
    DEFAULT_MAP_ZOOM: int = 16
    GEOLOCATION_TIMEOUT_SECONDS: int = 5
    MAP_BOUNDS_DEBOUNCE_MS: int = 400
    REPORTS_BOUNDS_LIMIT: int = 500

    # Business Rules
    FIXED_VOTE_THRESHOLD: int = 2
    ESCALATION_CONFIRM_THRESHOLD: int = 5
    ESCALATION_SEVERITY_THRESHOLD: int = 70
    ROUTE_HAZARD_BUFFER_METERS: float = 25.0

    # Request Resource Limits (5 MB max image + 1 MB multipart/form overhead)
    MAX_REQUEST_BODY_BYTES: int = 6 * 1024 * 1024

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("SUPABASE_URL", mode="before")
    @classmethod
    def _normalize_supabase_url(cls, value: Optional[str]) -> str:
        """Strip whitespace and trailing slashes from SUPABASE_URL."""
        if not value:
            return ""
        return str(value).strip().rstrip("/")

    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def _normalize_environment(cls, value: Optional[str]) -> str:
        """Normalize deployment environment name."""
        if not value:
            return "development"
        return str(value).strip().lower()

    @field_validator("MAX_REQUEST_BODY_BYTES")
    @classmethod
    def _validate_max_request_body_bytes(cls, value: int) -> int:
        """Ensure MAX_REQUEST_BODY_BYTES is a positive byte limit."""
        if value < 1024:
            raise ValueError("MAX_REQUEST_BODY_BYTES must be at least 1024 bytes.")
        return value

    @model_validator(mode="after")
    def _validate_production_secret_key(self) -> "Settings":
        """Reject default or weak SECRET_KEY values in staging/production."""
        if not self.is_production:
            return self

        secret = self.SECRET_KEY.strip()
        lowered = secret.lower()
        is_weak = (
            not secret
            or secret == DEFAULT_DEV_SECRET_KEY
            or len(secret) < MIN_PRODUCTION_SECRET_KEY_LENGTH
            or any(marker in lowered for marker in _WEAK_SECRET_MARKERS)
        )
        if is_weak:
            raise ValueError(
                "A strong non-default SECRET_KEY (minimum 32 characters) "
                "is required when ENVIRONMENT is production or staging."
            )
        return self

    @property
    def is_production(self) -> bool:
        """Return True when running in production or staging."""
        return self.ENVIRONMENT in ("production", "staging")

    @property
    def use_secure_cookies(self) -> bool:
        """Use Secure cookies automatically in HTTPS environments unless overridden."""
        if self.COOKIE_SECURE is not None:
            return self.COOKIE_SECURE
        return self.is_production

    @property
    def is_supabase_configured(self) -> bool:
        """Return True if SUPABASE_URL and SUPABASE_ANON_KEY are minimally valid."""
        url = self.SUPABASE_URL
        key = self.SUPABASE_ANON_KEY.strip()
        return (
            bool(url)
            and url.startswith(("https://", "http://"))
            and "placeholder" not in url.lower()
            and bool(key)
        )

    @property
    def has_service_key(self) -> bool:
        """Return True if SUPABASE_SERVICE_KEY is configured."""
        return bool(self.SUPABASE_SERVICE_KEY.strip())

    @property
    def DUPLICATE_RADIUS_METERS(self) -> float:
        """Read-only compatibility accessor for the immutable 30-metre duplicate rule."""
        return APPROVED_DUPLICATE_RADIUS_METERS

    @property
    def DUPLICATE_WINDOW_DAYS(self) -> int:
        """Read-only compatibility accessor for the immutable 30-day duplicate rule."""
        return APPROVED_DUPLICATE_WINDOW_DAYS


# Global settings singleton
settings = Settings()
