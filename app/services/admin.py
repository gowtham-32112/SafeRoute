"""SafeRoute Stage 7 Admin Service (Issue #26).

Implements:
- Admin candidate query for hazard reports recommended for escalation.
- Recording of escalations in public.escalations with authority name and audit note.
- Updating hazard_reports status to 'escalated' under authenticated admin user JWT/RLS.
- Zero usage of administrative service-role secret in runtime application code.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Literal, Optional
import uuid

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field

from app.config import Settings
from app.services.reports import parse_postgis_point, severity_band
from app.services.supabase_client import create_authenticated_supabase_client


class AdminServiceError(Exception):
    """Base exception for admin escalation operations."""

    def __init__(self, user_message: str, status_code: int = 400) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.status_code = status_code


def validate_report_uuid(report_id: Any) -> str:
    """Validate report UUID format, raising HTTP 400 on malformed input."""
    try:
        parsed = uuid.UUID(str(report_id).strip())
        return str(parsed)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AdminServiceError("Invalid report ID format.", status_code=400) from exc


def _extract_data(resp: Any) -> Any:
    """Extract rows from a Supabase PostgREST response."""
    if isinstance(resp, dict):
        return resp.get("data")
    return getattr(resp, "data", None)


# ==============================================================================
# Pydantic Schemas
# ==============================================================================


class EscalationCandidateReport(BaseModel):
    """An open hazard report flagged for escalation to road authorities."""

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
    escalated_at: Optional[str] = None
    has_evidence_photo: bool = False
    flag_reasons: list[str] = []


class EscalatedReportItem(BaseModel):
    """A hazard report that has been escalated to a higher authority."""

    id: str
    report_id: str
    category: str
    description: str
    address_text: Optional[str] = None
    severity_score: int
    severity_band: Literal["low", "medium", "high"]
    authority_name: str
    note: str
    escalated_by: Optional[str] = None
    escalated_at: str


class EscalationsDashboardResponse(BaseModel):
    """Complete escalation dashboard payload for admin review."""

    pending_count: int
    high_severity_count: int
    total_escalated_count: int
    candidates: list[EscalationCandidateReport] = []
    recent_escalations: list[EscalatedReportItem] = []


class EscalateReportRequest(BaseModel):
    """Input payload for marking a hazard as escalated."""

    authority_name: str = Field(..., min_length=1, max_length=150)
    note: str = Field(..., min_length=1, max_length=1000)


class EscalateReportResponse(BaseModel):
    """Authoritative response after recording an escalation."""

    escalation_id: str
    report_id: str
    status: Literal["escalated"] = "escalated"
    authority_name: str
    note: str
    escalated_at: str
    escalated_by: str


# ==============================================================================
# Service Operations
# ==============================================================================


def get_admin_escalations_dashboard(
    *,
    access_token: str,
    app_settings: Settings,
) -> EscalationsDashboardResponse:
    """Fetch all open escalation candidate reports and recent escalations.

    Executes under the admin user's JWT via PostgREST RLS.
    """
    client = create_authenticated_supabase_client(access_token, app_settings)

    # 1. Query candidate reports (status='open')
    raw_reports: list[dict[str, Any]] = []
    try:
        resp = (
            client.table("hazard_reports")
            .select(
                "id, reporter_id, category, description, location, address_text, status, "
                "escalation_recommended, severity_score, confidence, confirm_count, dispute_count, "
                "fixed_count, created_at, escalated_at, report_images(id, kind, signed_url)"
            )
            .eq("status", "open")
            .order("severity_score", desc=True)
            .order("created_at", desc=True)
            .execute()
        )
        extracted = _extract_data(resp)
        if isinstance(extracted, list):
            raw_reports = extracted
    except Exception as exc:
        logger.warning(f"Failed to fetch candidate reports: {exc}")
        raw_reports = []

    candidates: list[EscalationCandidateReport] = []
    high_sev_count = 0

    for row in raw_reports:
        sev = int(row.get("severity_score") or 0)
        conf_count = int(row.get("confirm_count") or 0)
        is_rec = bool(row.get("escalation_recommended"))

        flag_reasons: list[str] = []
        if sev >= 70:
            flag_reasons.append("High Severity (\u2265 70)")
            high_sev_count += 1
        if conf_count >= 5:
            flag_reasons.append("5+ Community Confirmations")

        # Keep report if escalation_recommended is true OR criteria met
        if not is_rec and not flag_reasons:
            continue

        raw_loc = row.get("location")
        lat, lng = (app_settings.DEFAULT_MAP_LAT, app_settings.DEFAULT_MAP_LNG)
        if raw_loc is not None:
            try:
                lat, lng = parse_postgis_point(raw_loc)
            except Exception:
                lat, lng = (app_settings.DEFAULT_MAP_LAT, app_settings.DEFAULT_MAP_LNG)

        band = severity_band(sev)
        images = row.get("report_images") or []
        has_photo = bool(images and isinstance(images, list) and len(images) > 0)

        candidates.append(
            EscalationCandidateReport(
                id=str(row["id"]),
                reporter_id=str(row.get("reporter_id")) if row.get("reporter_id") else None,
                category=str(row.get("category") or "other"),
                description=str(row.get("description") or ""),
                lat=lat,
                lng=lng,
                address_text=row.get("address_text"),
                status=str(row.get("status") or "open"),
                escalation_recommended=True,
                severity_score=sev,
                severity_band=band,
                confidence=str(row.get("confidence") or "medium"),
                confirm_count=conf_count,
                dispute_count=int(row.get("dispute_count") or 0),
                fixed_count=int(row.get("fixed_count") or 0),
                created_at=str(row.get("created_at") or ""),
                escalated_at=str(row.get("escalated_at")) if row.get("escalated_at") else None,
                has_evidence_photo=has_photo,
                flag_reasons=flag_reasons or ["Flagged for Escalation"],
            )
        )

    # 2. Query recent escalations from public.escalations
    recent_escalations: list[EscalatedReportItem] = []
    total_escalated = 0
    try:
        esc_resp = (
            client.table("escalations")
            .select("id, report_id, authority_name, note, escalated_by, created_at")
            .order("created_at", desc=True)
            .limit(25)
            .execute()
        )
        raw_esc = _extract_data(esc_resp) or []
        if isinstance(raw_esc, list):
            total_escalated = len(raw_esc)
            report_ids = [e["report_id"] for e in raw_esc if e.get("report_id")]
            report_map: dict[str, Any] = {}
            if report_ids:
                rep_resp = (
                    client.table("hazard_reports")
                    .select("id, category, description, address_text, severity_score, status")
                    .in_("id", report_ids)
                    .execute()
                )
                reps = _extract_data(rep_resp) or []
                if isinstance(reps, list):
                    report_map = {r["id"]: r for r in reps if r.get("id")}

            for e in raw_esc:
                r_info = report_map.get(e.get("report_id"), {})
                sev = int(r_info.get("severity_score") or 0)
                recent_escalations.append(
                    EscalatedReportItem(
                        id=str(e["id"]),
                        report_id=str(e["report_id"]),
                        category=str(r_info.get("category") or "other"),
                        description=str(r_info.get("description") or ""),
                        address_text=r_info.get("address_text"),
                        severity_score=sev,
                        severity_band=severity_band(sev),
                        authority_name=str(e.get("authority_name") or ""),
                        note=str(e.get("note") or ""),
                        escalated_by=str(e.get("escalated_by")) if e.get("escalated_by") else None,
                        escalated_at=str(e.get("created_at") or ""),
                    )
                )
    except Exception:
        pass

    return EscalationsDashboardResponse(
        pending_count=len(candidates),
        high_severity_count=high_sev_count,
        total_escalated_count=total_escalated,
        candidates=candidates,
        recent_escalations=recent_escalations,
    )


def escalate_hazard_report(
    *,
    report_id: str,
    authority_name: str,
    note: str,
    admin_id: str,
    access_token: str,
    app_settings: Settings,
) -> EscalateReportResponse:
    """Escalate a hazard report to higher municipal authorities.

    Inserts an audit record into public.escalations and sets hazard_reports.status='escalated'.
    Enforces UUID format, report existence, and rejects already fixed or escalated reports.
    """
    clean_id = validate_report_uuid(report_id)

    clean_authority = (authority_name or "").strip()
    if not clean_authority or len(clean_authority) > 150:
        raise AdminServiceError(
            "Authority name must be between 1 and 150 characters.",
            status_code=400,
        )

    clean_note = (note or "").strip()
    if not clean_note or len(clean_note) > 1000:
        raise AdminServiceError(
            "Escalation note must be between 1 and 1000 characters.",
            status_code=400,
        )

    client = create_authenticated_supabase_client(access_token, app_settings)

    # 1. Fetch current report status
    resp = (
        client.table("hazard_reports")
        .select("id, status, escalation_recommended")
        .eq("id", clean_id)
        .execute()
    )
    rows = _extract_data(resp)
    if not rows or not isinstance(rows, list) or len(rows) == 0:
        raise AdminServiceError("Hazard report not found.", status_code=404)

    report_row = rows[0]
    cur_status = str(report_row.get("status") or "open").lower()
    if cur_status == "fixed":
        raise AdminServiceError(
            "Cannot escalate a report that has already been resolved / fixed.",
            status_code=400,
        )
    if cur_status == "escalated":
        raise AdminServiceError(
            "This hazard report has already been escalated.",
            status_code=409,
        )

    # 2. Insert into public.escalations
    now_iso = datetime.now(timezone.utc).isoformat()
    esc_insert = (
        client.table("escalations")
        .insert(
            {
                "report_id": clean_id,
                "escalated_by": admin_id,
                "authority_name": clean_authority,
                "note": clean_note,
                "created_at": now_iso,
            }
        )
        .execute()
    )
    esc_rows = _extract_data(esc_insert)
    esc_id = (
        esc_rows[0].get("id")
        if (esc_rows and isinstance(esc_rows, list) and len(esc_rows) > 0)
        else str(uuid.uuid4())
    )

    # 3. Update public.hazard_reports status to 'escalated'
    client.table("hazard_reports").update(
        {
            "status": "escalated",
            "escalated_at": now_iso,
        }
    ).eq("id", clean_id).execute()

    return EscalateReportResponse(
        escalation_id=str(esc_id),
        report_id=clean_id,
        status="escalated",
        authority_name=clean_authority,
        note=clean_note,
        escalated_at=now_iso,
        escalated_by=admin_id,
    )
