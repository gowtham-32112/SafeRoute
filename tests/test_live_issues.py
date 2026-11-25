"""Unit and integration tests for Commuter Live Issues page and actions."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_settings, require_admin_context, require_csrf
from app.main import app
from app.services.auth import AuthenticatedUser
from app.services.live_issues import LiveIssueCard

ADMIN_USER = AuthenticatedUser(
    id="admin-0000-0000-0000-000000000001",
    email="admin@saferoute.demo",
    full_name="Municipal Chief Admin",
    role="admin",
)

COMMUTER_USER = AuthenticatedUser(
    id="commuter-0000-0000-0000-000000000002",
    email="commuter@saferoute.demo",
    full_name="Daily Commuter",
    role="user",
)


def _test_settings() -> Settings:
    return Settings(
        SUPABASE_URL="https://test.supabase.co",
        SUPABASE_ANON_KEY="dummy-anon-key",
        SUPABASE_SERVICE_KEY="dummy-service-key",
    )


def test_live_issues_requires_auth():
    client = TestClient(app)
    r = client.get("/live-issues", follow_redirects=False)
    assert r.status_code == 401


def test_live_issues_page_renders_for_commuter():
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: AuthenticatedContext(
        user=COMMUTER_USER, access_token="commuter-jwt"
    )

    sample_issue = LiveIssueCard(
        id="11111111-1111-4111-8111-111111111111",
        category="pothole",
        description="Large pothole near JNTU gate",
        address_text="JNTU Main Road",
        location_label="17.4950, 78.3900",
        image_url=None,
        status="open",
        severity_score=65,
        severity_band="medium",
        confirm_count=3,
        dispute_count=0,
        created_at="2026-11-24T10:00:00Z",
        updated_at="2026-11-24T10:00:00Z",
        reporter_id="other-user",
        is_own_report=False,
        my_vote=None,
    )

    with patch("app.routers.live_issues.list_live_issues", return_value=([sample_issue], False)):
        try:
            r = client.get("/live-issues")
            assert r.status_code == 200
            assert "text/html" in r.headers["content-type"]
            html = r.text
            assert "Live Issues" in html
            assert "Large pothole near JNTU gate" in html
            assert "Looks real" in html
            assert "Looks false" in html
        finally:
            app.dependency_overrides.clear()


def test_live_issues_commuter_vote():
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: AuthenticatedContext(
        user=COMMUTER_USER, access_token="commuter-jwt"
    )
    app.dependency_overrides[require_csrf] = lambda: None

    with patch("app.routers.live_issues.cast_hazard_vote") as mock_vote:
        try:
            r = client.post(
                "/live-issues/11111111-1111-4111-8111-111111111111/vote",
                data={"vote": "confirm"},
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert "notice=vote-recorded" in r.headers["location"]
            mock_vote.assert_called_once()
        finally:
            app.dependency_overrides.clear()


def test_live_issues_admin_actions_restricted_for_commuters():
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: AuthenticatedContext(
        user=COMMUTER_USER, access_token="commuter-jwt"
    )
    app.dependency_overrides[require_csrf] = lambda: None

    # Commuter should be rejected with 403 on admin-update
    r_update = client.post(
        "/live-issues/11111111-1111-4111-8111-111111111111/admin-update",
        data={"category": "pothole", "description": "Edited", "status": "open"},
    )
    assert r_update.status_code == 403

    # Commuter should be rejected with 403 on admin-delete
    r_del = client.post(
        "/live-issues/11111111-1111-4111-8111-111111111111/admin-delete",
        data={"return_status": "open"},
    )
    assert r_del.status_code == 403

    app.dependency_overrides.clear()


def test_live_issues_admin_actions_allowed_for_admin():
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = lambda: AuthenticatedContext(
        user=ADMIN_USER, access_token="admin-jwt"
    )
    app.dependency_overrides[require_csrf] = lambda: None

    with patch("app.routers.live_issues.update_live_issue") as mock_update, patch(
        "app.routers.live_issues.delete_live_issue"
    ) as mock_delete:
        try:
            r_update = client.post(
                "/live-issues/11111111-1111-4111-8111-111111111111/admin-update",
                data={
                    "category": "pothole",
                    "description": "Repaired road section",
                    "status": "fixed",
                    "return_status": "open",
                },
                follow_redirects=False,
            )
            assert r_update.status_code == 303
            assert "notice=issue-updated" in r_update.headers["location"]
            mock_update.assert_called_once()

            r_del = client.post(
                "/live-issues/11111111-1111-4111-8111-111111111111/admin-delete",
                data={"return_status": "open"},
                follow_redirects=False,
            )
            assert r_del.status_code == 303
            assert "notice=issue-deleted" in r_del.headers["location"]
            mock_delete.assert_called_once()
        finally:
            app.dependency_overrides.clear()
