"""Community hazard verification (voting) and comments service.

All database and storage operations execute strictly under the authenticated
user's RLS context via create_authenticated_supabase_client(). The service-role
key is never used here.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Literal, Optional
import uuid

from pydantic import BaseModel, Field

from app.config import Settings, settings
from app.services.images import (
    build_hazard_storage_path,
    validate_and_strip_exif,
)
from app.services.reports import (
    HAZARD_PHOTOS_BUCKET,
    ReportServiceError,
    _extract_data,
    severity_band,
)
from app.services.supabase_client import (
    SupabaseConfigError,
    create_authenticated_supabase_client,
)

logger = logging.getLogger(__name__)

VoteType = Literal["confirm", "dispute", "fixed"]
VALID_VOTES = ("confirm", "dispute", "fixed")
MAX_COMMENT_LENGTH = 1000


class CommunityServiceError(ReportServiceError):
    """Domain error for community verification and comments with clean HTTP status mapping."""

    def __init__(self, user_message: str, status_code: int = 400) -> None:
        super().__init__(user_message=user_message, status_code=status_code)


def validate_report_uuid(report_id: Any) -> str:
    """Validate report UUID format, raising HTTP 400 on malformed input."""
    try:
        parsed = uuid.UUID(str(report_id).strip())
        return str(parsed)
    except (ValueError, AttributeError, TypeError) as exc:
        raise CommunityServiceError("Invalid report ID format.", status_code=400) from exc


class VoteRequest(BaseModel):
    """Validated input payload for casting a verification vote."""

    vote: VoteType


class VoteResponse(BaseModel):
    """Authoritative response returned after casting a hazard vote."""

    report_id: str
    user_id: str
    vote: VoteType
    status: str
    severity_score: int
    severity_band: Literal["low", "medium", "high"]
    confidence: str
    confirm_count: int
    dispute_count: int
    fixed_count: int
    escalation_recommended: bool
    fixed_at: Optional[str] = None
    created_at: str


class CommentCreateRequest(BaseModel):
    """Validated input payload for creating a community comment."""

    body: str = Field(..., min_length=1, max_length=MAX_COMMENT_LENGTH)
    hide_name: bool = False


class CommentResponse(BaseModel):
    """Public safe metadata for a hazard report comment."""

    id: str
    report_id: str
    user_id: Optional[str] = None
    author_name: str
    body: str
    is_anonymous: bool = False
    created_at: str


class CommentsListResponse(BaseModel):
    """Collection of comments returned for a hazard report."""

    report_id: str
    total: int
    comments: list[CommentResponse] = []


def cast_hazard_vote(
    *,
    report_id: str,
    user_id: str,
    access_token: str,
    vote_type: str,
    after_photo_bytes: Optional[bytes] = None,
    after_photo_content_type: Optional[str] = None,
    app_settings: Optional[Settings] = None,
) -> VoteResponse:
    """Cast a verification vote (confirm, dispute, or fixed) on a hazard report.

    Enforces:
    - UUID format validation (HTTP 400 on malformed UUID).
    - Report existence (HTTP 404 if missing).
    - Self-voting prevention: users cannot vote on their own report (HTTP 403).
    - Duplicate voting prevention: users cannot recast the same vote or cast
      conflicting confirm/dispute votes (HTTP 409).
    - Optional after-photo on 'fixed' votes with EXIF stripping and storage upload.
    - PostgreSQL triggers handle recomputing confirm_count, dispute_count,
      fixed_count, confidence, severity_score, escalation_recommended, and
      transitioning status to 'fixed' when 2 distinct users vote 'fixed'.
    """
    norm_report_id = validate_report_uuid(report_id)
    norm_vote = (vote_type or "").strip().lower()
    if norm_vote not in VALID_VOTES:
        raise CommunityServiceError(
            "Invalid vote type. Allowed values: confirm, dispute, fixed.",
            status_code=400,
        )

    cfg = app_settings or settings
    try:
        client = create_authenticated_supabase_client(access_token, cfg)
    except SupabaseConfigError as exc:
        raise CommunityServiceError(
            "Voting service is temporarily unavailable.",
            status_code=503,
        ) from exc

    # 1. Fetch parent report to verify existence and check self-voting
    try:
        report_res = (
            client.table("hazard_reports")
            .select("*")
            .eq("id", norm_report_id)
            .maybe_single()
            .execute()
        )
        report_raw = _extract_data(report_res)
        report_row = (
            report_raw[0]
            if isinstance(report_raw, list) and report_raw
            else (report_raw if isinstance(report_raw, dict) else None)
        )
    except Exception as exc:
        logger.warning("Failed to fetch hazard report for vote check: %s", type(exc).__name__)
        raise CommunityServiceError(
            "Unable to verify hazard report right now.",
            status_code=503,
        ) from exc

    if not report_row or not isinstance(report_row, dict):
        raise CommunityServiceError("Hazard report not found.", status_code=404)

    if str(report_row.get("status") or "open").strip().lower() == "fixed":
        raise CommunityServiceError(
            "Closed issues no longer accept votes.",
            status_code=409,
        )

    reporter_id = report_row.get("reporter_id")
    if reporter_id and str(reporter_id) == str(user_id):
        raise CommunityServiceError(
            "Users cannot vote on their own hazard report.",
            status_code=403,
        )

    # 2. Check existing votes by this user on this report
    try:
        existing_res = (
            client.table("verifications")
            .select("vote")
            .eq("report_id", norm_report_id)
            .eq("user_id", str(user_id))
            .execute()
        )
        existing_rows = _extract_data(existing_res) or []
    except Exception as exc:
        logger.warning("Failed to check existing verifications: %s", type(exc).__name__)
        raise CommunityServiceError(
            "Unable to verify your voting eligibility right now.",
            status_code=503,
        ) from exc

    existing_votes = {
        row.get("vote") for row in existing_rows if isinstance(row, dict) and row.get("vote")
    }

    if norm_vote in existing_votes:
        raise CommunityServiceError(
            f"You have already submitted a '{norm_vote}' vote for this hazard report.",
            status_code=409,
        )
    if norm_vote in ("confirm", "dispute") and ("confirm" in existing_votes or "dispute" in existing_votes):
        raise CommunityServiceError(
            "You have already verified this hazard report and cannot cast conflicting votes.",
            status_code=409,
        )

    # 3. Handle optional after-photo on 'fixed' votes
    after_photo_storage_path: Optional[str] = None
    if after_photo_bytes is not None:
        if norm_vote != "fixed":
            raise CommunityServiceError(
                "An after-photo can only be attached to a 'fixed' vote.",
                status_code=400,
            )
        processed = validate_and_strip_exif(after_photo_bytes, after_photo_content_type)
        after_photo_storage_path = build_hazard_storage_path(
            user_id, norm_report_id, processed.extension
        )

        try:
            client.storage.from_(HAZARD_PHOTOS_BUCKET).upload(
                after_photo_storage_path,
                processed.content,
                {"content-type": processed.content_type},
            )
        except Exception as exc:
            logger.warning("Storage upload failed for after photo: %s", type(exc).__name__)
            raise CommunityServiceError(
                "Failed to upload after photo. Please try again.",
                status_code=503,
            ) from exc

        # Best-effort record in report_images
        try:
            client.table("report_images").insert(
                {
                    "report_id": norm_report_id,
                    "storage_path": after_photo_storage_path,
                    "kind": "after",
                }
            ).execute()
        except Exception as exc:
            logger.info(
                "report_images insert skipped or restricted by policy: %s",
                type(exc).__name__,
            )

    # 4. Insert into public.verifications
    try:
        client.table("verifications").insert(
            {
                "report_id": norm_report_id,
                "user_id": str(user_id),
                "vote": norm_vote,
            }
        ).execute()
    except Exception as exc:
        err_lower = str(exc).lower()
        if "23505" in err_lower or "duplicate key" in err_lower or "unique" in err_lower:
            raise CommunityServiceError(
                f"You have already submitted a '{norm_vote}' vote for this hazard report.",
                status_code=409,
            ) from exc
        if (
            "42501" in err_lower
            or "own hazard report" in err_lower
            or "row-level security" in err_lower
        ):
            raise CommunityServiceError(
                "Users cannot vote on their own hazard report.",
                status_code=403,
            ) from exc
        logger.warning("Verification insert failed: %s", type(exc).__name__)
        # Clean up uploaded after-photo if verification insert failed
        if after_photo_storage_path:
            try:
                client.storage.from_(HAZARD_PHOTOS_BUCKET).remove([after_photo_storage_path])
            except Exception:
                pass
        raise CommunityServiceError(
            "Unable to record your vote right now.",
            status_code=503,
        ) from exc

    # 5. Re-fetch updated hazard report so trigger-recomputed metrics are reflected
    try:
        refreshed_res = (
            client.table("hazard_reports")
            .select("*")
            .eq("id", norm_report_id)
            .maybe_single()
            .execute()
        )
        refreshed = _extract_data(refreshed_res)
        if isinstance(refreshed, dict):
            report_row = refreshed
    except Exception:
        pass

    score = int(report_row.get("severity_score") or 0)
    band = severity_band(score)

    return VoteResponse(
        report_id=norm_report_id,
        user_id=str(user_id),
        vote=norm_vote,  # type: ignore[arg-type]
        status=str(report_row.get("status") or "open"),
        severity_score=score,
        severity_band=band,
        confidence=str(report_row.get("confidence") or "low"),
        confirm_count=int(report_row.get("confirm_count") or 0),
        dispute_count=int(report_row.get("dispute_count") or 0),
        fixed_count=int(report_row.get("fixed_count") or 0),
        escalation_recommended=bool(report_row.get("escalation_recommended", False)),
        fixed_at=str(report_row["fixed_at"]) if report_row.get("fixed_at") else None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def create_report_comment(
    *,
    report_id: str,
    user_id: str,
    access_token: str,
    body: str,
    hide_name: bool = False,
    app_settings: Optional[Settings] = None,
) -> CommentResponse:
    """Create a new community comment on a hazard report.

    Validates:
    - UUID format validation (HTTP 400 on malformed UUID).
    - Report existence (HTTP 404 if missing).
    - Non-empty, trimmed comment text (1..1000 characters).
    - Honors user privacy: displays 'Community Member' when hide_name is True,
      never leaking user email or internal secrets.
    """
    norm_report_id = validate_report_uuid(report_id)
    trimmed_body = (body or "").strip()
    if not trimmed_body:
        raise CommunityServiceError("Comment text cannot be empty.", status_code=400)
    if len(trimmed_body) > MAX_COMMENT_LENGTH:
        raise CommunityServiceError(
            f"Comment must not exceed {MAX_COMMENT_LENGTH} characters.",
            status_code=400,
        )

    cfg = app_settings or settings
    try:
        client = create_authenticated_supabase_client(access_token, cfg)
    except SupabaseConfigError as exc:
        raise CommunityServiceError(
            "Comments service is temporarily unavailable.",
            status_code=503,
        ) from exc

    # 1. Verify parent report exists
    try:
        report_res = (
            client.table("hazard_reports")
            .select("id")
            .eq("id", norm_report_id)
            .maybe_single()
            .execute()
        )
        report_raw = _extract_data(report_res)
        report_data = (
            report_raw[0]
            if isinstance(report_raw, list) and report_raw
            else (report_raw if isinstance(report_raw, dict) else None)
        )
    except Exception as exc:
        logger.warning("Failed to verify report for comment creation: %s", type(exc).__name__)
        raise CommunityServiceError(
            "Unable to post comment right now.",
            status_code=503,
        ) from exc

    if not report_data or not isinstance(report_data, dict):
        raise CommunityServiceError("Hazard report not found.", status_code=404)

    # 2. Resolve author display name according to privacy option
    is_anon = bool(hide_name)
    author_name = "Community Member"

    if not is_anon:
        try:
            profile_res = (
                client.table("profiles")
                .select("full_name")
                .eq("id", str(user_id))
                .maybe_single()
                .execute()
            )
            profile_raw = _extract_data(profile_res)
            p_row = (
                profile_raw[0]
                if isinstance(profile_raw, list) and profile_raw
                else (profile_raw if isinstance(profile_raw, dict) else {})
            )
            if p_row.get("full_name"):
                author_name = str(p_row["full_name"]).strip()
        except Exception:
            author_name = "Community Member"

    # 3. Insert comment into public.comments
    comment_id = str(uuid.uuid4())
    comment_payload = {
        "id": comment_id,
        "report_id": norm_report_id,
        "user_id": str(user_id),
        "body": trimmed_body,
    }

    try:
        insert_res = client.table("comments").insert(comment_payload).execute()
        inserted_rows = _extract_data(insert_res)
        c_row = inserted_rows[0] if isinstance(inserted_rows, list) and inserted_rows else (
            inserted_rows if isinstance(inserted_rows, dict) else {}
        )
    except Exception as exc:
        logger.warning("Comment insert failed: %s", type(exc).__name__)
        raise CommunityServiceError(
            "Failed to post comment. Please try again.",
            status_code=503,
        ) from exc

    created_at = (
        str(c_row.get("created_at"))
        if c_row.get("created_at")
        else datetime.now(timezone.utc).isoformat()
    )

    return CommentResponse(
        id=str(c_row.get("id") or comment_id),
        report_id=norm_report_id,
        user_id=None if is_anon else str(user_id),
        author_name=author_name,
        body=trimmed_body,
        is_anonymous=is_anon,
        created_at=created_at,
    )


def list_report_comments(
    *,
    report_id: str,
    access_token: str,
    app_settings: Optional[Settings] = None,
) -> CommentsListResponse:
    """Retrieve all comments for a hazard report ordered chronologically (created_at ASC).

    Enforces:
    - UUID format validation (HTTP 400 on malformed UUID).
    - Report existence (HTTP 404 if missing).
    - Sanitized display labels (never leaking email, passwords, or private claims).
    """
    norm_report_id = validate_report_uuid(report_id)

    cfg = app_settings or settings
    try:
        client = create_authenticated_supabase_client(access_token, cfg)
    except SupabaseConfigError as exc:
        raise CommunityServiceError(
            "Comments service is temporarily unavailable.",
            status_code=503,
        ) from exc

    # 1. Verify parent report exists
    try:
        report_res = (
            client.table("hazard_reports")
            .select("id")
            .eq("id", norm_report_id)
            .maybe_single()
            .execute()
        )
        report_raw = _extract_data(report_res)
        report_data = (
            report_raw[0]
            if isinstance(report_raw, list) and report_raw
            else (report_raw if isinstance(report_raw, dict) else None)
        )
    except Exception as exc:
        logger.warning("Failed to verify report for comments listing: %s", type(exc).__name__)
        raise CommunityServiceError(
            "Unable to retrieve comments right now.",
            status_code=503,
        ) from exc

    if not report_data or not isinstance(report_data, dict):
        raise CommunityServiceError("Hazard report not found.", status_code=404)

    # 2. Query comments ordered chronologically (created_at ASC)
    try:
        comments_res = (
            client.table("comments")
            .select("id, report_id, user_id, body, created_at")
            .eq("report_id", norm_report_id)
            .order("created_at", desc=False)
            .execute()
        )
        raw_comments = _extract_data(comments_res) or []
    except Exception as exc:
        logger.warning("Failed to query report comments: %s", type(exc).__name__)
        raise CommunityServiceError(
            "Unable to load comments right now.",
            status_code=503,
        ) from exc

    # 3. Resolve user display names from public.profiles
    user_ids = list({
        str(c["user_id"])
        for c in raw_comments
        if isinstance(c, dict) and c.get("user_id")
    })

    names_by_uid: dict[str, str] = {}
    if user_ids:
        try:
            profiles_res = (
                client.table("profiles")
                .select("id, full_name")
                .in_("id", user_ids)
                .execute()
            )
            raw_profiles = _extract_data(profiles_res) or []
            for p in raw_profiles:
                if isinstance(p, dict) and p.get("id"):
                    names_by_uid[str(p["id"])] = str(p.get("full_name") or "").strip()
        except Exception:
            pass

    # 4. Construct response list
    results: list[CommentResponse] = []
    for c in raw_comments:
        if not isinstance(c, dict):
            continue
        uid = str(c["user_id"]) if c.get("user_id") else None
        name = names_by_uid.get(uid) if uid else None
        author_name = name if name and name.strip() else "Community Member"
        results.append(
            CommentResponse(
                id=str(c.get("id") or ""),
                report_id=norm_report_id,
                user_id=uid if author_name != "Community Member" else None,
                author_name=author_name,
                body=str(c.get("body") or ""),
                is_anonymous=(author_name == "Community Member" or uid is None),
                created_at=str(c.get("created_at") or ""),
            )
        )

    return CommentsListResponse(
        report_id=norm_report_id,
        total=len(results),
        comments=results,
    )
