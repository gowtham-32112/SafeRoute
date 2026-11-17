"""SafeRoute application configuration settings."""

from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Supabase Configuration
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    # Vision AI Configuration
    VISION_API_KEY: str = ""

    # Application Security
    SECRET_KEY: str = "dev-secret-key-change-in-production"

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
    DUPLICATE_RADIUS_METERS: float = 30.0
    ROUTE_HAZARD_BUFFER_METERS: float = 25.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


# Global settings singleton
settings = Settings()
