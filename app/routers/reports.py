"""Core Hazard Reports API, duplicate detection, map bounds, and SSE Realtime routes."""

from pathlib import Path
from typing import Any, Dict, Optional, Union

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile as StarletteUploadFile

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    AuthenticatedUser,
    get_authenticated_context,
    get_current_user,
    get_settings,
    require_csrf,
)
from app.services.duplicates import (
    ConfirmDuplicateRequest,
    ConfirmDuplicateResponse,
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    check_nearby_duplicates,
    confirm_existing_duplicate,
)
from app.services.images import MAX_IMAGE_SIZE_BYTES, ImageTooLargeError
from app.services.realtime import stream_hazard_updates
from app.services.reports import (
    BoundsReportsResponse,
    ReportDetailResponse,
    create_hazard_report,
    get_report_by_id,
    get_reports_in_bounds,
)
from app.services.session import issue_csrf_cookie

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _copy_set_cookie_headers(source: Response, target: Response) -> None:
    """Propagate any Set-Cookie headers from a dependency/helper response onto target."""
    for header_name, header_value in source.raw_headers:
        if header_name.lower() == b"set-cookie":
            target.raw_headers.append((header_name, header_value))


def _prepare_page_csrf(request: Request, app_settings: Settings) -> tuple[str, Response]:
    """Reuse an active CSRF double-submit cookie or issue a fresh one."""
    existing = request.cookies.get(app_settings.CSRF_COOKIE_NAME, "").strip()
    carrier = Response()
    if existing:
        return existing, carrier
    token = issue_csrf_cookie(carrier, app_settings)
    return token, carrier

router = APIRouter(tags=["reports"])


async def _parse_json_or_form(request: Request) -> Dict[str, Any]:
    """Parse either JSON body or HTML form payload into a dictionary."""
    content_type = request.headers.get("content-type", "").lower()
    if (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        form = await request.form()
        try:
            return {k: str(v) for k, v in form.items() if k != "csrf_token"}
        finally:
            await form.close()
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON request body.",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object.",
        )
    return body


def _form_str(form: FormData, key: str) -> Optional[str]:
    """Extract an optional string field from FormData."""
    val = form.get(key)
    if val is None or isinstance(val, StarletteUploadFile):
        return None
    return str(val)


def _form_float(form: FormData, key: str) -> Optional[float]:
    """Extract an optional float field from FormData, raising 400 on bad numbers."""
    raw = _form_str(form, key)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Field '{key}' must be a valid numeric value.",
        ) from exc


@router.post(
    "/reports",
    status_code=status.HTTP_201_CREATED,
    response_model=None,
    summary="Submit a New Hazard Report (or Confirm Existing Duplicate)",
)
async def submit_hazard_report(
    request: Request,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    _csrf: None = Depends(require_csrf),
    app_settings: Settings = Depends(get_settings),
) -> Union[ReportDetailResponse, JSONResponse]:
    """Create a hazard report with EXIF-stripped image upload, or confirm a duplicate.

    Request-body size is enforced first by RequestSizeLimitMiddleware (HTTP 413)
    before routing, authentication, or multipart parsing. Because Form/File
    parameters are parsed inside this handler rather than in the FastAPI route
    signature, authentication (get_authenticated_context) executes before
    request.form() for bounded requests.

    Never trusts client-supplied reporter_id, status, severity, counts, AI fields,
    duplicate_of links, or client-computed duplicate distances.
    """
    content_type = request.headers.get("content-type", "").lower()
    if (
        "multipart/form-data" not in content_type
        and "application/x-www-form-urlencoded" not in content_type
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content-Type must be multipart/form-data or form-urlencoded.",
        )

    form = await request.form()
    try:
        category = _form_str(form, "category")
        description = _form_str(form, "description")
        lat = _form_float(form, "lat")
        lng = _form_float(form, "lng")
        address_text = _form_str(form, "address_text")
        duplicate_action = _form_str(form, "duplicate_action")
        existing_report_id = _form_str(form, "existing_report_id")

        # Support "Same hazard, add my confirmation" with full server-side revalidation
        if (duplicate_action or "").strip().lower() == "same_hazard":
            if (
                not existing_report_id
                or not existing_report_id.strip()
                or not category
                or lat is None
                or lng is None
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Fields 'existing_report_id', 'category', 'lat', and 'lng' "
                        "are required when selecting 'same_hazard'."
                    ),
                )
            try:
                confirm_payload = ConfirmDuplicateRequest.model_validate(
                    {
                        "report_id": existing_report_id,
                        "category": category.strip(),
                        "lat": lat,
                        "lng": lng,
                    }
                )
            except ValidationError as exc:
                first_msg = exc.errors()[0].get(
                    "msg", "Invalid duplicate confirmation parameters."
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=first_msg,
                ) from exc

            confirmed = confirm_existing_duplicate(
                report_id=confirm_payload.report_id,
                lat=confirm_payload.lat,
                lng=confirm_payload.lng,
                category=confirm_payload.category,
                user_id=ctx.user.id,
                access_token=ctx.access_token,
                app_settings=app_settings,
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=confirmed.model_dump(),
            )

        if not category or not description or lat is None or lng is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Fields 'category', 'description', 'lat', and 'lng' are required.",
            )

        raw_image = form.get("image")
        if not isinstance(raw_image, StarletteUploadFile):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An evidence photograph ('image') is required.",
            )

        # Read up to MAX_IMAGE_SIZE_BYTES + 1 bytes to enforce exact 5 MB image cap
        raw_bytes = await raw_image.read(MAX_IMAGE_SIZE_BYTES + 1)
        if len(raw_bytes) > MAX_IMAGE_SIZE_BYTES:
            raise ImageTooLargeError()
        image_content_type = raw_image.content_type
    finally:
        await form.close()

    return create_hazard_report(
        user_id=ctx.user.id,
        access_token=ctx.access_token,
        category=category,
        description=description,
        lat=lat,
        lng=lng,
        image_bytes=raw_bytes,
        image_content_type=image_content_type,
        address_text=address_text,
        app_settings=app_settings,
    )


@router.post(
    "/reports/check-duplicate",
    response_model=DuplicateCheckResponse,
    summary="Check Nearby Duplicate Reports (30m, 30 days, Same Category, Non-Fixed)",
)
async def check_duplicate_reports(
    request: Request,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    _csrf: None = Depends(require_csrf),
    app_settings: Settings = Depends(get_settings),
) -> DuplicateCheckResponse:
    """Query authoritative PostGIS nearby_reports function at fixed 30m radius."""
    raw_data = await _parse_json_or_form(request)
    try:
        payload = DuplicateCheckRequest.model_validate(raw_data)
    except ValidationError as exc:
        first_msg = exc.errors()[0].get("msg", "Invalid duplicate-check parameters.")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=first_msg,
        ) from exc

    return check_nearby_duplicates(
        payload=payload,
        access_token=ctx.access_token,
        app_settings=app_settings,
    )


@router.post(
    "/reports/confirm-duplicate",
    response_model=ConfirmDuplicateResponse,
    summary="Confirm an Existing Duplicate Report Instead of Creating a New One",
)
async def confirm_duplicate_report(
    request: Request,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    _csrf: None = Depends(require_csrf),
    app_settings: Settings = Depends(get_settings),
) -> ConfirmDuplicateResponse:
    """Revalidate duplicate criteria via PostGIS and create a 'confirm' vote."""
    raw_data = await _parse_json_or_form(request)
    try:
        payload = ConfirmDuplicateRequest.model_validate(raw_data)
    except ValidationError as exc:
        first_msg = exc.errors()[0].get(
            "msg", "Invalid duplicate confirmation parameters."
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=first_msg,
        ) from exc

    return confirm_existing_duplicate(
        report_id=payload.report_id,
        lat=payload.lat,
        lng=payload.lng,
        category=payload.category,
        user_id=ctx.user.id,
        access_token=ctx.access_token,
        app_settings=app_settings,
    )


@router.get(
    "/reports/new",
    response_model=None,
    summary="Hazard Report Submission Form Page",
)
async def new_report_page(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Render the authenticated hazard report submission form at /reports/new."""
    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)
    map_config = {
        "fallbackLat": app_settings.DEFAULT_MAP_LAT,
        "fallbackLng": app_settings.DEFAULT_MAP_LNG,
        "fallbackLabel": "Kukatpally Y Junction",
        "defaultZoom": app_settings.DEFAULT_MAP_ZOOM,
        "geolocationTimeoutMs": int(app_settings.GEOLOCATION_TIMEOUT_SECONDS * 1000),
    }
    template_resp = templates.TemplateResponse(
        request=request,
        name="report_new.html",
        context={
            "current_user": current_user,
            "active_nav": "report",
            "csrf_token": csrf_token,
            "map_config": map_config,
        },
    )
    _copy_set_cookie_headers(response, template_resp)
    _copy_set_cookie_headers(csrf_carrier, template_resp)
    return template_resp


@router.get(
    "/reports/{report_id}",
    response_model=ReportDetailResponse,
    summary="Retrieve Authorized Hazard Report Details",
)
async def read_hazard_report(
    report_id: str,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
) -> ReportDetailResponse:
    """Return hazard report details and linked images under the user's RLS context."""
    return get_report_by_id(
        report_id=report_id,
        access_token=ctx.access_token,
        app_settings=app_settings,
    )


@router.get(
    "/api/reports",
    response_model=BoundsReportsResponse,
    summary="List Hazard Reports Within Map Bounding Box (Max 500)",
)
async def list_reports_in_bounds(
    bounds: str = Query(
        ...,
        description="Bounding box in format 'min_lat,min_lng,max_lat,max_lng'",
    ),
    category: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
) -> BoundsReportsResponse:
    """Return up to 500 hazard reports within visible map bounds via reports_in_bounds."""
    return get_reports_in_bounds(
        bounds=bounds,
        access_token=ctx.access_token,
        category=category,
        status_filter=status_filter,
        app_settings=app_settings,
    )


@router.get(
    "/api/reports/stream",
    summary="Authenticated Server-Sent Events Stream for Live Hazard Updates (Option R1)",
)
async def stream_reports_realtime(
    request: Request,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Bridge authenticated Supabase Realtime events for public.hazard_reports via SSE.

    Preserves HttpOnly auth cookies: the user's JWT stays strictly server-side.
    """
    return StreamingResponse(
        stream_hazard_updates(
            access_token=ctx.access_token,
            is_disconnected=request.is_disconnected,
            app_settings=app_settings,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
