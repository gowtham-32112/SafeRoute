"""SafeRoute Stage 9 — Hazard-Aware Route Planner Service (C3 / C4).

Features:
- Queries OSRM driving service with alternatives=true and geometries=geojson.
- Seamless fallback to deterministic cached demo routes on timeout or network error.
- PostGIS spatial intersection via hazards_near_route(route_geojson, buffer_m=25.0).
- Spec Section 10 risk calculation:
  - Severity multiplier: low -> 0.5, medium -> 0.8, high -> 1.0.
  - Excludes fixed hazards (status != 'fixed').
  - route_risk = sum(severity_score * multiplier).
  - route_score = duration_minutes + 0.05 * route_risk.
- Sorts alternative routes ascending by route_score (marking top route recommended).
- Mandatory route safety disclaimer:
  "Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."
- Operates strictly under authenticated user JWT/RLS context (never administrative service-role secret).
"""

from dataclasses import dataclass
import json
import logging
import math
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.reports import severity_band
from app.services.supabase_client import create_authenticated_supabase_client

logger = logging.getLogger(__name__)

ROUTE_SAFETY_DISCLAIMER: str = "Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."

RISK_MULTIPLIERS = {
    "low": 0.5,
    "medium": 0.8,
    "high": 1.0,
}


class RoutingError(Exception):
    """Base exception for routing and route planning errors."""


class IntersectingHazard(BaseModel):
    """Hazard located within the buffer distance (25m) of a candidate route."""

    id: str
    category: str
    description: str
    lat: float
    lng: float
    address_text: Optional[str] = None
    status: str
    severity_score: int
    severity_band: Literal["low", "medium", "high"]
    confidence: str
    confirm_count: int
    dispute_count: int
    distance_m: float
    multiplier: float
    risk_contribution: float


class RankedRoute(BaseModel):
    """A scored alternative driving route with risk metrics and geometry."""

    id: str
    summary: str
    duration_minutes: float
    distance_km: float
    route_risk: float
    route_score: float
    recommended: bool
    geojson: Dict[str, Any]
    hazards_count: int
    hazards: List[IntersectingHazard] = []


class RoutePlanResponse(BaseModel):
    """Authoritative API response returned by POST /api/routes/plan."""

    routes: List[RankedRoute]
    is_fallback_route: bool = False
    fallback_notice: Optional[str] = None
    disclaimer: str = ROUTE_SAFETY_DISCLAIMER


def compute_hazard_risk_multiplier(score: int) -> Tuple[str, float]:
    """Compute severity band and risk multiplier for a numeric severity score."""
    band = severity_band(score)
    mult = RISK_MULTIPLIERS.get(band, 1.0)
    return band, mult


def calculate_route_risk(hazards: List[Dict[str, Any]]) -> Tuple[float, List[IntersectingHazard]]:
    """Compute total route risk and enriched hazard objects for active hazards along a route.

    Filters out any 'fixed' hazards. Multipliers: low 0.5, medium 0.8, high 1.0.
    """
    total_risk = 0.0
    enriched: List[IntersectingHazard] = []

    for h in hazards:
        status = str(h.get("status") or "").strip().lower()
        if status == "fixed":
            continue

        raw_score = int(h.get("severity_score") or 0)
        score = max(0, min(100, raw_score))
        band, mult = compute_hazard_risk_multiplier(score)
        risk_contrib = round(score * mult, 2)
        total_risk += risk_contrib

        enriched.append(
            IntersectingHazard(
                id=str(h.get("id")),
                category=str(h.get("category") or "other"),
                description=str(h.get("description") or ""),
                lat=float(h.get("lat") or 0.0),
                lng=float(h.get("lng") or 0.0),
                address_text=h.get("address_text"),
                status=status or "open",
                severity_score=score,
                severity_band=band,  # type: ignore[arg-type]
                confidence=str(h.get("confidence") or "low"),
                confirm_count=int(h.get("confirm_count") or 0),
                dispute_count=int(h.get("dispute_count") or 0),
                distance_m=round(float(h.get("distance_m") or 0.0), 1),
                multiplier=mult,
                risk_contribution=risk_contrib,
            )
        )

    return round(total_risk, 2), enriched


def calculate_route_score(duration_minutes: float, route_risk: float) -> float:
    """Compute authoritative route score: duration_minutes + 0.05 * route_risk."""
    return round(float(duration_minutes) + (0.05 * float(route_risk)), 2)


def get_cached_demo_routes(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
) -> List[Dict[str, Any]]:
    """Generate deterministic cached demo route alternatives when OSRM is unreachable."""
    # Build coordinates between origin and destination
    # Route 1: Direct path with slight mid-arc
    mid1_lng = (origin_lng + dest_lng) / 2.0 + 0.0015
    mid1_lat = (origin_lat + dest_lat) / 2.0 + 0.0010
    coords_route_1 = [
        [round(origin_lng, 6), round(origin_lat, 6)],
        [round(mid1_lng, 6), round(mid1_lat, 6)],
        [round(dest_lng, 6), round(dest_lat, 6)],
    ]

    # Route 2: Alternative corridor with opposite curve
    mid2_lng = (origin_lng + dest_lng) / 2.0 - 0.0020
    mid2_lat = (origin_lat + dest_lat) / 2.0 - 0.0015
    coords_route_2 = [
        [round(origin_lng, 6), round(origin_lat, 6)],
        [round(mid2_lng, 6), round(mid2_lat, 6)],
        [round(dest_lng, 6), round(dest_lat, 6)],
    ]

    # Estimate base driving distance and duration via Haversine
    d_lat = math.radians(dest_lat - origin_lat)
    d_lng = math.radians(dest_lng - origin_lng)
    a = (
        math.sin(d_lat / 2.0) ** 2
        + math.cos(math.radians(origin_lat))
        * math.cos(math.radians(dest_lat))
        * math.sin(d_lng / 2.0) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    approx_km = max(0.5, round(6371.0 * c * 1.3, 2))  # ~1.3 detour index

    dur1_min = max(2.0, round((approx_km / 30.0) * 60.0, 1))  # 30 km/h city avg
    dur2_min = max(2.5, round(dur1_min * 1.15, 1))
    dist2_km = round(approx_km * 1.12, 2)

    return [
        {
            "id": "demo-route-main",
            "summary": "Primary Arterial Corridor (Demo Fallback)",
            "duration_minutes": dur1_min,
            "distance_km": approx_km,
            "geojson": {
                "type": "LineString",
                "coordinates": coords_route_1,
            },
        },
        {
            "id": "demo-route-alt",
            "summary": "Alternative Bypass Corridor (Demo Fallback)",
            "duration_minutes": dur2_min,
            "distance_km": dist2_km,
            "geojson": {
                "type": "LineString",
                "coordinates": coords_route_2,
            },
        },
    ]


async def fetch_osrm_routes(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    app_settings: Settings,
) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
    """Fetch driving routes from OSRM with alternatives=true and geometries=geojson.

    Returns: (raw_routes, is_fallback, fallback_notice)
    """
    base_url = (app_settings.OSRM_BASE_URL or "https://router.project-osrm.org").rstrip("/")
    timeout_sec = max(1.0, float(app_settings.OSRM_TIMEOUT_SECONDS))

    # OSRM coordinate order in URL path is strictly {lng},{lat};{lng},{lat}
    url = (
        f"{base_url}/route/v1/driving/"
        f"{origin_lng:.6f},{origin_lat:.6f};{dest_lng:.6f},{dest_lat:.6f}"
        f"?alternatives=true&overview=full&geometries=geojson&steps=false"
    )

    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})

        if resp.status_code != 200:
            logger.warning("OSRM returned HTTP status %d: %s", resp.status_code, resp.text[:200])
            fallback = get_cached_demo_routes(origin_lat, origin_lng, dest_lat, dest_lng)
            return (
                fallback,
                True,
                "Routing service temporarily unavailable. Displaying cached fallback route.",
            )

        data = resp.json()
        osrm_routes = data.get("routes") or []
        if not osrm_routes:
            fallback = get_cached_demo_routes(origin_lat, origin_lng, dest_lat, dest_lng)
            return (
                fallback,
                True,
                "No driving route returned by routing provider. Displaying cached fallback route.",
            )

        parsed_routes: List[Dict[str, Any]] = []
        for idx, r in enumerate(osrm_routes):
            dur_sec = float(r.get("duration") or 0.0)
            dist_m = float(r.get("distance") or 0.0)
            geom = r.get("geometry") or {}
            legs = r.get("legs") or []
            summary = (legs[0].get("summary") if legs else None) or f"Route {idx + 1}"

            parsed_routes.append({
                "id": f"osrm-route-{idx + 1}",
                "summary": summary,
                "duration_minutes": round(dur_sec / 60.0, 1),
                "distance_km": round(dist_m / 1000.0, 2),
                "geojson": geom,
            })

        return parsed_routes, False, None

    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning("OSRM request failed (%s). Using cached fallback routes.", type(exc).__name__)
        fallback = get_cached_demo_routes(origin_lat, origin_lng, dest_lat, dest_lng)
        return (
            fallback,
            True,
            "Live routing connection timed out. Serving cached demo fallback route corridor.",
        )
    except Exception as exc:
        logger.warning("Unexpected error during OSRM call: %s", exc)
        fallback = get_cached_demo_routes(origin_lat, origin_lng, dest_lat, dest_lng)
        return (
            fallback,
            True,
            "Routing service encountered an issue. Serving cached demo route.",
        )


def _extract_supabase_data(resp: Any) -> Any:
    """Helper to extract data from a Supabase postgrest response."""
    if isinstance(resp, dict):
        return resp.get("data")
    return getattr(resp, "data", None)


async def plan_hazard_aware_routes(
    *,
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    access_token: str,
    app_settings: Settings,
    buffer_meters: float = 25.0,
) -> RoutePlanResponse:
    """Plan driving routes, intersect hazards via PostGIS, and compute risk scores."""
    # 1. Fetch routes from OSRM or deterministic fallback
    raw_routes, is_fallback, fallback_notice = await fetch_osrm_routes(
        origin_lat, origin_lng, dest_lat, dest_lng, app_settings
    )

    supabase = create_authenticated_supabase_client(access_token, app_settings)
    ranked_routes: List[RankedRoute] = []

    # 2. For each route, query PostGIS hazards_near_route
    for r in raw_routes:
        geom = r.get("geojson") or {}
        route_hazards_raw: List[Dict[str, Any]] = []

        if geom and geom.get("type") in ("LineString", "MultiLineString") and geom.get("coordinates"):
            try:
                rpc_resp = (
                    supabase.rpc(
                        "hazards_near_route",
                        {
                            "route_geojson": geom,
                            "buffer_m": float(buffer_meters),
                        },
                    )
                    .execute()
                )
                data = _extract_supabase_data(rpc_resp)
                if isinstance(data, list):
                    route_hazards_raw = data
            except Exception as rpc_err:
                logger.warning("hazards_near_route RPC failed for route %s: %s", r.get("id"), rpc_err)
                raise RoutingError("Hazard data is temporarily unavailable.") from rpc_err

        # 3. Calculate route risk and score
        route_risk, enriched_hazards = calculate_route_risk(route_hazards_raw)
        dur_min = float(r.get("duration_minutes") or 0.0)
        route_score = calculate_route_score(dur_min, route_risk)

        ranked_routes.append(
            RankedRoute(
                id=str(r.get("id")),
                summary=str(r.get("summary") or "Alternative route"),
                duration_minutes=dur_min,
                distance_km=float(r.get("distance_km") or 0.0),
                route_risk=route_risk,
                route_score=route_score,
                recommended=False,  # Updated after sorting
                geojson=geom,
                hazards_count=len(enriched_hazards),
                hazards=enriched_hazards,
            )
        )

    # 4. Sort alternative routes ascending by route_score (lowest score = safest/fastest balance)
    ranked_routes.sort(key=lambda x: (x.route_score, x.duration_minutes))

    # 5. Mark the top route recommended
    if ranked_routes:
        ranked_routes[0].recommended = True

    return RoutePlanResponse(
        routes=ranked_routes,
        is_fallback_route=is_fallback,
        fallback_notice=fallback_notice,
        disclaimer=ROUTE_SAFETY_DISCLAIMER,
    )
