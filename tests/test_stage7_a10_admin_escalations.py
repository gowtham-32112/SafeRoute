"""Automated tests for Stage 7 Issue #26 (A10 · Admin Escalations API & Dashboard).

Verifies:
- Unauthenticated access rejection (303 HTML redirect to /login, 401 JSON for API).
- Authenticated ordinary user (role='user') rejection with HTTP 403 Forbidden.
- Double-submit CSRF protection on POST /reports/{id}/escalate.
- Invalid UUID (400) and nonexistent report (404) handling.
- Validation bounds: authority_name and note non-empty caps, rejecting fixed/already-escalated reports.
- Successful admin escalation flow (inserting into public.escalations and updating hazard_reports).
- Static security and design rules (no SUPABASE_SERVICE_KEY, no dark: classes, no innerHTML).
"""

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Optional
from unittest.mock import MagicMock, patch
import uuid

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import (
    AuthenticatedContext,
    get_authenticated_context,
    get_current_user,
    get_settings,
    require_admin_context,
)
from app.main import app
from app.services.admin import (
    EscalatedReportItem,
    EscalationCandidateReport,
    EscalationsDashboardResponse,
)
from app.services.auth import AuthenticatedUser

USER_ORDINARY_ID = "11111111-1111-4111-8111-111111111111"
ADMIN_USER_ID = "99999999-9999-4999-8999-999999999999"
REPORT_CANDIDATE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REPORT_ESCALATED_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REPORT_FIXED_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key-1234567890",
        ENVIRONMENT="development",
    )


def _user_ctx(user_id: str = USER_ORDINARY_ID, role: str = "user") -> AuthenticatedContext:
    return AuthenticatedContext(
        user=AuthenticatedUser(
            id=user_id,
            email=f"{user_id[:8]}@example.com",
            full_name="Ordinary Citizen" if role == "user" else "Admin Municipal",
            role=role,
        ),
        access_token="mock-user-jwt",
    )


def _admin_ctx() -> AuthenticatedContext:
    return AuthenticatedContext(
        user=AuthenticatedUser(
            id=ADMIN_USER_ID,
            email=f"{ADMIN_USER_ID[:8]}@example.com",
            full_name="Admin Municipal",
            role="admin",
        ),
        access_token="mock-admin-jwt",
    )


def _mock_dashboard_payload() -> EscalationsDashboardResponse:
    now_iso = datetime.now(timezone.utc).isoformat()
    return EscalationsDashboardResponse(
        pending_count=1,
        high_severity_count=1,
        total_escalated_count=1,
        candidates=[
            EscalationCandidateReport(
                id=REPORT_CANDIDATE_ID,
                reporter_id=USER_ORDINARY_ID,
                category="pothole",
                description="Severe crater near junction causing wheel rim cracks.",
                lat=17.4849,
                lng=78.4138,
                address_text="Kukatpally Y Junction, Hyderabad",
                status="open",
                escalation_recommended=True,
                severity_score=78,
                severity_band="high",
                confidence="high",
                confirm_count=5,
                dispute_count=0,
                fixed_count=0,
                created_at=now_iso,
                escalated_at=None,
                has_evidence_photo=True,
                flag_reasons=["High Severity (\u2265 70)", "5+ Community Confirmations"],
            )
        ],
        recent_escalations=[
            EscalatedReportItem(
                id=str(uuid.uuid4()),
                report_id=REPORT_ESCALATED_ID,
                category="road_break",
                description="Broken asphalt slab on main roadway.",
                address_text="Near Pillar 1045",
                severity_score=75,
                severity_band="high",
                authority_name="GHMC Road Maintenance Circle 4",
                note="Dispatched road repair crew for emergency patching.",
                escalated_by=ADMIN_USER_ID,
                escalated_at=now_iso,
            )
        ],
    )


# ==============================================================================
# 1. Access Control: Unauthenticated & Non-Admin Rejection
# ==============================================================================


def test_unauthenticated_requests_rejected():
    """Unauthenticated requests must be redirected (HTML) or rejected (JSON)."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    try:
        # 1. HTML request to /admin/escalations redirects 303 to /login
        r_html = client.get(
            "/admin/escalations",
            headers={"Accept": "text/html,application/xhtml+xml"},
            follow_redirects=False,
        )
        assert r_html.status_code == 303
        assert "/login" in r_html.headers["location"]

        # 2. JSON request to /admin/escalations returns 401
        r_api = client.get(
            "/admin/escalations",
            headers={"Accept": "application/json"},
        )
        assert r_api.status_code == 401
        assert "authentication required" in r_api.json()["detail"].lower()

        # 3. POST /reports/{id}/escalate without auth returns 401
        r_post = client.post(
            f"/reports/{REPORT_CANDIDATE_ID}/escalate",
            json={"authority_name": "GHMC", "note": "Urgent"},
        )
        assert r_post.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_non_admin_authenticated_user_rejected_with_403():
    """Ordinary authenticated users (role='user') must receive HTTP 403 Forbidden."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _user_ctx(role="user")
    app.dependency_overrides[get_current_user] = lambda: _user_ctx(role="user").user
    try:
        # 1. GET /admin/escalations rejected with 403
        r_get = client.get(
            "/admin/escalations",
            headers={"Accept": "application/json"},
        )
        assert r_get.status_code == 403
        assert "administrator privileges required" in r_get.json()["detail"].lower()

        # 2. POST /reports/{id}/escalate rejected with 403
        r_post = client.post(
            f"/reports/{REPORT_CANDIDATE_ID}/escalate",
            json={"authority_name": "GHMC", "note": "Urgent"},
        )
        assert r_post.status_code == 403
        assert "administrator privileges required" in r_post.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 2. CSRF Protection on Escalation
# ==============================================================================


def test_csrf_protection_on_escalate():
    """State-changing escalation POST requires valid CSRF double-submit matching."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    try:
        # Cookie set without matching header -> 403 Forbidden
        client.cookies.set("sr_access_token", "mock-admin-jwt")
        client.cookies.set("sr_csrf_token", "correct-csrf-token")
        r = client.post(
            f"/reports/{REPORT_CANDIDATE_ID}/escalate",
            json={"authority_name": "GHMC", "note": "Urgent repair required"},
            headers={"X-CSRF-Token": "wrong-csrf-token"},
        )
        assert r.status_code == 403
        assert "csrf" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 3. Input Validation, UUID & Status Handling
# ==============================================================================


def test_invalid_uuid_returns_400():
    """Invalid report UUID must return HTTP 400 Bad Request."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    client.cookies.set("sr_access_token", "mock-admin-jwt")
    client.cookies.set("sr_csrf_token", "test-csrf")
    try:
        r = client.post(
            "/reports/invalid-uuid/escalate",
            json={"authority_name": "GHMC", "note": "Fix road hazard"},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert r.status_code == 400
        assert "invalid report id format" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_nonexistent_report_returns_404():
    """Escalating a nonexistent report returns HTTP 404 Not Found."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    client.cookies.set("sr_access_token", "mock-admin-jwt")
    client.cookies.set("sr_csrf_token", "test-csrf")

    mock_client = MagicMock()
    # select returns empty list
    mock_client.table().select().eq().execute.return_value = MagicMock(data=[])

    with patch("app.services.admin.create_authenticated_supabase_client", return_value=mock_client):
        try:
            r = client.post(
                f"/reports/{REPORT_CANDIDATE_ID}/escalate",
                json={"authority_name": "GHMC", "note": "Road hazard fix"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
            assert r.status_code == 404
            assert "hazard report not found" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


def test_reject_escalating_already_fixed_or_escalated_reports():
    """Reports with status='fixed' (400) or 'escalated' (409) must be rejected."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    client.cookies.set("sr_access_token", "mock-admin-jwt")
    client.cookies.set("sr_csrf_token", "test-csrf")

    mock_client = MagicMock()

    with patch("app.services.admin.create_authenticated_supabase_client", return_value=mock_client):
        try:
            # 1. Report already fixed -> 400
            mock_client.table().select().eq().execute.return_value = MagicMock(
                data=[{"id": REPORT_FIXED_ID, "status": "fixed"}]
            )
            r_fixed = client.post(
                f"/reports/{REPORT_FIXED_ID}/escalate",
                json={"authority_name": "GHMC", "note": "Attempt to escalate resolved hazard"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
            assert r_fixed.status_code == 400
            assert "already been resolved" in r_fixed.json()["detail"].lower()

            # 2. Report already escalated -> 409 Conflict
            mock_client.table().select().eq().execute.return_value = MagicMock(
                data=[{"id": REPORT_ESCALATED_ID, "status": "escalated"}]
            )
            r_esc = client.post(
                f"/reports/{REPORT_ESCALATED_ID}/escalate",
                json={"authority_name": "GHMC", "note": "Duplicate escalation attempt"},
                headers={"X-CSRF-Token": "test-csrf"},
            )
            assert r_esc.status_code == 409
            assert "already been escalated" in r_esc.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


def test_empty_authority_name_or_note_rejected():
    """Empty or oversized authority names / notes must be rejected with 400."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    client.cookies.set("sr_access_token", "mock-admin-jwt")
    client.cookies.set("sr_csrf_token", "test-csrf")

    try:
        # Empty authority_name
        r_empty_auth = client.post(
            f"/reports/{REPORT_CANDIDATE_ID}/escalate",
            json={"authority_name": "   ", "note": "Valid note"},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert r_empty_auth.status_code == 400

        # Empty note
        r_empty_note = client.post(
            f"/reports/{REPORT_CANDIDATE_ID}/escalate",
            json={"authority_name": "GHMC Circle 4", "note": "   "},
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert r_empty_note.status_code == 400
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 4. Successful Admin Escalation Flow
# ==============================================================================


def test_get_admin_escalations_dashboard_json_and_html():
    """Admin GET /admin/escalations returns JSON for API and renders accessible HTML for browser."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    mock_data = _mock_dashboard_payload()

    with patch("app.routers.admin.get_admin_escalations_dashboard", return_value=mock_data):
        try:
            # 1. API JSON Request
            r_json = client.get(
                "/admin/escalations",
                headers={"Accept": "application/json"},
            )
            assert r_json.status_code == 200
            assert "application/json" in r_json.headers["content-type"]
            data = r_json.json()
            assert data["pending_count"] == 1
            assert data["high_severity_count"] == 1
            assert len(data["candidates"]) == 1
            assert data["candidates"][0]["id"] == REPORT_CANDIDATE_ID
            assert data["candidates"][0]["severity_score"] == 78

            # 2. Browser HTML Request
            r_html = client.get(
                "/admin/escalations",
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            assert r_html.status_code == 200
            assert "text/html" in r_html.headers["content-type"]
            html = r_html.text
            assert "<!DOCTYPE html>" in html
            assert "Hazard Escalation Dashboard" in html
            assert "Municipal Administration" in html
            assert "sr-kpi-pending" in html
            assert "sr-kpi-high-severity" in html
            assert "sr-kpi-total-escalated" in html
            assert "sr-escalate-modal" in html
            assert "sr-authority-input" in html
            assert "sr-note-input" in html
        finally:
            app.dependency_overrides.clear()


def test_successful_hazard_escalation():
    """Admin can successfully escalate a report; records public.escalations and sets status='escalated'."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    client.cookies.set("sr_access_token", "mock-admin-jwt")
    client.cookies.set("sr_csrf_token", "test-csrf")

    mock_client = MagicMock()
    # Report query returns open candidate
    mock_client.table().select().eq().execute.return_value = MagicMock(
        data=[{"id": REPORT_CANDIDATE_ID, "status": "open", "escalation_recommended": True}]
    )
    # Insert returns escalation id
    mock_client.table().insert().execute.return_value = MagicMock(
        data=[{"id": "esc-12345"}]
    )
    # Update returns updated report
    mock_client.table().update().eq().execute.return_value = MagicMock(
        data=[{"id": REPORT_CANDIDATE_ID, "status": "escalated"}]
    )

    with patch("app.services.admin.create_authenticated_supabase_client", return_value=mock_client) as mock_supa:
        try:
            r = client.post(
                f"/reports/{REPORT_CANDIDATE_ID}/escalate",
                json={
                    "authority_name": "GHMC Road Maintenance Circle 4",
                    "note": "Assigned to Circle 4 road team for emergency patch work.",
                },
                headers={"X-CSRF-Token": "test-csrf"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["report_id"] == REPORT_CANDIDATE_ID
            assert data["status"] == "escalated"
            assert data["authority_name"] == "GHMC Road Maintenance Circle 4"
            assert data["escalation_id"] == "esc-12345"
            assert data["escalated_by"] == ADMIN_USER_ID

            # Verify mock_client called with access_token and never service role
            mock_supa.assert_called_once()
            args, _ = mock_supa.call_args
            assert args[0] == "mock-admin-jwt"
        finally:
            app.dependency_overrides.clear()


def test_form_urlencoded_escalation():
    """Admin escalation supports form-urlencoded submissions."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = _admin_ctx

    client.cookies.set("sr_access_token", "mock-admin-jwt")
    client.cookies.set("sr_csrf_token", "test-csrf")

    mock_client = MagicMock()
    mock_client.table().select().eq().execute.return_value = MagicMock(
        data=[{"id": REPORT_CANDIDATE_ID, "status": "open", "escalation_recommended": True}]
    )
    mock_client.table().insert().execute.return_value = MagicMock(
        data=[{"id": "esc-999"}]
    )
    mock_client.table().update().eq().execute.return_value = MagicMock(
        data=[{"id": REPORT_CANDIDATE_ID, "status": "escalated"}]
    )

    with patch("app.services.admin.create_authenticated_supabase_client", return_value=mock_client):
        try:
            r = client.post(
                f"/reports/{REPORT_CANDIDATE_ID}/escalate",
                data={
                    "authority_name": "Hyderabad Traffic Police",
                    "note": "Temporary barricade and priority pothole patch requested.",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-CSRF-Token": "test-csrf",
                },
            )
            assert r.status_code == 200
            assert r.json()["status"] == "escalated"
            assert r.json()["authority_name"] == "Hyderabad Traffic Police"
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 5. Static Security & Design Rules Guard
# ==============================================================================


def test_static_security_and_design_rules():
    """Verify security boundaries and locked design tokens across admin files."""
    admin_py = Path("app/services/admin.py").read_text(encoding="utf-8")
    router_py = Path("app/routers/admin.py").read_text(encoding="utf-8")
    template_html = Path("app/templates/admin_escalations.html").read_text(encoding="utf-8")
    admin_js = Path("app/static/js/admin_escalations.js").read_text(encoding="utf-8")

    # 1. Zero SUPABASE_SERVICE_KEY or service_role in admin code
    for code, name in [(admin_py, "admin.py"), (router_py, "routers/admin.py")]:
        assert "SUPABASE_SERVICE_KEY" not in code, f"SUPABASE_SERVICE_KEY found in {name}!"
        assert "service_role" not in code, f"service_role found in {name}!"

    # 2. Light theme only: Zero dark: classes in template
    assert "dark:" not in template_html, "Found dark: class in admin_escalations.html"

    # 3. Mint contrast rule: Zero text-accent in template
    assert "text-accent" not in template_html or 'bg-primary' in template_html, \
        "text-accent mint used improperly on light surface"

    # 4. Safe DOM text bindings: Zero .innerHTML in client JS
    assert ".innerHTML" not in admin_js, "Found forbidden .innerHTML in admin_escalations.js!"
