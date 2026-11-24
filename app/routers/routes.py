"""SafeRoute Stage 9 — Hazard-Aware Route Planner Router (C3 / C4).

Endpoints:
- GET /routes: Authenticated route planner page with interactive Leaflet map,
  waypoint pickers, and ranked route comparison.
- POST /api/routes/plan: Authenticated, CSRF-protected route planning endpoint.
  Queries OSRM for alternative driving routes, buffers them in PostGIS
  via hazards_near_route(route_geojson, 25), computes route risk and score,
  and returns ranked routes.
- POST /routes/plan: Alias endpoint for backwards compatibility.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    AuthenticatedUser,
    get_authenticated_context,
    get_current_user,
    get_settings,
    require_csrf,
)
from app.services.routing import (
    ROUTE_SAFETY_DISCLAIMER,
    RankedRoute,
    RoutingError,
    RoutePlanResponse,
    plan_hazard_aware_routes,
)
from app.services.session import issue_csrf_cookie

ROUTE_SAFETY_DISCLAIMER: str = "Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Routes"])

KUKATPALLY_LANDMARKS = [
    {"name": "Kukatpally Y Junction", "lat": 17.4849, "lng": 78.4138},
    {"name": "KPHB Colony Metro Station", "lat": 17.4930, "lng": 78.3980},
    {"name": "JNTU Hyderabad Junction", "lat": 17.4975, "lng": 78.3888},
    {"name": "Ameerpet Metro Junction", "lat": 17.4375, "lng": 78.4483},
    {"name": "Miyapur Metro Terminal", "lat": 17.4968, "lng": 78.3614},
]


class PlanRoutePayload(BaseModel):
    """Input payload for planning hazard-aware routes."""

    origin_lat: float = Field(..., ge=-90.0, le=90.0)
    origin_lng: float = Field(..., ge=-180.0, le=180.0)
    dest_lat: float = Field(..., ge=-90.0, le=90.0)
    dest_lng: float = Field(..., ge=-180.0, le=180.0)
    origin_label: Optional[str] = None
    dest_label: Optional[str] = None


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


async def _parse_json_or_form(request: Request) -> Dict[str, Any]:
    """Parse either JSON body or HTML form payload into a dictionary."""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                return raw
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="JSON body must be an object.",
            )
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed JSON in request body.",
            )

    if (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        form = await request.form()
        return dict(form)

    # Fall back to attempting JSON parse
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Content-Type must be application/json or form-encoded.",
    )


@router.get("/routes", summary="Hazard-Aware Route Planner Page")
async def routes_page(
    request: Request,
    response: Response,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app_settings: Settings = Depends(get_settings),
) -> Response:
    """Render the authenticated Route Planner page with map, origin/dest controls, and rankings."""
    csrf_token, csrf_carrier = _prepare_page_csrf(request, app_settings)
    map_config = {
        "fallbackLat": app_settings.DEFAULT_MAP_LAT,
        "fallbackLng": app_settings.DEFAULT_MAP_LNG,
        "fallbackLabel": "Kukatpally Y Junction",
        "defaultZoom": 14,
        "geolocationTimeoutMs": int(app_settings.GEOLOCATION_TIMEOUT_SECONDS * 1000),
    }

    template_resp = templates.TemplateResponse(
        request=request,
        name="routes.html",
        context={
            "current_user": current_user,
            "active_nav": "routes",
            "csrf_token": csrf_token,
            "map_config": map_config,
            "landmarks": KUKATPALLY_LANDMARKS,
            "disclaimer": ROUTE_SAFETY_DISCLAIMER,
        },
    )
    _copy_set_cookie_headers(response, template_resp)
    _copy_set_cookie_headers(csrf_carrier, template_resp)
    return template_resp


@router.post(
    "/api/routes/plan",
    response_model=RoutePlanResponse,
    summary="Plan Hazard-Aware Routes with OSRM and PostGIS",
)
@router.post(
    "/routes/plan",
    response_model=RoutePlanResponse,
    include_in_schema=False,
)
async def api_plan_routes(
    request: Request,
    auth_ctx: AuthenticatedContext = Depends(get_authenticated_context),
    app_settings: Settings = Depends(get_settings),
    _csrf: None = Depends(require_csrf),
) -> JSONResponse:
    """Plan driving routes between origin and destination coordinates.

    Queries OSRM driving service, buffers routes by 25m in PostGIS via hazards_near_route,
    computes hazard risk score, and ranks routes ascending by total route score.
    """
    raw_payload = await _parse_json_or_form(request)

    try:
        payload = PlanRoutePayload(**raw_payload)
    except ValidationError as val_err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=val_err.errors(),
        )

    # Validate distinct coordinates (origin != destination)
    lat_diff = abs(payload.origin_lat - payload.dest_lat)
    lng_diff = abs(payload.origin_lng - payload.dest_lng)
    if lat_diff < 0.0001 and lng_diff < 0.0001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Origin and destination must be distinct locations.",
        )

    try:
        plan_result = await plan_hazard_aware_routes(
            origin_lat=payload.origin_lat,
            origin_lng=payload.origin_lng,
            dest_lat=payload.dest_lat,
            dest_lng=payload.dest_lng,
            access_token=auth_ctx.access_token,
            app_settings=app_settings,
            buffer_meters=app_settings.ROUTE_HAZARD_BUFFER_METERS,
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=plan_result.model_dump())
    except RoutingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Route hazard data is temporarily unavailable. Please try again shortly.",
        ) from exc
    except Exception:
        logger.exception("Unexpected failure while planning routes.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to plan routes.",
        )
