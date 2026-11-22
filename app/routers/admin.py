"""SafeRoute Stage 7 Admin Router (Issue #26).

Provides:
- GET /admin/escalations (HTML dashboard for municipal admins; JSON for API clients)
- POST /reports/{report_id}/escalate (authoritative escalation recording to higher authorities)
- POST /admin/escalations/{report_id} (alias endpoint)
- Protected by require_admin_context and require_csrf.
"""

from pathlib import Path
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    get_settings,
    require_admin_context,
    require_csrf,
)
from app.services.admin import (
    AdminServiceError,
    EscalateReportRequest,
    EscalateReportResponse,
    EscalationsDashboardResponse,
    escalate_hazard_report,
    get_admin_escalations_dashboard,
)
from app.services.session import issue_csrf_cookie

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["Admin"])


def _copy_set_cookie_headers(source: Response, target: Response) -> None:
    """Propagate any Set-Cookie headers from carrier response onto template response."""
    for header_name, header_value in source.raw_headers:
        if header_name.lower() == b"set-cookie":
            target.raw_headers.append((header_name, header_value))


def _prepare_page_csrf(request: Request, app_settings: Settings) -> tuple[str, Response]:
    """Reuse an active CSRF double-submit cookie or issue a fresh one for the admin page."""
    existing = request.cookies.get(app_settings.CSRF_COOKIE_NAME, "").strip()
    carrier = Response()
    if existing:
        return existing, carrier
    token = issue_csrf_cookie(carrier, app_settings)
    return token, carrier


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


@router.get(
    "/admin/escalations",
    response_model=None,
    summary="Admin Escalation Candidate Queue and Audit Dashboard",
)
async def admin_escalations_page(
    request: Request,
    response: Response,
    ctx: AuthenticatedContext = Depends(require_admin_context),
    app_settings: Settings = Depends(get_settings),
) -> Union[EscalationsDashboardResponse, Response]:
    """Render the admin escalations dashboard (HTML) or return candidates list (JSON).

    Requires authenticated user with public.profiles.role == 'admin'.
    """
    dashboard = get_admin_escalations_dashboard(
        access_token=ctx.access_token,
        app_settings=app_settings,
    )

    accept = request.headers.get("accept", "").lower()
    wants_html = "text/html" in accept

    if not wants_html:
        return dashboard

    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)

    template_resp = templates.TemplateResponse(
        request=request,
        name="admin_escalations.html",
        context={
            "current_user": ctx.user,
            "active_nav": "escalations",
            "csrf_token": csrf_token,
            "dashboard": dashboard,
        },
    )
    _copy_set_cookie_headers(response, template_resp)
    _copy_set_cookie_headers(csrf_carrier, template_resp)
    return template_resp


@router.post(
    "/reports/{report_id}/escalate",
    response_model=EscalateReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Record Hazard Escalation to Higher Road Maintenance Authority",
)
@router.post(
    "/admin/escalations/{report_id}",
    response_model=EscalateReportResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def record_hazard_escalation(
    report_id: str,
    request: Request,
    ctx: AuthenticatedContext = Depends(require_admin_context),
    _csrf: None = Depends(require_csrf),
    app_settings: Settings = Depends(get_settings),
) -> EscalateReportResponse:
    """Record an official escalation for a hazard report.

    Requires role='admin', valid CSRF token, and valid authority name & note.
    """
    raw_payload = await _parse_json_or_form(request)
    try:
        payload = EscalateReportRequest.model_validate(raw_payload)
    except ValidationError as exc:
        first_msg = exc.errors()[0].get("msg", "Invalid escalation parameters.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=first_msg,
        ) from exc

    return escalate_hazard_report(
        report_id=report_id,
        authority_name=payload.authority_name,
        note=payload.note,
        admin_id=ctx.user.id,
        access_token=ctx.access_token,
        app_settings=app_settings,
    )
