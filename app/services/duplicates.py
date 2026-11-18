"""Duplicate hazard detection and 'same hazard' confirmation service.

Uses the authoritative Stage 4 PostGIS function public.nearby_reports under
the authenticated user's RLS context (same category, non-fixed, within 30m,
created within the last 30 days). Never trusts browser-calculated distances,
client-supplied search radii, or unverified previous duplicate-check results.
"""

from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any, Literal, Optional
import uuid

from pydantic import BaseModel, Field, field_validator

from app.config import (
    APPROVED_DUPLICATE_RADIUS_METERS,
    APPROVED_DUPLICATE_WINDOW_DAYS,
    Settings,
    settings,
)
from app.services.supabase_client import (
    SupabaseConfigError,
    create_authenticated_supabase_client,
)

logger = logging.getLogger(__name__)

HazardCategory = Literal["pothole", "road_break", "streetlight", "footpath", "other"]
VALID_CATEGORIES = ("pothole", "road_break", "streetlight", "footpath", "other")


class DuplicateServiceError(Exception):
    """Domain error for duplicate-check and confirmation operations."""

    def __init__(self, user_message: str, status_code: int = 400) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code


class DuplicateCheckRequest(BaseModel):
    """Validated input payload for POST /reports/check-duplicate.

    The duplicate search radius is locked server-side to 30 metres. Any client
    attempt to supply a different radius_m value is explicitly rejected (422).
    """

    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)
    category: HazardCategory
    radius_m: Optional[float] = None

    @field_validator("lat", "lng")
    @classmethod
    def _ensure_finite_coords(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Coordinate values must be finite numbers.")
        return value

    @field_validator("radius_m")
    @classmethod
    def _enforce_approved_radius(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if not math.isfinite(value) or not math.isclose(
            value,
            APPROVED_DUPLICATE_RADIUS_METERS,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "Duplicate search radius is fixed at 30 metres "
                "and cannot be changed by the client."
            )
        return APPROVED_DUPLICATE_RADIUS_METERS


class NearbyReportMatch(BaseModel):
    """Nearby existing hazard returned by public.nearby_reports."""

    id: str
    reporter_id: Optional[str] = None
    category: str
    description: str = ""
    lat: float
    lng: float
    address_text: Optional[str] = None
    status: str
    severity_score: int
    confidence: str
    confirm_count: int
    dispute_count: int
    created_at: str
    distance_m: float


class DuplicateCheckResponse(BaseModel):
    """Response contract for POST /reports/check-duplicate."""

    has_duplicates: bool
    radius_m: float
    lookback_days: int = APPROVED_DUPLICATE_WINDOW_DAYS
    matches: list[NearbyReportMatch]


class ConfirmDuplicateRequest(BaseModel):
    """Validated request payload for confirming an existing duplicate hazard.

    Requires the submitted hazard's coordinates and category so the backend can
    independently revalidate the duplicate relationship via PostGIS rather than
    trusting browser-calculated distances or prior check results.
    """

    report_id: str
    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)
    category: HazardCategory

    @field_validator("report_id")
    @classmethod
    def _validate_uuid(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value.strip()))
        except ValueError as exc:
            raise ValueError("Invalid report_id UUID format.") from exc

    @field_validator("lat", "lng")
    @classmethod
    def _ensure_finite_coords(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Coordinate values must be finite numbers.")
        return value


class ConfirmDuplicateResponse(BaseModel):
    """Response contract when user selects 'Same hazard, add my confirmation'."""

    action: Literal["confirmed_existing"] = "confirmed_existing"
    report_id: str
    vote: Literal["confirm"] = "confirm"
    message: str


def _extract_data(resp: Any) -> Any:
    if isinstance(resp, dict):
        return resp.get("data")
    return getattr(resp, "data", None)


def _parse_iso_timestamp(raw_value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp string into a UTC-aware datetime if valid."""
    if not raw_value:
        return None
    if isinstance(raw_value, datetime):
        if raw_value.tzinfo is None:
            return raw_value.replace(tzinfo=timezone.utc)
        return raw_value.astimezone(timezone.utc)
    if not isinstance(raw_value, str):
        return None
    text = raw_value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def check_nearby_duplicates(
    payload: DuplicateCheckRequest,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> DuplicateCheckResponse:
    """Query public.nearby_reports via authenticated RLS client at exact 30m/30d rules."""
    cfg = app_settings or settings
    radius_m = APPROVED_DUPLICATE_RADIUS_METERS
    window_days = APPROVED_DUPLICATE_WINDOW_DAYS

    try:
        client = create_authenticated_supabase_client(access_token, cfg)
        rpc_resp = client.rpc(
            "nearby_reports",
            {
                "lat": payload.lat,
                "lng": payload.lng,
                "radius_m": radius_m,
                "category": payload.category,
            },
        ).execute()
    except SupabaseConfigError as exc:
        logger.warning("Supabase config error on duplicate check: %s", type(exc).__name__)
        raise DuplicateServiceError(
            "Duplicate detection service is temporarily unavailable.",
            status_code=503,
        ) from exc
    except Exception as exc:
        logger.warning("nearby_reports RPC failed: %s", type(exc).__name__)
        raise DuplicateServiceError(
            "Unable to check for nearby duplicate reports right now.",
            status_code=503,
        ) from exc

    rows = _extract_data(rpc_resp) or []
    matches: list[NearbyReportMatch] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        matches.append(
            NearbyReportMatch(
                id=str(row["id"]),
                reporter_id=str(row["reporter_id"]) if row.get("reporter_id") else None,
                category=str(
                    row.get("report_category") or row.get("category") or payload.category
                ),
                description=str(row.get("description") or ""),
                lat=float(
                    row["report_lat"] if row.get("report_lat") is not None else row["lat"]
                ),
                lng=float(
                    row["report_lng"] if row.get("report_lng") is not None else row["lng"]
                ),
                address_text=row.get("address_text"),
                status=str(row.get("status") or "open"),
                severity_score=int(row.get("severity_score") or 0),
                confidence=str(row.get("confidence") or "low"),
                confirm_count=int(row.get("confirm_count") or 0),
                dispute_count=int(row.get("dispute_count") or 0),
                created_at=str(row.get("created_at") or ""),
                distance_m=round(float(row.get("distance_m") or 0.0), 2),
            )
        )

    return DuplicateCheckResponse(
        has_duplicates=len(matches) > 0,
        radius_m=radius_m,
        lookback_days=window_days,
        matches=matches,
    )


def confirm_existing_duplicate(
    report_id: str,
    lat: float,
    lng: float,
    category: str,
    user_id: str,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> ConfirmDuplicateResponse:
    """Create a 'confirm' vote after authoritatively revalidating duplicate criteria.

    Independently verifies server-side (using PostGIS public.nearby_reports and
    the target hazard_reports row under user RLS):
    1. Target report exists (404) and matches the submitted hazard category (400).
    2. Target report is within the approved 30-metre PostGIS radius (400).
    3. Target report was created within the last 30 days (400).
    4. Target report is not fixed (400).
    5. Current user is not the report owner (403).
    6. User has not already submitted a confirm vote for this report (409).
    """
    try:
        normalized_report_id = str(uuid.UUID(str(report_id).strip()))
    except ValueError as exc:
        raise DuplicateServiceError("Invalid report identifier.", status_code=400) from exc

    submitted_category = str(category or "").strip()
    if submitted_category not in VALID_CATEGORIES:
        raise DuplicateServiceError(
            "Invalid hazard category. Allowed: "
            "pothole, road_break, streetlight, footpath, other.",
            status_code=400,
        )

    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise DuplicateServiceError("Coordinates must be finite numbers.", status_code=400)
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        raise DuplicateServiceError(
            "Latitude must be between -90 and 90, and longitude between -180 and 180.",
            status_code=400,
        )

    cfg = app_settings or settings
    radius_m = APPROVED_DUPLICATE_RADIUS_METERS
    window_days = APPROVED_DUPLICATE_WINDOW_DAYS
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=window_days)

    try:
        client = create_authenticated_supabase_client(access_token, cfg)
        report_resp = (
            client.table("hazard_reports")
            .select("id, reporter_id, category, status, created_at")
            .eq("id", normalized_report_id)
            .maybe_single()
            .execute()
        )
    except SupabaseConfigError as exc:
        raise DuplicateServiceError(
            "Report confirmation service is temporarily unavailable.",
            status_code=503,
        ) from exc
    except Exception as exc:
        logger.warning(
            "Failed to inspect existing report for confirmation: %s",
            type(exc).__name__,
        )
        raise DuplicateServiceError(
            "Unable to verify existing hazard report.",
            status_code=503,
        ) from exc

    report_row = _extract_data(report_resp)
    if not report_row or not isinstance(report_row, dict):
        raise DuplicateServiceError("Existing hazard report not found.", status_code=404)

    if str(report_row.get("reporter_id") or "") == str(user_id):
        raise DuplicateServiceError(
            "You cannot confirm your own hazard report.",
            status_code=403,
        )

    if str(report_row.get("status") or "") == "fixed":
        raise DuplicateServiceError(
            "Cannot confirm a report that is already marked fixed.",
            status_code=400,
        )

    existing_category = str(report_row.get("category") or "").strip()
    if existing_category and existing_category != submitted_category:
        raise DuplicateServiceError(
            "Existing report does not match the submitted hazard category.",
            status_code=400,
        )

    created_at_dt = _parse_iso_timestamp(report_row.get("created_at"))
    if created_at_dt is not None and created_at_dt < cutoff_utc:
        raise DuplicateServiceError(
            "Existing report is older than 30 days and cannot be confirmed as a duplicate.",
            status_code=400,
        )

    # Authoritative PostGIS revalidation: ST_DWithin(30m), same category, non-fixed, <=30d
    try:
        nearby_resp = client.rpc(
            "nearby_reports",
            {
                "lat": float(lat),
                "lng": float(lng),
                "radius_m": radius_m,
                "category": submitted_category,
            },
        ).execute()
    except Exception as exc:
        logger.warning(
            "Authoritative nearby_reports revalidation failed: %s",
            type(exc).__name__,
        )
        raise DuplicateServiceError(
            "Unable to verify duplicate relationship right now.",
            status_code=503,
        ) from exc

    nearby_rows = _extract_data(nearby_resp) or []
    matched_row: Optional[dict[str, Any]] = None
    for row in nearby_rows:
        if isinstance(row, dict) and str(row.get("id") or "") == normalized_report_id:
            matched_row = row
            break

    if matched_row is None:
        raise DuplicateServiceError(
            "Selected report is not an eligible duplicate within 30 metres, "
            "30 days, and the same category.",
            status_code=400,
        )

    matched_category = str(
        matched_row.get("report_category")
        or matched_row.get("category")
        or submitted_category
    ).strip()
    matched_status = str(matched_row.get("status") or "open").strip()
    matched_created = _parse_iso_timestamp(matched_row.get("created_at"))
    raw_distance = matched_row.get("distance_m")
    distance_invalid = False
    if raw_distance is not None:
        try:
            dist_val = float(raw_distance)
            distance_invalid = not math.isfinite(dist_val) or dist_val > radius_m
        except (TypeError, ValueError):
            distance_invalid = True

    if (
        matched_category != submitted_category
        or matched_status == "fixed"
        or distance_invalid
        or (matched_created is not None and matched_created < cutoff_utc)
    ):
        raise DuplicateServiceError(
            "Selected report does not satisfy duplicate confirmation criteria.",
            status_code=400,
        )

    try:
        client.table("verifications").insert(
            {
                "report_id": normalized_report_id,
                "user_id": str(user_id),
                "vote": "confirm",
            }
        ).execute()
    except Exception as exc:
        err_lower = str(exc).lower()
        if "23505" in err_lower or "duplicate key" in err_lower or "unique" in err_lower:
            raise DuplicateServiceError(
                "You have already confirmed this hazard report.",
                status_code=409,
            ) from exc
        if (
            "42501" in err_lower
            or "own hazard report" in err_lower
            or "row-level security" in err_lower
        ):
            raise DuplicateServiceError(
                "You are not permitted to confirm this hazard report.",
                status_code=403,
            ) from exc
        logger.warning("Verification insert failed: %s", type(exc).__name__)
        raise DuplicateServiceError(
            "Unable to record your confirmation right now.",
            status_code=503,
        ) from exc

    return ConfirmDuplicateResponse(
        report_id=normalized_report_id,
        message="Confirmation added to the existing hazard report.",
    )
