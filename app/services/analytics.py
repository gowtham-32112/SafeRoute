"""SafeRoute Stage 9 Part 2 — Hazard Analytics & Heatmap Service (C5).

Features:
- Queries public.hazard_reports strictly under authenticated user JWT/RLS context.
- Aggregates KPIs:
  - total_reports
  - verification_rate: percentage of reports with at least one community verification
  - duplicate_rate: percentage of reports marked as duplicate
  - escalation_count: count of escalated reports
  - avg_time_to_fix_hours: average resolution duration in hours for fixed reports
  - by_category and by_status counts
  - top_affected_areas: Top 5 hotspot areas ranked descending by report count
  - heatmap_points: [lat, lng, intensity] array for Leaflet.heat
- Dynamically supports exclude_seed=True/False toggle.
- Zero-division safety: handles empty datasets gracefully without crashing.
- Zero references to administrative service-role secret.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from app.config import Settings
from app.services.reports import parse_postgis_point
from app.services.supabase_client import create_authenticated_supabase_client

logger = logging.getLogger(__name__)


class AnalyticsServiceError(Exception):
    """Raised when hazard data cannot be fetched for analytics."""


APPROVED_CATEGORIES = ("pothole", "road_break", "streetlight", "footpath", "other")
APPROVED_STATUSES = ("open", "escalated", "fixed")

CATEGORY_LABELS = {
    "pothole": "Pothole",
    "road_break": "Road Break",
    "streetlight": "Streetlight",
    "footpath": "Footpath",
    "other": "Other Hazard",
}

STATUS_LABELS = {
    "open": "Open",
    "escalated": "Escalated",
    "fixed": "Fixed",
}


class AffectedArea(BaseModel):
    """Hotspot area summary ranked in the Top 5 affected areas."""

    area_name: str
    report_count: int
    avg_severity: float
    lat: float
    lng: float


class CategoryCount(BaseModel):
    """Category breakdown item for charts and tables."""

    category: str
    label: str
    count: int
    percentage: float


class StatusCount(BaseModel):
    """Status breakdown item for charts and tables."""

    status: str
    label: str
    count: int
    percentage: float


class AnalyticsResponse(BaseModel):
    """Authoritative API response returned by GET /api/analytics."""

    total_reports: int
    verification_rate: float
    duplicate_rate: float
    escalation_count: int
    avg_time_to_fix_hours: Optional[float] = None
    by_category: Dict[str, int]
    by_status: Dict[str, int]
    category_breakdown: List[CategoryCount]
    status_breakdown: List[StatusCount]
    top_affected_areas: List[AffectedArea]
    heatmap_points: List[List[float]] = []  # [[lat, lng, intensity], ...]
    exclude_seed: bool = False


def _parse_iso_datetime(dt_val: Any) -> Optional[datetime]:
    """Parse ISO datetime string or return datetime object."""
    if not dt_val:
        return None
    if isinstance(dt_val, datetime):
        return dt_val if dt_val.tzinfo else dt_val.replace(tzinfo=timezone.utc)
    if isinstance(dt_val, str):
        try:
            cleaned = dt_val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def _extract_supabase_data(resp: Any) -> Any:
    """Helper to extract data from a Supabase postgrest response."""
    if isinstance(resp, dict):
        return resp.get("data")
    return getattr(resp, "data", None)


def compute_analytics(
    reports: List[Dict[str, Any]],
    exclude_seed: bool = False,
) -> AnalyticsResponse:
    """Compute all analytics metrics deterministically from a list of report dictionaries.

    Guarantees zero-division safety on empty datasets.
    """
    # Filter reports if exclude_seed is True
    if exclude_seed:
        filtered_reports = [r for r in reports if not bool(r.get("is_seed", False))]
    else:
        filtered_reports = list(reports)

    total = len(filtered_reports)

    # Initialize count dictionaries with all approved keys
    cat_counts: Dict[str, int] = {cat: 0 for cat in APPROVED_CATEGORIES}
    status_counts: Dict[str, int] = {st: 0 for st in APPROVED_STATUSES}

    if total == 0:
        cat_breakdown = [
            CategoryCount(
                category=cat,
                label=CATEGORY_LABELS.get(cat, cat.title()),
                count=0,
                percentage=0.0,
            )
            for cat in APPROVED_CATEGORIES
        ]
        status_breakdown = [
            StatusCount(
                status=st,
                label=STATUS_LABELS.get(st, st.title()),
                count=0,
                percentage=0.0,
            )
            for st in APPROVED_STATUSES
        ]
        return AnalyticsResponse(
            total_reports=0,
            verification_rate=0.0,
            duplicate_rate=0.0,
            escalation_count=0,
            avg_time_to_fix_hours=None,
            by_category=cat_counts,
            by_status=status_counts,
            category_breakdown=cat_breakdown,
            status_breakdown=status_breakdown,
            top_affected_areas=[],
            heatmap_points=[],
            exclude_seed=exclude_seed,
        )

    verified_count = 0
    duplicate_count = 0
    escalation_count = 0
    fix_durations_hours: List[float] = []

    # Map areas for grouping: area_name -> {"count": int, "severities": List[int], "lats": List[float], "lngs": List[float]}
    area_groups: Dict[str, Dict[str, Any]] = {}
    heatmap_points: List[List[float]] = []

    for r in filtered_reports:
        # 1. Category count
        cat = str(r.get("category") or "").strip().lower()
        if cat in cat_counts:
            cat_counts[cat] += 1
        else:
            cat_counts["other"] = cat_counts.get("other", 0) + 1

        # 2. Status count & escalation
        st = str(r.get("status") or "").strip().lower()
        if st in status_counts:
            status_counts[st] += 1
        else:
            status_counts["open"] = status_counts.get("open", 0) + 1

        if st == "escalated":
            escalation_count += 1

        # 3. Verification rate: confirm_count + dispute_count + fixed_count > 0
        confirms = int(r.get("confirm_count") or 0)
        disputes = int(r.get("dispute_count") or 0)
        fixeds = int(r.get("fixed_count") or 0)
        if (confirms + disputes + fixeds) > 0:
            verified_count += 1

        # 4. Duplicate rate
        if r.get("duplicate_of") is not None:
            duplicate_count += 1

        # 5. Resolution time calculation for fixed reports
        if st == "fixed" and r.get("fixed_at") and r.get("created_at"):
            created_dt = _parse_iso_datetime(r["created_at"])
            fixed_dt = _parse_iso_datetime(r["fixed_at"])
            if created_dt and fixed_dt and fixed_dt >= created_dt:
                hours = (fixed_dt - created_dt).total_seconds() / 3600.0
                fix_durations_hours.append(hours)

        # 6. Extract coordinates for heatmap and area clustering
        lat, lng = None, None
        if r.get("lat") is not None and r.get("lng") is not None:
            try:
                lat, lng = float(r["lat"]), float(r["lng"])
            except (ValueError, TypeError):
                pass
        elif r.get("location") is not None:
            try:
                lat, lng = parse_postgis_point(r["location"])
            except Exception:
                pass

        severity = max(0, min(100, int(r.get("severity_score") or 0)))

        if lat is not None and lng is not None:
            # Heatmap intensity between 0.2 and 1.0 based on severity
            intensity = round(max(0.2, min(1.0, severity / 100.0)), 2)
            heatmap_points.append([round(lat, 6), round(lng, 6), intensity])

            # Area grouping: prefer normalized address_text, fallback to grid coords
            raw_addr = str(r.get("address_text") or "").strip()
            if raw_addr and len(raw_addr) > 2:
                area_name = raw_addr
            else:
                area_name = f"Near {lat:.3f}, {lng:.3f}"

            if area_name not in area_groups:
                area_groups[area_name] = {
                    "count": 0,
                    "severities": [],
                    "lats": [],
                    "lngs": [],
                }
            area_groups[area_name]["count"] += 1
            area_groups[area_name]["severities"].append(severity)
            area_groups[area_name]["lats"].append(lat)
            area_groups[area_name]["lngs"].append(lng)

    # Compile Top 5 affected areas
    sorted_areas = sorted(
        area_groups.items(),
        key=lambda item: (item[1]["count"], sum(item[1]["severities"]) / max(1, len(item[1]["severities"]))),
        reverse=True,
    )

    top_areas: List[AffectedArea] = []
    for area_name, group in sorted_areas[:5]:
        cnt = group["count"]
        avg_sev = round(sum(group["severities"]) / max(1, cnt), 1)
        mean_lat = round(sum(group["lats"]) / max(1, cnt), 6)
        mean_lng = round(sum(group["lngs"]) / max(1, cnt), 6)
        top_areas.append(
            AffectedArea(
                area_name=area_name,
                report_count=cnt,
                avg_severity=avg_sev,
                lat=mean_lat,
                lng=mean_lng,
            )
        )

    # Compute KPI percentages
    verif_rate = round((verified_count / total) * 100.0, 1)
    dup_rate = round((duplicate_count / total) * 100.0, 1)

    avg_fix_hours: Optional[float] = None
    if fix_durations_hours:
        avg_fix_hours = round(sum(fix_durations_hours) / len(fix_durations_hours), 1)

    # Build category breakdown
    cat_breakdown = [
        CategoryCount(
            category=cat,
            label=CATEGORY_LABELS.get(cat, cat.title()),
            count=cat_counts[cat],
            percentage=round((cat_counts[cat] / total) * 100.0, 1),
        )
        for cat in APPROVED_CATEGORIES
    ]

    # Build status breakdown
    status_breakdown = [
        StatusCount(
            status=st,
            label=STATUS_LABELS.get(st, st.title()),
            count=status_counts[st],
            percentage=round((status_counts[st] / total) * 100.0, 1),
        )
        for st in APPROVED_STATUSES
    ]

    return AnalyticsResponse(
        total_reports=total,
        verification_rate=verif_rate,
        duplicate_rate=dup_rate,
        escalation_count=escalation_count,
        avg_time_to_fix_hours=avg_fix_hours,
        by_category=cat_counts,
        by_status=status_counts,
        category_breakdown=cat_breakdown,
        status_breakdown=status_breakdown,
        top_affected_areas=top_areas,
        heatmap_points=heatmap_points,
        exclude_seed=exclude_seed,
    )


async def get_hazard_analytics(
    *,
    access_token: str,
    app_settings: Settings,
    exclude_seed: bool = False,
) -> AnalyticsResponse:
    """Fetch hazard reports from Supabase under user JWT/RLS and compute analytics."""
    supabase = create_authenticated_supabase_client(access_token, app_settings)

    query = supabase.table("hazard_reports").select(
        "id, category, description, location, address_text, status, severity_score, "
        "confirm_count, dispute_count, fixed_count, duplicate_of, is_seed, created_at, fixed_at"
    )

    if exclude_seed:
        query = query.eq("is_seed", False)

    try:
        resp = query.execute()
        raw_rows = _extract_supabase_data(resp) or []
    except Exception as exc:
        logger.warning("Failed to query hazard_reports for analytics: %s", type(exc).__name__)
        raise AnalyticsServiceError("Analytics data is temporarily unavailable.") from exc

    return compute_analytics(raw_rows, exclude_seed=exclude_seed)
