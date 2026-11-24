"""Authenticated live issue page and its commuter/admin actions."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse, Response as StarletteResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    get_authenticated_context,
    get_settings,
    require_admin_context,
    require_csrf,
)
from app.services.broadcast import get_active_broadcast
from app.services.community import CommunityServiceError, cast_hazard_vote
from app.services.live_issues import (
    LIVE_ISSUES_PAGE_SIZE,
    LiveIssueAdminUpdate,
    LiveIssueServiceError,
    delete_live_issue,
    list_live_issues,
    update_live_issue,
)
from app.services.session import issue_csrf_cookie

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(tags=["Live Issues"])


def _copy_set_cookie_headers(source: Response, target: Response) -> None:
    """Propagate cookies issued by auth and CSRF dependencies to a template response."""
    for header_name, header_value in source.raw_headers:
        if header_name.lower() == b"set-cookie":
            target.raw_headers.append((header_name, header_value))


def _prepare_page_csrf(request: Request, app_settings: Settings) -> tuple[str, Response]:
    """Reuse an active CSRF cookie or issue one for the live issues page."""
    existing = request.cookies.get(app_settings.CSRF_COOKIE_NAME, "").strip()
    carrier = Response()
    if existing:
        return existing, carrier
    token = issue_csrf_cookie(carrier, app_settings)
    return token, carrier


def _page_redirect(status_filter: str, notice: str) -> RedirectResponse:
    """Redirect a completed form action to a safe, fixed live-page URL."""
    safe_status = "closed" if status_filter == "closed" else "open"
    return RedirectResponse(
        url=f"/live-issues?status={safe_status}&notice={notice}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/live-issues", response_model=None, summary="Live Community Road Issues")
async def live_issues_page(
    request: Request,
    response: Response,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
    status_filter: Literal["open", "closed"] = Query(default="open", alias="status"),
    page: int = Query(default=1, ge=1, le=1000),
) -> StarletteResponse:
    """Show open and closed community issues with voting, broadcasts and admin controls."""
    try:
        issues, has_next = await run_in_threadpool(
            list_live_issues,
            access_token=ctx.access_token,
            user_id=ctx.user.id,
            status_filter=status_filter,
            page=page,
            app_settings=app_settings,
        )
    except LiveIssueServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.user_message) from exc

    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)
    template_response = templates.TemplateResponse(
        request=request,
        name="live_issues.html",
        context={
            "current_user": ctx.user,
            "active_nav": "live-issues",
            "csrf_token": csrf_token,
            "issues": issues,
            "status_filter": status_filter,
            "page": page,
            "has_next": has_next,
            "page_size": LIVE_ISSUES_PAGE_SIZE,
            "broadcast": get_active_broadcast(),
            "notice": request.query_params.get("notice", ""),
        },
    )
    _copy_set_cookie_headers(response, template_response)
    _copy_set_cookie_headers(csrf_carrier, template_response)
    return template_response


@router.post(
    "/live-issues/{report_id}/vote",
    summary="Vote Whether a Live Issue Is Real",
)
async def vote_on_live_issue(
    report_id: str,
    request: Request,
    ctx: AuthenticatedContext = Depends(get_authenticated_context),
    _csrf: None = Depends(require_csrf),
    app_settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Record a commuter confirm or dispute vote and return to the open issue list."""
    form = await request.form()
    try:
        vote = str(form.get("vote") or "").strip().lower()
    finally:
        await form.close()

    if vote not in ("confirm", "dispute"):
        return _page_redirect("open", "invalid-vote")

    try:
        await run_in_threadpool(
            cast_hazard_vote,
            report_id=report_id,
            user_id=ctx.user.id,
            access_token=ctx.access_token,
            vote_type=vote,
            app_settings=app_settings,
        )
    except CommunityServiceError as exc:
        notice = {
            403: "vote-not-allowed",
            404: "issue-not-found",
            409: "vote-already-recorded",
        }.get(exc.status_code, "vote-unavailable")
        return _page_redirect("open", notice)

    return _page_redirect("open", "vote-recorded")


@router.post(
    "/live-issues/{report_id}/admin-update",
    summary="Update a Live Issue (Admin Only)",
)
async def admin_update_live_issue(
    report_id: str,
    request: Request,
    ctx: AuthenticatedContext = Depends(require_admin_context),
    _csrf: None = Depends(require_csrf),
    app_settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Allow an administrator to correct issue details or change its status."""
    form = await request.form()
    try:
        return_status = str(form.get("return_status") or "open")
        payload = LiveIssueAdminUpdate.model_validate(
            {
                "category": form.get("category"),
                "description": form.get("description"),
                "address_text": form.get("address_text") or None,
                "status": form.get("status"),
            }
        )
    except ValidationError:
        return _page_redirect(return_status, "invalid-update")
    finally:
        await form.close()

    try:
        await run_in_threadpool(
            update_live_issue,
            report_id=report_id,
            payload=payload,
            access_token=ctx.access_token,
            app_settings=app_settings,
        )
    except LiveIssueServiceError as exc:
        notice = (
            "issue-not-found"
            if exc.status_code == 404
            else "invalid-update" if exc.status_code == 400 else "update-unavailable"
        )
        return _page_redirect(return_status, notice)

    return _page_redirect(return_status, "issue-updated")


@router.post(
    "/live-issues/{report_id}/admin-delete",
    summary="Delete a Live Issue (Admin Only)",
)
async def admin_delete_live_issue(
    report_id: str,
    request: Request,
    ctx: AuthenticatedContext = Depends(require_admin_context),
    _csrf: None = Depends(require_csrf),
    app_settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Allow an administrator to remove an issue and its linked database rows."""
    form = await request.form()
    try:
        return_status = str(form.get("return_status") or "open")
    finally:
        await form.close()

    try:
        await run_in_threadpool(
            delete_live_issue,
            report_id=report_id,
            access_token=ctx.access_token,
            app_settings=app_settings,
        )
    except LiveIssueServiceError as exc:
        notice = "issue-not-found" if exc.status_code == 404 else "delete-unavailable"
        return _page_redirect(return_status, notice)

    return _page_redirect(return_status, "issue-deleted")
