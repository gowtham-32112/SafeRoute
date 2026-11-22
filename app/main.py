"""SafeRoute FastAPI application entrypoint."""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import AsyncIterator, Dict, Optional, Union

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings, settings
from app.deps import (
    AuthenticationRedirectRequired,
    get_current_user,
    get_optional_user,
    get_settings,
)
from app.routers.auth import router as auth_router
from app.routers.reports import router as reports_router
from app.services.auth import AuthenticatedUser, AuthError
from app.services.community import CommunityServiceError
from app.services.duplicates import DuplicateServiceError
from app.services.images import ImageValidationError
from app.services.reports import ReportServiceError
from app.services.session import clear_session_cookies, issue_csrf_cookie
from app.services.supabase_client import SupabaseConfigError, clear_supabase_client_cache

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


class RequestBodyTooLargeError(Exception):
    """Raised when an incoming HTTP request exceeds MAX_REQUEST_BODY_BYTES."""

    def __init__(
        self,
        user_message: str = "Request body exceeds the maximum allowed size.",
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class RequestSizeLimitMiddleware:
    """Pure ASGI middleware enforcing a strict request body byte limit.

    Protects against unbounded request buffering and oversized multipart uploads
    before routing, authentication, CSRF parsing, or multipart parsing run.
    Handles both Content-Length headers and chunked/streaming bodies.
    """

    def __init__(self, app: ASGIApp, fastapi_app: FastAPI) -> None:
        self.app = app
        self.fastapi_app = fastapi_app

    def _get_max_bytes(self) -> int:
        provider = self.fastapi_app.dependency_overrides.get(get_settings, get_settings)
        return int(provider().MAX_REQUEST_BODY_BYTES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self._get_max_bytes()

        # 1. Fast-path Content-Length check before touching receive() or multipart parsing
        for raw_name, raw_value in scope.get("headers", ()):
            if raw_name.lower() == b"content-length":
                text_value = raw_value.decode("latin-1").strip()
                if not text_value:
                    continue
                try:
                    declared_len = int(text_value)
                except ValueError:
                    resp = JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content={"detail": "Invalid Content-Length header."},
                    )
                    await resp(scope, receive, send)
                    return
                if declared_len < 0:
                    resp = JSONResponse(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        content={"detail": "Invalid Content-Length header."},
                    )
                    await resp(scope, receive, send)
                    return
                if declared_len > max_bytes:
                    resp = JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={"detail": "Request body exceeds the maximum allowed size."},
                    )
                    await resp(scope, receive, send)
                    return
                break

        # 2. Streaming / chunked body byte counter (blocks missing or spoofed Content-Length)
        bytes_received = 0
        limit_exceeded = False
        response_started = False
        suppress_swallowed_response = False

        async def bounded_receive() -> Message:
            nonlocal bytes_received, limit_exceeded
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                bytes_received += len(body)
                if bytes_received > max_bytes:
                    limit_exceeded = True
                    raise RequestBodyTooLargeError()
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started, suppress_swallowed_response
            if message.get("type") == "http.response.start":
                if limit_exceeded and message.get("status") != status.HTTP_413_CONTENT_TOO_LARGE:
                    suppress_swallowed_response = True
                    return
                response_started = True
            elif suppress_swallowed_response:
                return
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except RequestBodyTooLargeError as exc:
            if not response_started:
                resp = JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"detail": exc.user_message},
                )
                await resp(scope, receive, send)
            return

        if limit_exceeded and not response_started:
            resp = JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={"detail": "Request body exceeds the maximum allowed size."},
            )
            await resp(scope, receive, send)


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
app.add_middleware(RequestSizeLimitMiddleware, fastapi_app=app)

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


@app.exception_handler(RequestBodyTooLargeError)
async def handle_request_body_too_large(
    _request: Request,
    exc: RequestBodyTooLargeError,
) -> JSONResponse:
    """Return HTTP 413 when a streamed/chunked request body exceeds the configured cap."""
    return JSONResponse(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
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


@app.exception_handler(CommunityServiceError)
async def handle_community_service_error(
    _request: Request,
    exc: CommunityServiceError,
) -> JSONResponse:
    """Return clean JSON errors for community verification and comments actions."""
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


def _copy_set_cookie_headers(source: Response, target: Response) -> None:
    """Propagate any Set-Cookie headers from a dependency/helper response onto target."""
    for header_name, header_value in source.raw_headers:
        if header_name.lower() == b"set-cookie":
            target.raw_headers.append((header_name, header_value))


def _prepare_page_csrf(request: Request, app_settings: Settings) -> tuple[str, Response]:
    """Reuse an active CSRF double-submit cookie or issue a fresh one for auth pages."""
    existing = request.cookies.get(app_settings.CSRF_COOKIE_NAME, "").strip()
    carrier = Response()
    if existing:
        return existing, carrier
    token = issue_csrf_cookie(carrier, app_settings)
    return token, carrier


@app.get("/", response_model=None, summary="Public Landing Page / Root Welcome Endpoint")
async def root(
    request: Request,
    response: Response,
    current_user: Optional[AuthenticatedUser] = Depends(get_optional_user),
    app_settings: Settings = Depends(get_settings),
) -> Union[Response, Dict[str, str]]:
    """Render the public HTML landing page for browsers; preserve JSON for API clients."""
    accept = request.headers.get("accept", "").lower()
    if "text/html" in accept:
        template_resp = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"current_user": current_user},
        )
        _copy_set_cookie_headers(response, template_resp)
        return template_resp

    return {
        "message": (
            f"Welcome to {app_settings.APP_NAME} - "
            "AI-powered Community Road-Hazard Awareness Platform"
        ),
        "status": "active",
        "version": app_settings.APP_VERSION,
        "docs": "/docs",
    }


@app.get("/login", response_model=None, summary="Public Login Page")
async def login_page(
    request: Request,
    response: Response,
    current_user: Optional[AuthenticatedUser] = Depends(get_optional_user),
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Render the SafeRoute login page or redirect authenticated users to /map."""
    if current_user is not None:
        redirect_resp = RedirectResponse(url="/map", status_code=status.HTTP_303_SEE_OTHER)
        _copy_set_cookie_headers(response, redirect_resp)
        return redirect_resp

    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)
    confirm_email = request.query_params.get("confirm_email") == "1"
    toasts = (
        [
            {
                "type": "info",
                "title": "Confirm your email",
                "message": "Please verify your email address before logging in.",
            }
        ]
        if confirm_email
        else []
    )
    template_resp = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "current_user": None,
            "csrf_token": csrf_token,
            "confirm_email": confirm_email,
            "toasts": toasts,
        },
    )
    _copy_set_cookie_headers(response, template_resp)
    _copy_set_cookie_headers(csrf_carrier, template_resp)
    return template_resp


@app.get("/signup", response_model=None, summary="Public Signup Page")
async def signup_page(
    request: Request,
    response: Response,
    current_user: Optional[AuthenticatedUser] = Depends(get_optional_user),
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Render the SafeRoute signup page or redirect authenticated users to /map."""
    if current_user is not None:
        redirect_resp = RedirectResponse(url="/map", status_code=status.HTTP_303_SEE_OTHER)
        _copy_set_cookie_headers(response, redirect_resp)
        return redirect_resp

    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)
    template_resp = templates.TemplateResponse(
        request=request,
        name="signup.html",
        context={
            "current_user": None,
            "csrf_token": csrf_token,
        },
    )
    _copy_set_cookie_headers(response, template_resp)
    _copy_set_cookie_headers(csrf_carrier, template_resp)
    return template_resp


@app.get("/map", response_model=None, summary="Authenticated Live Hazard Map Page")
async def map_page(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Render the authenticated SafeRoute map page with approved fallback config."""
    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)
    map_config = {
        "fallbackLat": app_settings.DEFAULT_MAP_LAT,
        "fallbackLng": app_settings.DEFAULT_MAP_LNG,
        "fallbackLabel": "Kukatpally Y Junction",
        "defaultZoom": app_settings.DEFAULT_MAP_ZOOM,
        "geolocationTimeoutMs": int(app_settings.GEOLOCATION_TIMEOUT_SECONDS * 1000),
        "boundsDebounceMs": app_settings.MAP_BOUNDS_DEBOUNCE_MS,
    }
    template_resp = templates.TemplateResponse(
        request=request,
        name="map.html",
        context={
            "current_user": current_user,
            "active_nav": "map",
            "csrf_token": csrf_token,
            "map_config": map_config,
        },
    )
    _copy_set_cookie_headers(response, template_resp)
    _copy_set_cookie_headers(csrf_carrier, template_resp)
    return template_resp


@app.get("/health", summary="Health Check")
async def health_check() -> Dict[str, str]:
    """Application health check endpoint returning system status."""
    return {
        "status": "healthy",
        "application": "SafeRoute",
    }
