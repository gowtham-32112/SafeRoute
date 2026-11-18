"""Core Hazard Reports API, duplicate detection, map bounds, and SSE Realtime routes."""

from typing import Any, Dict, Optional, Union

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    get_authenticated_context,
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

router = APIRouter(tags=["reports"])


async def _parse_json_or_form(request: Request) -> Dict[str, Any]:
    """Parse either JSON body or HTML form payload into a dictionary."""
    content_type = request.headers.get("content-type", "").lower()
    if (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        form = await request.form()
        return {k: str(v) for k, v in form.items()}
    try:
        body = await request.json()
    except Exception as exc:
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


@router.post(
    "/reports",
    status_code=status.HTTP_201_CREATED,
    response_model=None,
    summary="Submit a New Hazard Report (or Confirm Existing Duplicate)",
)
async def submit_hazard_report(
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    _csrf: None = Depends(require_csrf),
    category: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    lat: Optional[float] = Form(default=None),
    lng: Optional[float] = Form(default=None),
    address_text: Optional[str] = Form(default=None),
    image: Optional[UploadFile] = File(default=None),
    duplicate_action: Optional[str] = Form(default=None),
    existing_report_id: Optional[str] = Form(default=None),
    app_settings: Settings = Depends(get_settings),
) -> Union[ReportDetailResponse, JSONResponse]:
    """Create a hazard report with EXIF-stripped image upload, or confirm a duplicate.

    Never trusts client-supplied reporter_id, status, severity, counts, AI fields,
    or duplicate_of links. Enforces 5 MB max upload size and JPEG/PNG/WebP formats.
    """
    # Support "Same hazard, add my confirmation" directly from report submission flow
    if (duplicate_action or "").strip().lower() == "same_hazard":
        if not existing_report_id or not existing_report_id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="existing_report_id is required when selecting 'same_hazard'.",
            )
        confirmed = confirm_existing_duplicate(
            report_id=existing_report_id,
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

    if image is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An evidence photograph ('image') is required.",
        )

    # Read up to MAX_IMAGE_SIZE_BYTES + 1 bytes to bound memory and return 413 early
    raw_bytes = await image.read(MAX_IMAGE_SIZE_BYTES + 1)
    if len(raw_bytes) > MAX_IMAGE_SIZE_BYTES:
        raise ImageTooLargeError()

    return create_hazard_report(
        user_id=ctx.user.id,
        access_token=ctx.access_token,
        category=category,
        description=description,
        lat=lat,
        lng=lng,
        image_bytes=raw_bytes,
        image_content_type=image.content_type,
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
    """Query authoritative PostGIS nearby_reports function for potential duplicates."""
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
    """Create a 'confirm' vote on an existing hazard report."""
    raw_data = await _parse_json_or_form(request)
    try:
        payload = ConfirmDuplicateRequest.model_validate(raw_data)
    except ValidationError as exc:
        first_msg = exc.errors()[0].get("msg", "Invalid report_id.")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=first_msg,
        ) from exc

    return confirm_existing_duplicate(
        report_id=payload.report_id,
        user_id=ctx.user.id,
        access_token=ctx.access_token,
        app_settings=app_settings,
    )


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
