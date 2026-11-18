"""SafeRoute FastAPI application entrypoint."""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import AsyncIterator, Dict

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, settings
from app.deps import AuthenticationRedirectRequired, get_settings
from app.routers.auth import router as auth_router
from app.routers.reports import router as reports_router
from app.services.auth import AuthError
from app.services.duplicates import DuplicateServiceError
from app.services.images import ImageValidationError
from app.services.reports import ReportServiceError
from app.services.session import clear_session_cookies
from app.services.supabase_client import SupabaseConfigError, clear_supabase_client_cache

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage FastAPI startup and shutdown lifecycle cleanly."""
    yield
    clear_supabase_client_cache()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered Community Road-Hazard Awareness Platform",
    lifespan=lifespan,
)

# Mount static files directory
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Configure Jinja2 templates
templates_dir = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


# ------------------------------------------------------------------------------
# Centralized Exception Handlers (Consistent, Redacted Error Responses)
# ------------------------------------------------------------------------------


@app.exception_handler(AuthenticationRedirectRequired)
async def handle_auth_redirect(
    request: Request,
    exc: AuthenticationRedirectRequired,
) -> RedirectResponse:
    """Redirect unauthenticated browser/HTMX requests to /login."""
    headers = {}
    if request.headers.get("hx-request", "").lower() == "true":
        headers["HX-Redirect"] = exc.login_url

    resp = RedirectResponse(
        url=exc.login_url,
        status_code=status.HTTP_303_SEE_OTHER,
        headers=headers,
    )
    if exc.clear_cookies:
        clear_session_cookies(resp, settings)
    return resp


@app.exception_handler(AuthError)
async def handle_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
    """Return safe, redacted JSON errors for authentication/authorization failures."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.user_message},
    )


@app.exception_handler(ImageValidationError)
async def handle_image_validation_error(
    _request: Request,
    exc: ImageValidationError,
) -> JSONResponse:
    """Return clean JSON errors for image size/type/integrity violations."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.user_message},
    )


@app.exception_handler(DuplicateServiceError)
async def handle_duplicate_service_error(
    _request: Request,
    exc: DuplicateServiceError,
) -> JSONResponse:
    """Return clean JSON errors for duplicate-check and confirm actions."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.user_message},
    )


@app.exception_handler(ReportServiceError)
async def handle_report_service_error(
    _request: Request,
    exc: ReportServiceError,
) -> JSONResponse:
    """Return clean JSON errors for report creation, retrieval, and bounds queries."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.user_message},
    )


@app.exception_handler(SupabaseConfigError)
async def handle_supabase_config_error(
    _request: Request,
    exc: SupabaseConfigError,
) -> JSONResponse:
    """Handle missing/invalid Supabase config without exposing internal details."""
    logger.error("Supabase configuration error: %s", type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Authentication service is temporarily unavailable."},
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    _request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Ensure consistent JSON structure for 401, 403, 404, and other HTTP errors."""
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
    headers = dict(exc.headers) if exc.headers else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail},
        headers=headers,
    )


@app.exception_handler(Exception)
async def handle_unexpected_exception(_request: Request, exc: Exception) -> JSONResponse:
    """Catch unhandled exceptions and return a redacted 500 error (zero stack trace)."""
    logger.error("Unhandled application error: %s", type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# ------------------------------------------------------------------------------
# Routers & Core Public Endpoints
# ------------------------------------------------------------------------------

app.include_router(auth_router)
app.include_router(reports_router)


@app.get("/", summary="Root Welcome Endpoint")
async def root(app_settings: Settings = Depends(get_settings)) -> Dict[str, str]:
    """Simple SafeRoute welcome response."""
    return {
        "message": (
            f"Welcome to {app_settings.APP_NAME} - "
            "AI-powered Community Road-Hazard Awareness Platform"
        ),
        "status": "active",
        "version": app_settings.APP_VERSION,
        "docs": "/docs",
    }


@app.get("/health", summary="Health Check")
async def health_check() -> Dict[str, str]:
    """Application health check endpoint returning system status."""
    return {
        "status": "healthy",
        "application": "SafeRoute",
    }
