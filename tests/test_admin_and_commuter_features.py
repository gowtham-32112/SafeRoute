"""Tests for expanded Admin Resolution, Commuter Role Management, Civic Broadcast, and AI Bot."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_settings, require_admin_context, require_csrf
from app.main import app
from app.services.auth import AuthenticatedUser
from app.services.ai_assistant import answer_commuter_query
from app.services.broadcast import clear_broadcast, get_active_broadcast, set_broadcast

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
        GEMINI_API_KEY="",
    )


@pytest.fixture(autouse=True)
def cleanup_broadcast():
    clear_broadcast()
    yield
    clear_broadcast()


def test_broadcast_service_and_api():
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = lambda: AuthenticatedContext(
        user=ADMIN_USER, access_token="test-jwt"
    )
    app.dependency_overrides[require_csrf] = lambda: None

    try:
        # Initially empty broadcast
        r_empty = client.get("/api/broadcast")
        assert r_empty.status_code == 200
        assert r_empty.json() is None

        # Admin posts a broadcast
        r_post = client.post(
            "/admin/broadcast",
            json={
                "title": "Road Works on Flyover",
                "message": "Heavy resurfacing in progress. Exercise caution.",
                "severity": "warning",
            },
        )
        assert r_post.status_code == 200
        data = r_post.json()
        assert data["title"] == "Road Works on Flyover"
        assert data["severity"] == "warning"

        # Public user sees the broadcast
        r_public = client.get("/api/broadcast")
        assert r_public.status_code == 200
        assert r_public.json()["title"] == "Road Works on Flyover"

        # Admin dismisses broadcast
        r_del = client.delete("/admin/broadcast")
        assert r_del.status_code == 200

        # Public user sees None again
        r_cleared = client.get("/api/broadcast")
        assert r_cleared.status_code == 200
        assert r_cleared.json() is None
    finally:
        app.dependency_overrides.clear()


def test_admin_direct_resolve_report():
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[require_admin_context] = lambda: AuthenticatedContext(
        user=ADMIN_USER, access_token="test-jwt"
    )
    app.dependency_overrides[require_csrf] = lambda: None

    target_report_id = "11111111-1111-4111-8111-111111111111"

    with patch("app.services.admin.create_authenticated_supabase_client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client

        # Mock select report
        mock_select = MagicMock()
        mock_select.select.return_value.eq.return_value.execute.return_value.data = [
            {"id": target_report_id, "status": "open"}
        ]
        # Mock update report
        mock_update = MagicMock()
        mock_update.update.return_value.eq.return_value.execute.return_value.data = [
            {"id": target_report_id, "status": "fixed"}
        ]
        # Mock comments insert
        mock_comments = MagicMock()
        mock_comments.insert.return_value.execute.return_value.data = [{"id": "comment-1"}]

        def table_router(tbl):
            if tbl == "hazard_reports":
                m = MagicMock()
                m.select.return_value.eq.return_value.execute.return_value.data = [
                    {"id": target_report_id, "status": "open"}
                ]
                m.update.return_value.eq.return_value.execute.return_value.data = [
                    {"id": target_report_id, "status": "fixed"}
                ]
                return m
            elif tbl == "comments":
                return mock_comments
            return MagicMock()

        mock_client.table.side_effect = table_router

        try:
            r = client.post(
                f"/admin/reports/{target_report_id}/resolve",
                json={"resolution_note": "Asphalt patch completed by municipal crew."},
            )
            assert r.status_code == 200
            res = r.json()
            assert res["report_id"] == target_report_id
            assert res["status"] == "fixed"
            assert "Asphalt patch completed" in res["resolution_note"]
        finally:
            app.dependency_overrides.clear()


def test_commuter_civic_ai_assistant():
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: AuthenticatedContext(
        user=COMMUTER_USER, access_token="commuter-jwt"
    )

    with patch("app.services.ai_assistant.create_authenticated_supabase_client") as mock_client_factory:
        mock_client = MagicMock()
        mock_client_factory.return_value = mock_client
        mock_client.table.return_value.select.return_value.execute.return_value.data = [
            {
                "id": "1",
                "category": "pothole",
                "description": "Deep pothole near metro",
                "status": "fixed",
                "severity_score": 80,
                "confirm_count": 5,
                "dispute_count": 0,
                "fixed_count": 2,
                "created_at": "2026-11-20T10:00:00Z",
                "fixed_at": "2026-11-21T10:00:00Z",
                "is_seed": False,
            },
            {
                "id": "2",
                "category": "pothole",
                "description": "Open trench",
                "status": "open",
                "severity_score": 60,
                "confirm_count": 2,
                "dispute_count": 0,
                "fixed_count": 0,
                "created_at": "2026-11-22T10:00:00Z",
                "fixed_at": None,
                "is_seed": False,
            },
        ]

        try:
            # Query about resolved hazards
            r = client.post(
                "/api/ai/assistant",
                json={"query": "How many hazards have been resolved?"},
            )
            assert r.status_code == 200
            res = r.json()
            assert "Resolution Status Report" in res["reply"] or "resolved" in res["reply"].lower()
            assert len(res["suggested_actions"]) > 0
        finally:
            app.dependency_overrides.clear()


def test_commuter_ai_uses_configured_gemini_model():
    settings = Settings(
        SUPABASE_URL="https://test.supabase.co",
        SUPABASE_ANON_KEY="dummy-anon-key",
        GEMINI_API_KEY="test-gemini-key",
        VISION_AI_MODEL="gemini-test-model",
    )
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "Grounded answer."}]}}]
    }

    with patch("app.services.ai_assistant._get_live_civic_context", return_value={}), patch(
        "app.services.ai_assistant.httpx.Client"
    ) as client_factory:
        client_factory.return_value.__enter__.return_value.post.return_value = response

        result = answer_commuter_query("Summarize the reports", "test-token", settings)

    request_url = client_factory.return_value.__enter__.return_value.post.call_args.args[0]
    assert request_url.endswith("/gemini-test-model:generateContent")
    assert result.reply == "Grounded answer."
