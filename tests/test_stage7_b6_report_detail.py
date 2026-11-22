"""Automated tests for Stage 7 Issue #24 (B6 · Report detail and verification page).

Verifies:
- Content negotiation on GET /reports/{id} (JSON for API, HTML for browser).
- Authentication guard and redirect on unauthenticated HTML navigation.
- Rendering of photo gallery, severity score breakdown card, voting panel,
  timeline, and comments thread.
- Self-report verification disabled notice.
- Escalated and Fixed badges when report status changes.
- Static security and design rules (light theme only, no mint on white, safe DOM text bindings).
"""

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_settings
from app.main import app
from app.services.auth import AuthenticatedUser
from app.services.reports import ReportDetailResponse, ReportImageOut

USER_REPORTER_ID = "11111111-1111-4111-8111-111111111111"
USER_VIEWER_ID = "22222222-2222-4222-8222-222222222222"
REPORT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
    )


def _auth_ctx(user_id: str = USER_VIEWER_ID, full_name: str = "Test Viewer") -> AuthenticatedContext:
    return AuthenticatedContext(
        user=AuthenticatedUser(
            id=user_id,
            email=f"{user_id[:8]}@example.com",
            full_name=full_name,
            role="user",
        ),
        access_token="mock-jwt-token",
    )


def _mock_report_detail(
    report_id: str = REPORT_ID,
    reporter_id: str = USER_REPORTER_ID,
    status: str = "open",
    category: str = "pothole",
    severity_score: int = 68,
    confirm_count: int = 1,
    dispute_count: int = 0,
    fixed_count: int = 0,
) -> ReportDetailResponse:
    now_iso = datetime.now(timezone.utc).isoformat()
    return ReportDetailResponse(
        id=report_id,
        reporter_id=reporter_id,
        category=category,
        description="Deep dangerous pothole near junction",
        lat=17.4849,
        lng=78.4138,
        address_text="Kukatpally Y Junction, Hyderabad",
        status=status,
        escalation_recommended=False,
        severity_score=severity_score,
        severity_band="medium",
        confidence="medium",
        confirm_count=confirm_count,
        dispute_count=dispute_count,
        fixed_count=fixed_count,
        is_seed=False,
        created_at=now_iso,
        updated_at=now_iso,
        escalated_at=now_iso if status == "escalated" else None,
        fixed_at=now_iso if status == "fixed" else None,
        images=[
            ReportImageOut(
                id=str(uuid.uuid4()),
                storage_path=f"{reporter_id}/{report_id}/evidence.jpg",
                kind="evidence",
                signed_url="https://test-project.supabase.co/storage/v1/signed/evidence.jpg",
                created_at=now_iso,
            )
        ],
    )


# ==============================================================================
# 1. Content Negotiation & Authentication Guards
# ==============================================================================


def test_content_negotiation_json_vs_html():
    """GET /reports/{id} returns JSON when Accept: application/json, and HTML when text/html."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VIEWER_ID)

    mock_detail = _mock_report_detail()

    with patch("app.routers.reports.get_report_by_id", return_value=mock_detail), \
         patch("app.routers.reports.list_report_comments") as mock_comments, \
         patch("app.routers.reports.create_authenticated_supabase_client"):
        mock_comments.return_value = MagicMock(comments=[])
        try:
            # 1. JSON Request (e.g. from B4 map popup)
            r_json = client.get(
                f"/reports/{REPORT_ID}",
                headers={"Accept": "application/json"},
            )
            assert r_json.status_code == 200
            assert r_json.headers["content-type"].startswith("application/json")
            data = r_json.json()
            assert data["id"] == REPORT_ID
            assert data["category"] == "pothole"
            assert data["severity_score"] == 68

            # 2. Browser HTML Request
            r_html = client.get(
                f"/reports/{REPORT_ID}",
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            assert r_html.status_code == 200
            assert "text/html" in r_html.headers["content-type"]
            html = r_html.text
            assert "<!DOCTYPE html>" in html
            assert "Severity Score Calculation" in html
            assert "Community Verification" in html
            assert "Kukatpally Y Junction" in html
        finally:
            app.dependency_overrides.clear()


def test_unauthenticated_browser_request_redirects_to_login():
    """Unauthenticated browser request to /reports/{id} must redirect 303 to /login."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    try:
        r = client.get(
            f"/reports/{REPORT_ID}",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/login" in r.headers["location"]
    finally:
        app.dependency_overrides.clear()


# ==============================================================================
# 2. Report Detail Template Components
# ==============================================================================


def test_report_detail_template_renders_all_spec_components():
    """Template renders evidence photo, severity breakdown formula, voting buttons, and comments."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VIEWER_ID)

    mock_detail = _mock_report_detail()
    mock_comment = MagicMock(
        id=str(uuid.uuid4()),
        report_id=REPORT_ID,
        user_id=USER_VIEWER_ID,
        author_name="Alice Citizen",
        body="Pothole has deepened after rain.",
        is_anonymous=False,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    with patch("app.routers.reports.get_report_by_id", return_value=mock_detail), \
         patch("app.routers.reports.list_report_comments") as mock_comments, \
         patch("app.routers.reports.create_authenticated_supabase_client"):
        mock_comments.return_value = MagicMock(comments=[mock_comment])
        try:
            r = client.get(
                f"/reports/{REPORT_ID}",
                headers={"Accept": "text/html"},
            )
            assert r.status_code == 200
            html = r.text

            # Header & metadata
            assert "Pothole" in html
            assert "Kukatpally Y Junction, Hyderabad" in html
            assert "Back to map" in html
            assert 'href="/map"' in html

            # Evidence Photo Gallery
            assert "Photographic Evidence" in html
            assert "https://test-project.supabase.co/storage/v1/signed/evidence.jpg" in html

            # Severity Breakdown Card
            assert "Category Base Weight" in html
            assert "+40" in html
            assert "Photographic Evidence" in html
            assert "Community Confirmations" in html
            assert "Final Clamped Score (0&ndash;100)" in html

            # Community Verification Panel
            assert "Confirm &ndash; Still Here" in html
            assert "Dispute &ndash; Not A Hazard" in html
            assert "Mark Fixed" in html
            assert "Optional After-Repair Photo" in html

            # Status Timeline
            assert "Status Timeline" in html
            assert "Hazard Reported" in html

            # Comments Thread
            assert "Community Discussion" in html
            assert "Pothole has deepened after rain." in html
            assert "Alice Citizen" in html
            assert "Hide my name" in html
            assert "Post Comment" in html
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 3. Self-Report & Badge States
# ==============================================================================


def test_self_report_disables_voting_with_notice():
    """When the authenticated user is the reporter, voting buttons are disabled with clear notice."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    # Viewer is USER_REPORTER_ID
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_REPORTER_ID)

    mock_detail = _mock_report_detail(reporter_id=USER_REPORTER_ID)

    with patch("app.routers.reports.get_report_by_id", return_value=mock_detail), \
         patch("app.routers.reports.list_report_comments", return_value=MagicMock(comments=[])), \
         patch("app.routers.reports.create_authenticated_supabase_client"):
        try:
            r = client.get(
                f"/reports/{REPORT_ID}",
                headers={"Accept": "text/html"},
            )
            assert r.status_code == 200
            html = r.text
            assert "You submitted this report." in html
            assert "Peer verification is reserved for other citizens" in html
        finally:
            app.dependency_overrides.clear()


def test_escalated_and_fixed_badges():
    """Escalated reports display administrative notice; fixed reports display fixed indicator."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VIEWER_ID)

    # 1. Escalated Report
    mock_esc = _mock_report_detail(status="escalated")
    with patch("app.routers.reports.get_report_by_id", return_value=mock_esc), \
         patch("app.routers.reports.list_report_comments", return_value=MagicMock(comments=[])), \
         patch("app.routers.reports.create_authenticated_supabase_client"):
        try:
            r_esc = client.get(f"/reports/{REPORT_ID}", headers={"Accept": "text/html"})
            assert r_esc.status_code == 200
            assert "Escalated to higher authority" in r_esc.text
        finally:
            app.dependency_overrides.clear()

    # 2. Fixed Report
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VIEWER_ID)
    mock_fixed = _mock_report_detail(status="fixed")
    with patch("app.routers.reports.get_report_by_id", return_value=mock_fixed), \
         patch("app.routers.reports.list_report_comments", return_value=MagicMock(comments=[])), \
         patch("app.routers.reports.create_authenticated_supabase_client"):
        try:
            r_fixed = client.get(f"/reports/{REPORT_ID}", headers={"Accept": "text/html"})
            assert r_fixed.status_code == 200
            assert "Fixed" in r_fixed.text
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 4. Static Security & Design System Guardrails
# ==============================================================================


def test_static_security_and_design_rules():
    """Enforce light theme only, zero mint on light surfaces, safe DOM text bindings, and CSRF."""
    root = Path(__file__).resolve().parent.parent
    template_path = root / "app" / "templates" / "report_detail.html"
    js_path = root / "app" / "static" / "js" / "report_detail.js"
    css_path = root / "app" / "static" / "css" / "saferoute.css"

    template_content = template_path.read_text(encoding="utf-8")
    js_content = js_path.read_text(encoding="utf-8")
    css_content = css_path.read_text(encoding="utf-8")

    # 1. No dark: classes in template
    dark_matches = re.findall(r"\bdark:[a-zA-Z0-9_\-]+", template_content)
    assert not dark_matches, f"Forbidden dark: classes found in report_detail.html: {dark_matches}"

    # 2. No raw text-accent on white in template
    accent_text_matches = re.findall(r"\btext-accent\b", template_content)
    assert not accent_text_matches, "Forbidden text-accent found in report_detail.html"

    # 3. No raw innerHTML in report_detail.js (ensures safe DOM text bindings)
    assert ".innerHTML" not in js_content, "Dangerous .innerHTML usage found in report_detail.js"

    # 4. Double-submit CSRF token is wired
    assert "X-CSRF-Token" in js_content
    assert "csrfToken" in js_content
    assert "sr-report-detail-config" in template_content
