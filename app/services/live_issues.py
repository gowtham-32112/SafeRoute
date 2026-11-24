"""Live issue listing and administrator-only maintenance operations."""

import logging
from typing import Any, Literal, Optional
import uuid

from pydantic import BaseModel, Field

from app.config import Settings, settings
from app.services.reports import (
    HAZARD_PHOTOS_BUCKET,
    MAX_ADDRESS_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    VALID_STATUSES,
    _extract_data,
    parse_postgis_point,
    severity_band,
)
from app.services.supabase_client import (
    SupabaseConfigError,
    create_authenticated_supabase_client,
)

logger = logging.getLogger(__name__)

LIVE_ISSUES_PAGE_SIZE = 12
LIVE_ISSUES_CLOSED_STATUS = "fixed"
LIVE_ISSUES_OPEN_STATUSES = ("open", "escalated")
LIVE_ISSUE_CATEGORIES = ("pothole", "road_break", "streetlight", "footpath", "other")


class LiveIssueServiceError(Exception):
    """Domain error for live issue operations with a safe user-facing message."""

    def __init__(self, user_message: str, status_code: int = 400) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code


class LiveIssueAdminUpdate(BaseModel):
    """Fields administrators may update on a live issue."""

    category: Literal["pothole", "road_break", "streetlight", "footpath", "other"]
    description: str = Field(..., min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    address_text: Optional[str] = Field(default=None, max_length=MAX_ADDRESS_LENGTH)
    status: Literal["open", "escalated", "fixed"]


class LiveIssueCard(BaseModel):
    """Safe card data for one issue on the commuter live issues page."""

    id: str
    category: str
    description: str
    address_text: Optional[str] = None
    location_label: str
    image_url: Optional[str] = None
    status: Literal["open", "escalated", "fixed"]
    severity_score: int
    severity_band: Literal["low", "medium", "high"]
    confirm_count: int
    dispute_count: int
    created_at: str
    updated_at: str
    reporter_id: Optional[str] = None
    is_own_report: bool = False
    my_vote: Optional[Literal["confirm", "dispute"]] = None


def _normalize_report_id(report_id: Any) -> str:
    try:
        return str(uuid.UUID(str(report_id).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LiveIssueServiceError("Invalid issue identifier.", status_code=400) from exc


def _create_short_signed_url(client: Any, storage_path: str) -> Optional[str]:
    """Create a short-lived signed URL for an authenticated issue photo."""
    try:
        result = client.storage.from_(HAZARD_PHOTOS_BUCKET).create_signed_url(storage_path, 900)
        if isinstance(result, dict):
            return result.get("signedURL") or result.get("signedUrl")
        return getattr(result, "signedURL", None) or getattr(result, "signedUrl", None)
    except Exception as exc:
        logger.warning("Could not create live issue photo URL: %s", type(exc).__name__)
        return None


def list_live_issues(
    *,
    access_token: str,
    user_id: str,
    status_filter: Literal["open", "closed"],
    page: int = 1,
    app_settings: Optional[Settings] = None,
) -> tuple[list[LiveIssueCard], bool]:
    """Return a page of authenticated reports with images and the viewer's vote state."""
    if page < 1 or page > 1000:
        raise LiveIssueServiceError("Invalid page number.", status_code=400)

    cfg = app_settings or settings
    try:
        client = create_authenticated_supabase_client(access_token, cfg)
        query = (
            client.table("hazard_reports")
            .select(
                "id, reporter_id, category, description, location, address_text, status, "
                "severity_score, confirm_count, dispute_count, created_at, updated_at"
            )
            .order("created_at", desc=True)
        )
        if status_filter == "closed":
            query = query.eq("status", LIVE_ISSUES_CLOSED_STATUS)
        else:
            query = query.in_("status", list(LIVE_ISSUES_OPEN_STATUSES))

        offset = (page - 1) * LIVE_ISSUES_PAGE_SIZE
        response = query.range(offset, offset + LIVE_ISSUES_PAGE_SIZE).execute()
    except SupabaseConfigError as exc:
        raise LiveIssueServiceError(
            "Live issues are temporarily unavailable.", status_code=503
        ) from exc
    except Exception as exc:
        logger.warning("Live issue query failed: %s", type(exc).__name__)
        raise LiveIssueServiceError(
            "Live issues are temporarily unavailable.", status_code=503
        ) from exc

    raw_rows = _extract_data(response) or []
    if not isinstance(raw_rows, list):
        raw_rows = []
    has_next = len(raw_rows) > LIVE_ISSUES_PAGE_SIZE
    report_rows = raw_rows[:LIVE_ISSUES_PAGE_SIZE]
    report_ids = [str(row["id"]) for row in report_rows if isinstance(row, dict) and row.get("id")]
    if not report_ids:
        return [], False

    images_by_report: dict[str, str] = {}
    try:
        image_response = (
            client.table("report_images")
            .select("report_id, storage_path, kind, created_at")
            .in_("report_id", report_ids)
            .eq("kind", "evidence")
            .order("created_at")
            .execute()
        )
        image_rows = _extract_data(image_response) or []
        if isinstance(image_rows, list):
            for image_row in image_rows:
                issue_id = str(image_row.get("report_id") or "")
                path = str(image_row.get("storage_path") or "")
                if issue_id and path and issue_id not in images_by_report:
                    images_by_report[issue_id] = path
    except Exception as exc:
        logger.warning("Live issue image lookup failed: %s", type(exc).__name__)

    votes_by_report: dict[str, Literal["confirm", "dispute"]] = {}
    try:
        vote_response = (
            client.table("verifications")
            .select("report_id, vote")
            .eq("user_id", user_id)
            .in_("report_id", report_ids)
            .in_("vote", ["confirm", "dispute"])
            .execute()
        )
        vote_rows = _extract_data(vote_response) or []
        if isinstance(vote_rows, list):
            for vote_row in vote_rows:
                issue_id = str(vote_row.get("report_id") or "")
                vote = vote_row.get("vote")
                if issue_id and vote in ("confirm", "dispute"):
                    votes_by_report[issue_id] = vote
    except Exception as exc:
        logger.warning("Live issue vote lookup failed: %s", type(exc).__name__)

    items: list[LiveIssueCard] = []
    for row in report_rows:
        if not isinstance(row, dict):
            continue
        issue_id = str(row.get("id") or "")
        try:
            lat, lng = parse_postgis_point(row.get("location"))
            location_label = str(row.get("address_text") or "").strip()
            if not location_label:
                location_label = f"{lat:.5f}, {lng:.5f}"
            score = max(0, min(100, int(row.get("severity_score") or 0)))
            storage_path = images_by_report.get(issue_id)
            items.append(
                LiveIssueCard(
                    id=issue_id,
                    category=str(row.get("category") or "other"),
                    description=str(row.get("description") or ""),
                    address_text=row.get("address_text"),
                    location_label=location_label,
                    image_url=_create_short_signed_url(client, storage_path) if storage_path else None,
                    status=str(row.get("status") or "open"),
                    severity_score=score,
                    severity_band=severity_band(score),
                    confirm_count=int(row.get("confirm_count") or 0),
                    dispute_count=int(row.get("dispute_count") or 0),
                    created_at=str(row.get("created_at") or ""),
                    updated_at=str(row.get("updated_at") or ""),
                    reporter_id=str(row.get("reporter_id")) if row.get("reporter_id") else None,
                    is_own_report=str(row.get("reporter_id") or "") == str(user_id),
                    my_vote=votes_by_report.get(issue_id),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Skipped malformed live issue %s: %s", issue_id, type(exc).__name__)

    return items, has_next


def update_live_issue(
    *,
    report_id: str,
    payload: LiveIssueAdminUpdate,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> None:
    """Update allowed issue fields using the authenticated administrator's RLS context."""
    issue_id = _normalize_report_id(report_id)
    description = payload.description.strip()
    address_text = (payload.address_text or "").strip() or None
    if not description:
        raise LiveIssueServiceError("Description cannot be empty.", status_code=400)
    if payload.category not in LIVE_ISSUE_CATEGORIES or payload.status not in VALID_STATUSES:
        raise LiveIssueServiceError("Invalid issue category or status.", status_code=400)
    if len(address_text or "") > MAX_ADDRESS_LENGTH:
        raise LiveIssueServiceError("Address is too long.", status_code=400)

    try:
        client = create_authenticated_supabase_client(access_token, app_settings or settings)
        response = (
            client.table("hazard_reports")
            .update(
                {
                    "category": payload.category,
                    "description": description,
                    "address_text": address_text,
                    "status": payload.status,
                }
            )
            .eq("id", issue_id)
            .select("id")
            .execute()
        )
        if not _extract_data(response):
            raise LiveIssueServiceError("Issue not found.", status_code=404)
    except LiveIssueServiceError:
        raise
    except SupabaseConfigError as exc:
        raise LiveIssueServiceError(
            "Could not update this issue. Please try again.", status_code=503
        ) from exc
    except Exception as exc:
        logger.warning("Live issue update failed: %s", type(exc).__name__)
        raise LiveIssueServiceError(
            "Could not update this issue. Please try again.", status_code=503
        ) from exc


def delete_live_issue(
    *,
    report_id: str,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> None:
    """Delete a report under admin RLS and best-effort remove linked private photos."""
    issue_id = _normalize_report_id(report_id)
    storage_paths: list[str] = []
    try:
        client = create_authenticated_supabase_client(access_token, app_settings or settings)
        report_response = (
            client.table("hazard_reports")
            .select("id")
            .eq("id", issue_id)
            .maybe_single()
            .execute()
        )
        if not _extract_data(report_response):
            raise LiveIssueServiceError("Issue not found.", status_code=404)

        image_response = (
            client.table("report_images")
            .select("storage_path")
            .eq("report_id", issue_id)
            .execute()
        )
        image_rows = _extract_data(image_response) or []
        storage_paths = [
            str(row["storage_path"])
            for row in image_rows
            if isinstance(row, dict) and row.get("storage_path")
        ]
        delete_response = (
            client.table("hazard_reports")
            .delete()
            .eq("id", issue_id)
            .select("id")
            .execute()
        )
        if not _extract_data(delete_response):
            raise LiveIssueServiceError("Issue not found.", status_code=404)
    except LiveIssueServiceError:
        raise
    except SupabaseConfigError as exc:
        raise LiveIssueServiceError(
            "Could not delete this issue. Please try again.", status_code=503
        ) from exc
    except Exception as exc:
        logger.warning("Live issue deletion failed: %s", type(exc).__name__)
        raise LiveIssueServiceError(
            "Could not delete this issue. Please try again.", status_code=503
        ) from exc

    if storage_paths:
        try:
            client.storage.from_(HAZARD_PHOTOS_BUCKET).remove(storage_paths)
        except Exception as exc:
            logger.warning("Deleted issue photo cleanup failed: %s", type(exc).__name__)
