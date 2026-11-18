"""Duplicate hazard detection and 'same hazard' confirmation service.

Uses the authoritative Stage 4 PostGIS function public.nearby_reports under
the authenticated user's RLS context (same category, non-fixed, within 30m,
created within the last 30 days). Never trusts browser-calculated distances.
"""

import logging
import math
from typing import Any, Literal, Optional
import uuid

from pydantic import BaseModel, Field, field_validator

from app.config import Settings, settings
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
    """Validated input payload for POST /reports/check-duplicate."""

    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)
    category: HazardCategory
    radius_m: float = Field(default=30.0, gt=0.0, le=50000.0)

    @field_validator("lat", "lng", "radius_m")
    @classmethod
    def _ensure_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Coordinate and radius values must be finite numbers.")
        return value


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
    matches: list[NearbyReportMatch]


class ConfirmDuplicateRequest(BaseModel):
    """Validated request payload for confirming an existing duplicate hazard."""

    report_id: str

    @field_validator("report_id")
    @classmethod
    def _validate_uuid(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value.strip()))
        except ValueError as exc:
            raise ValueError("Invalid report_id UUID format.") from exc


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


def check_nearby_duplicates(
    payload: DuplicateCheckRequest,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> DuplicateCheckResponse:
    """Query public.nearby_reports via authenticated RLS client."""
    cfg = app_settings or settings
    radius_m = float(payload.radius_m or cfg.DUPLICATE_RADIUS_METERS)

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
                category=str(row.get("report_category") or row.get("category") or payload.category),
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
        matches=matches,
    )


def confirm_existing_duplicate(
    report_id: str,
    user_id: str,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> ConfirmDuplicateResponse:
    """Create a 'confirm' vote on an existing report instead of creating a duplicate.

    Enforces server-side (in addition to Stage 4 DB triggers and RLS):
    - Existing report must exist and not be fixed.
    - Users cannot confirm their own report (403).
    - Duplicate confirm votes by the same user are rejected (409).
    """
    try:
        normalized_report_id = str(uuid.UUID(str(report_id).strip()))
    except ValueError as exc:
        raise DuplicateServiceError("Invalid report identifier.", status_code=400) from exc

    cfg = app_settings or settings
    try:
        client = create_authenticated_supabase_client(access_token, cfg)
        report_resp = (
            client.table("hazard_reports")
            .select("id, reporter_id, status")
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
        logger.warning("Failed to inspect existing report for confirmation: %s", type(exc).__name__)
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
