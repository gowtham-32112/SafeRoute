"""SafeRoute Stage 9 Part 2 — Hazard Analytics & Heatmap Router (C5).

Endpoints:
- GET /analytics: Authenticated full-page analytics dashboard with KPI cards,
  Leaflet.heat hotspot map, Top 5 affected areas, and Chart.js breakdowns.
- GET /api/analytics: Authenticated JSON API endpoint supporting exclude_seed query param.
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    AuthenticatedUser,
    get_authenticated_context,
    get_current_user,
    get_settings,
)
from app.services.analytics import (
    AnalyticsServiceError,
    AnalyticsResponse,
    get_hazard_analytics,
)
from app.services.session import issue_csrf_cookie

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Analytics"])


def _copy_set_cookie_headers(source: Response, target: Response) -> None:
    """Propagate any Set-Cookie headers from carrier response onto template response."""
    for header_name, header_value in source.raw_headers:
        if header_name.lower() == b"set-cookie":
            target.raw_headers.append((header_name, header_value))


def _prepare_page_csrf(request: Request, app_settings: Settings) -> tuple[str, Response]:
    """Reuse an active CSRF double-submit cookie or issue a fresh one for the page."""
    existing = request.cookies.get(app_settings.CSRF_COOKIE_NAME, "").strip()
    carrier = Response()
    if existing:
        return existing, carrier
    token = issue_csrf_cookie(carrier, app_settings)
    return token, carrier


@router.get("/analytics", summary="Hazard Analytics Dashboard Page")
async def analytics_page(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
    auth_ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
    exclude_seed: bool = Query(default=False),
) -> Response:
    """Render the authenticated analytics dashboard with KPIs, heatmap, and charts."""
    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)

    try:
        initial_analytics = await get_hazard_analytics(
            access_token=auth_ctx.access_token,
            app_settings=app_settings,
            exclude_seed=exclude_seed,
        )
    except AnalyticsServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analytics data is temporarily unavailable. Please try again shortly.",
        ) from exc

    map_config = {
        "fallbackLat": app_settings.DEFAULT_MAP_LAT,
        "fallbackLng": app_settings.DEFAULT_MAP_LNG,
        "fallbackLabel": "Kukatpally Y Junction",
        "defaultZoom": 13,
    }

    template_resp = templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "current_user": current_user,
            "active_nav": "analytics",
            "csrf_token": csrf_token,
            "analytics": initial_analytics.model_dump(),
            "map_config": map_config,
            "exclude_seed": exclude_seed,
        },
    )
    _copy_set_cookie_headers(response, template_resp)
    _copy_set_cookie_headers(csrf_carrier, template_resp)
    return template_resp


@router.get(
    "/api/analytics",
    response_model=AnalyticsResponse,
    summary="Get Hazard Analytics Aggregate Metrics JSON",
)
async def api_get_analytics(
    auth_ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
    exclude_seed: bool = Query(default=False),
) -> JSONResponse:
    """Return structured analytics metrics JSON with support for exclude_seed filtering."""
    try:
        analytics = await get_hazard_analytics(
            access_token=auth_ctx.access_token,
            app_settings=app_settings,
            exclude_seed=exclude_seed,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=analytics.model_dump(),
        )
    except AnalyticsServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analytics data is temporarily unavailable. Please try again shortly.",
        ) from exc
    except Exception:
        logger.exception("Unexpected failure while computing analytics.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to compute analytics.",
        )
