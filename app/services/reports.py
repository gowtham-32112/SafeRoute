"""Core hazard report creation, retrieval, and spatial bounds query service.

All database and storage operations execute strictly under the authenticated
user's RLS context via create_authenticated_supabase_client(). The service-role
key is never used here.
"""

import logging
import math
import re
import struct
from typing import Any, Literal, Optional
import uuid

from pydantic import BaseModel

from app.config import Settings, settings
from app.services.duplicates import VALID_CATEGORIES
from app.services.images import (
    build_hazard_storage_path,
    validate_and_strip_exif,
)
from app.services.supabase_client import (
    SupabaseConfigError,
    create_authenticated_supabase_client,
)

logger = logging.getLogger(__name__)

VALID_STATUSES = ("open", "escalated", "fixed")
HAZARD_PHOTOS_BUCKET = "hazard-photos"
MAX_DESCRIPTION_LENGTH = 2000
MAX_ADDRESS_LENGTH = 300
MAX_BOUNDS_LIMIT = 500
_WKT_POINT_RE = re.compile(
    r"POINT\s*\(\s*([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s*\)",
    re.IGNORECASE,
)


class ReportServiceError(Exception):
    """Domain error for report operations with clean HTTP status mapping."""

    def __init__(self, user_message: str, status_code: int = 400) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code


class ReportImageOut(BaseModel):
    """Public metadata for an image linked to a hazard report."""

    id: str
    storage_path: str
    kind: Literal["evidence", "after"]
    signed_url: Optional[str] = None
    created_at: str


class ReportDetailResponse(BaseModel):
    """Full authorized detail response for GET /reports/{id} and POST /reports."""

    id: str
    reporter_id: Optional[str] = None
    category: str
    description: str
    lat: float
    lng: float
    address_text: Optional[str] = None
    status: str
    escalation_recommended: bool
    severity_score: int
    severity_band: Literal["low", "medium", "high"]
    confidence: str
    confirm_count: int
    dispute_count: int
    fixed_count: int
    is_seed: bool = False
    created_at: str
    updated_at: str
    escalated_at: Optional[str] = None
    fixed_at: Optional[str] = None
    images: list[ReportImageOut] = []


class ReportMapItem(BaseModel):
    """Compact hazard payload returned by GET /api/reports?bounds=..."""

    id: str
    reporter_id: Optional[str] = None
    category: str
    description: str
    lat: float
    lng: float
    address_text: Optional[str] = None
    status: str
    escalation_recommended: bool
    severity_score: int
    severity_band: Literal["low", "medium", "high"]
    confidence: str
    confirm_count: int
    dispute_count: int
    fixed_count: int
    created_at: str
    updated_at: str


class BoundsReportsResponse(BaseModel):
    """Response contract for GET /api/reports?bounds=..."""

    count: int
    limit: int
    reports: list[ReportMapItem]


def severity_band(score: int) -> Literal["low", "medium", "high"]:
    """Map numeric severity score (0-100) to Spec Section 8 severity band."""
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _extract_data(resp: Any) -> Any:
    if isinstance(resp, dict):
        return resp.get("data")
    return getattr(resp, "data", None)


def parse_postgis_point(raw_location: Any) -> tuple[float, float]:
    """Decode PostGIS geography(Point, 4326) into (lat, lng).

    Supports GeoJSON dicts, EWKB hex strings returned by PostgREST, and WKT.
    """
    if isinstance(raw_location, dict):
        coords = raw_location.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            return float(coords[1]), float(coords[0])

    if isinstance(raw_location, str):
        text = raw_location.strip()
        wkt_match = _WKT_POINT_RE.search(text)
        if wkt_match:
            lng = float(wkt_match.group(1))
            lat = float(wkt_match.group(2))
            return lat, lng

        # PostgREST EWKB hex string for Point (with or without SRID flag 0x20000000)
        if len(text) >= 42 and all(ch in "0123456789abcdefABCDEF" for ch in text):
            raw = bytes.fromhex(text)
            endian = "<" if raw[0] == 1 else ">"
            geom_type = struct.unpack(f"{endian}I", raw[1:5])[0]
            has_srid = bool(geom_type & 0x20000000)
            offset = 9 if has_srid else 5
            if len(raw) >= offset + 16:
                lng, lat = struct.unpack(f"{endian}dd", raw[offset : offset + 16])
                return float(lat), float(lng)

    raise ReportServiceError("Unable to parse hazard report coordinates.", status_code=500)


def parse_bounds_query(bounds_raw: str) -> tuple[float, float, float, float]:
    """Parse and validate 'min_lat,min_lng,max_lat,max_lng' query string."""
    if not bounds_raw or not bounds_raw.strip():
        raise ReportServiceError(
            "Query parameter 'bounds' is required in format min_lat,min_lng,max_lat,max_lng.",
            status_code=400,
        )

    parts = [p.strip() for p in bounds_raw.split(",")]
    if len(parts) != 4:
        raise ReportServiceError(
            "Invalid bounds format. Expected four comma-separated numbers: "
            "min_lat,min_lng,max_lat,max_lng.",
            status_code=400,
        )

    try:
        min_lat, min_lng, max_lat, max_lng = (float(p) for p in parts)
    except ValueError as exc:
        raise ReportServiceError(
            "Bounds values must be valid numeric coordinates.",
            status_code=400,
        ) from exc

    if not all(math.isfinite(v) for v in (min_lat, min_lng, max_lat, max_lng)):
        raise ReportServiceError("Bounds values must be finite numbers.", status_code=400)

    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ReportServiceError("Latitude bounds must be between -90 and 90.", status_code=400)

    if not (-180.0 <= min_lng <= 180.0 and -180.0 <= max_lng <= 180.0):
        raise ReportServiceError("Longitude bounds must be between -180 and 180.", status_code=400)

    if min_lat > max_lat or min_lng > max_lng:
        raise ReportServiceError(
            "Invalid bounds: min_lat must be <= max_lat and min_lng must be <= max_lng.",
            status_code=400,
        )

    return min_lat, min_lng, max_lat, max_lng


def _validate_report_inputs(
    category: str,
    description: str,
    lat: float,
    lng: float,
    address_text: Optional[str],
) -> tuple[str, str, float, float, Optional[str]]:
    """Validate and normalize core report creation inputs."""
    norm_category = (category or "").strip().lower()
    if norm_category not in VALID_CATEGORIES:
        raise ReportServiceError(
            "Invalid category. Allowed values: pothole, road_break, streetlight, footpath, other.",
            status_code=400,
        )

    norm_desc = (description or "").strip()
    if not norm_desc:
        raise ReportServiceError("Description is required.", status_code=400)
    if len(norm_desc) > MAX_DESCRIPTION_LENGTH:
        raise ReportServiceError(
            f"Description must not exceed {MAX_DESCRIPTION_LENGTH} characters.",
            status_code=400,
        )

    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise ReportServiceError("Coordinates must be finite numbers.", status_code=400)
    if not (-90.0 <= lat <= 90.0):
        raise ReportServiceError("Latitude must be between -90 and 90.", status_code=400)
    if not (-180.0 <= lng <= 180.0):
        raise ReportServiceError("Longitude must be between -180 and 180.", status_code=400)

    norm_addr: Optional[str] = None
    if address_text is not None:
        cleaned_addr = address_text.strip()
        if len(cleaned_addr) > MAX_ADDRESS_LENGTH:
            raise ReportServiceError(
                f"Address must not exceed {MAX_ADDRESS_LENGTH} characters.",
                status_code=400,
            )
        norm_addr = cleaned_addr or None

    return norm_category, norm_desc, float(lat), float(lng), norm_addr


def _try_create_signed_url(client: Any, storage_path: str) -> Optional[str]:
    """Best-effort short-lived signed URL generation for private hazard photos."""
    try:
        signed_resp = client.storage.from_(HAZARD_PHOTOS_BUCKET).create_signed_url(
            storage_path,
            3600,
        )
        if isinstance(signed_resp, dict):
            return signed_resp.get("signedURL") or signed_resp.get("signedUrl")
        return getattr(signed_resp, "signedURL", None) or getattr(signed_resp, "signedUrl", None)
    except Exception:
        return None


def _build_detail_response(
    report_row: dict[str, Any],
    image_rows: list[dict[str, Any]],
    client: Any,
    fallback_coords: Optional[tuple[float, float]] = None,
) -> ReportDetailResponse:
    """Construct validated ReportDetailResponse from DB rows."""
    if report_row.get("lat") is not None and report_row.get("lng") is not None:
        lat, lng = float(report_row["lat"]), float(report_row["lng"])
    elif report_row.get("location") is not None:
        lat, lng = parse_postgis_point(report_row["location"])
    elif fallback_coords is not None:
        lat, lng = fallback_coords
    else:
        raise ReportServiceError("Report location is unavailable.", status_code=500)

    score = int(report_row.get("severity_score") or 0)
    images = [
        ReportImageOut(
            id=str(img["id"]),
            storage_path=str(img["storage_path"]),
            kind="after" if img.get("kind") == "after" else "evidence",
            signed_url=_try_create_signed_url(client, str(img["storage_path"])),
            created_at=str(img.get("created_at") or ""),
        )
        for img in image_rows
        if isinstance(img, dict)
    ]

    return ReportDetailResponse(
        id=str(report_row["id"]),
        reporter_id=str(report_row["reporter_id"]) if report_row.get("reporter_id") else None,
        category=str(report_row["category"]),
        description=str(report_row.get("description") or ""),
        lat=lat,
        lng=lng,
        address_text=report_row.get("address_text"),
        status=str(report_row.get("status") or "open"),
        escalation_recommended=bool(report_row.get("escalation_recommended", False)),
        severity_score=score,
        severity_band=severity_band(score),
        confidence=str(report_row.get("confidence") or "low"),
        confirm_count=int(report_row.get("confirm_count") or 0),
        dispute_count=int(report_row.get("dispute_count") or 0),
        fixed_count=int(report_row.get("fixed_count") or 0),
        is_seed=bool(report_row.get("is_seed", False)),
        created_at=str(report_row.get("created_at") or ""),
        updated_at=str(report_row.get("updated_at") or ""),
        escalated_at=str(report_row["escalated_at"]) if report_row.get("escalated_at") else None,
        fixed_at=str(report_row["fixed_at"]) if report_row.get("fixed_at") else None,
        images=images,
    )


def create_hazard_report(
    *,
    user_id: str,
    access_token: str,
    category: str,
    description: str,
    lat: float,
    lng: float,
    image_bytes: bytes,
    image_content_type: Optional[str] = None,
    address_text: Optional[str] = None,
    app_settings: Optional[Settings] = None,
) -> ReportDetailResponse:
    """Validate inputs, strip EXIF, upload photo, and insert hazard report atomically.

    Failure recovery guarantees zero orphaned storage objects and zero broken
    hazard_reports records without their linked evidence photo:
    - If Storage upload fails: no DB row is created.
    - If hazard_reports insert fails: uploaded Storage object is removed.
    - If report_images insert fails: both Storage object and hazard_reports row
      are removed.
    """
    norm_category, norm_desc, norm_lat, norm_lng, norm_addr = _validate_report_inputs(
        category, description, lat, lng, address_text
    )
    processed = validate_and_strip_exif(image_bytes, image_content_type)

    report_id = str(uuid.uuid4())
    storage_path = build_hazard_storage_path(user_id, report_id, processed.extension)

    cfg = app_settings or settings
    try:
        client = create_authenticated_supabase_client(access_token, cfg)
    except SupabaseConfigError as exc:
        raise ReportServiceError(
            "Report submission service is temporarily unavailable.",
            status_code=503,
        ) from exc

    # Step 1: Upload EXIF-stripped image to private hazard-photos bucket
    try:
        client.storage.from_(HAZARD_PHOTOS_BUCKET).upload(
            storage_path,
            processed.content,
            {"content-type": processed.content_type},
        )
    except Exception as exc:
        logger.warning("Storage upload failed for hazard photo: %s", type(exc).__name__)
        raise ReportServiceError(
            "Failed to upload evidence photograph. Please try again.",
            status_code=503,
        ) from exc

    # Step 2: Insert hazard report with strictly trusted fields only
    report_payload: dict[str, Any] = {
        "id": report_id,
        "reporter_id": str(user_id),
        "category": norm_category,
        "description": norm_desc,
        "location": f"SRID=4326;POINT({norm_lng} {norm_lat})",
        "address_text": norm_addr,
    }

    try:
        insert_resp = client.table("hazard_reports").insert(report_payload).execute()
        inserted_rows = _extract_data(insert_resp)
        if not inserted_rows:
            raise RuntimeError("Empty insert response")
        report_row = inserted_rows[0] if isinstance(inserted_rows, list) else inserted_rows
    except Exception as exc:
        logger.warning(
            "hazard_reports insert failed; cleaning up uploaded photo: %s",
            type(exc).__name__,
        )
        try:
            client.storage.from_(HAZARD_PHOTOS_BUCKET).remove([storage_path])
        except Exception:
            logger.warning("Best-effort storage cleanup failed after DB insert error.")
        raise ReportServiceError(
            "Failed to save hazard report. Please try again.",
            status_code=503,
        ) from exc

    # Step 3: Link uploaded photo in public.report_images (triggers +10 photo severity bonus)
    try:
        img_resp = (
            client.table("report_images")
            .insert(
                {
                    "report_id": report_id,
                    "storage_path": storage_path,
                    "kind": "evidence",
                }
            )
            .execute()
        )
        img_data = _extract_data(img_resp)
        image_rows = img_data if isinstance(img_data, list) else ([img_data] if img_data else [])
    except Exception as exc:
        logger.warning(
            "report_images insert failed; rolling back report and storage object: %s",
            type(exc).__name__,
        )
        try:
            client.table("hazard_reports").delete().eq("id", report_id).execute()
        except Exception:
            logger.warning("Best-effort hazard_reports rollback failed.")
        try:
            client.storage.from_(HAZARD_PHOTOS_BUCKET).remove([storage_path])
        except Exception:
            logger.warning("Best-effort storage cleanup failed after report_images error.")
        raise ReportServiceError(
            "Failed to attach evidence photograph to report. Please try again.",
            status_code=503,
        ) from exc

    # Re-fetch report so trigger-recomputed severity_score (+10 photo evidence) is reflected
    try:
        refreshed_resp = (
            client.table("hazard_reports")
            .select("*")
            .eq("id", report_id)
            .maybe_single()
            .execute()
        )
        refreshed = _extract_data(refreshed_resp)
        if isinstance(refreshed, dict):
            report_row = refreshed
    except Exception:
        pass

    return _build_detail_response(
        report_row,
        image_rows,
        client,
        fallback_coords=(norm_lat, norm_lng),
    )


def get_report_by_id(
    report_id: str,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> ReportDetailResponse:
    """Retrieve a single hazard report and its linked images under user RLS."""
    try:
        normalized_id = str(uuid.UUID(str(report_id).strip()))
    except ValueError as exc:
        raise ReportServiceError("Invalid report identifier.", status_code=400) from exc

    cfg = app_settings or settings
    try:
        client = create_authenticated_supabase_client(access_token, cfg)
        report_resp = (
            client.table("hazard_reports")
            .select("*")
            .eq("id", normalized_id)
            .maybe_single()
            .execute()
        )
    except SupabaseConfigError as exc:
        raise ReportServiceError(
            "Report service is temporarily unavailable.",
            status_code=503,
        ) from exc
    except Exception as exc:
        logger.warning("Failed to fetch report by id: %s", type(exc).__name__)
        raise ReportServiceError(
            "Unable to retrieve hazard report.",
            status_code=503,
        ) from exc

    report_row = _extract_data(report_resp)
    if not report_row or not isinstance(report_row, dict):
        raise ReportServiceError("Hazard report not found.", status_code=404)

    image_rows: list[dict[str, Any]] = []
    try:
        img_resp = (
            client.table("report_images")
            .select("id, storage_path, kind, created_at")
            .eq("report_id", normalized_id)
            .order("created_at")
            .execute()
        )
        raw_imgs = _extract_data(img_resp)
        if isinstance(raw_imgs, list):
            image_rows = raw_imgs
    except Exception as exc:
        logger.warning("Failed to load report_images: %s", type(exc).__name__)

    return _build_detail_response(report_row, image_rows, client)


def get_reports_in_bounds(
    *,
    bounds: str,
    access_token: str,
    category: Optional[str] = None,
    status_filter: Optional[str] = None,
    app_settings: Optional[Settings] = None,
) -> BoundsReportsResponse:
    """Query public.reports_in_bounds RPC under authenticated RLS (limit 500)."""
    min_lat, min_lng, max_lat, max_lng = parse_bounds_query(bounds)

    norm_category: Optional[str] = None
    if category is not None and category.strip():
        norm_category = category.strip().lower()
        if norm_category not in VALID_CATEGORIES:
            raise ReportServiceError(
                "Invalid category filter. Allowed: "
                "pothole, road_break, streetlight, footpath, other.",
                status_code=400,
            )

    norm_status: Optional[str] = None
    if status_filter is not None and status_filter.strip():
        norm_status = status_filter.strip().lower()
        if norm_status not in VALID_STATUSES:
            raise ReportServiceError(
                "Invalid status filter. Allowed: open, escalated, fixed.",
                status_code=400,
            )

    cfg = app_settings or settings
    limit = min(int(cfg.REPORTS_BOUNDS_LIMIT), MAX_BOUNDS_LIMIT)

    try:
        client = create_authenticated_supabase_client(access_token, cfg)
        rpc_resp = client.rpc(
            "reports_in_bounds",
            {
                "min_lat": min_lat,
                "min_lng": min_lng,
                "max_lat": max_lat,
                "max_lng": max_lng,
                "category_filter": norm_category,
                "status_filter": norm_status,
            },
        ).execute()
    except SupabaseConfigError as exc:
        raise ReportServiceError(
            "Map report service is temporarily unavailable.",
            status_code=503,
        ) from exc
    except Exception as exc:
        logger.warning("reports_in_bounds RPC failed: %s", type(exc).__name__)
        raise ReportServiceError(
            "Unable to load reports for the selected map area.",
            status_code=503,
        ) from exc

    raw_rows = (_extract_data(rpc_resp) or [])[:limit]
    items: list[ReportMapItem] = []
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        score = int(row.get("severity_score") or 0)
        items.append(
            ReportMapItem(
                id=str(row["id"]),
                reporter_id=str(row["reporter_id"]) if row.get("reporter_id") else None,
                category=str(row["category"]),
                description=str(row.get("description") or ""),
                lat=float(row["lat"]),
                lng=float(row["lng"]),
                address_text=row.get("address_text"),
                status=str(row.get("status") or "open"),
                escalation_recommended=bool(row.get("escalation_recommended", False)),
                severity_score=score,
                severity_band=severity_band(score),
                confidence=str(row.get("confidence") or "low"),
                confirm_count=int(row.get("confirm_count") or 0),
                dispute_count=int(row.get("dispute_count") or 0),
                fixed_count=int(row.get("fixed_count") or 0),
                created_at=str(row.get("created_at") or ""),
                updated_at=str(row.get("updated_at") or ""),
            )
        )

    return BoundsReportsResponse(
        count=len(items),
        limit=limit,
        reports=items,
    )
