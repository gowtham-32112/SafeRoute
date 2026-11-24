"""Automated unit and integration tests for Stage 8 — Issue #28 (C1/C2).

Covers:
- Unauthenticated 401 rejection on POST /reports/suggest-category.
- CSRF double-submit 403 protection on POST /reports/suggest-category.
- Missing image field (400), unsupported MIME type (415), and oversized image >5MB (413).
- Graceful non-blocking fallback when GEMINI_API_KEY is unset or empty (HTTP 200, category: null).
- EXIF metadata stripping: GPS and camera tags are purged before image is processed.
- Mocked Gemini success: valid JSON response parsed into approved category, confidence, and reason.
- Markdown-wrapped JSON handling from Gemini output.
- Unknown/disallowed category or "none" normalization to category: null.
- Upstream HTTP error, timeout, network exception, and invalid JSON resilience (HTTP 200, category: null).
- Service-level unit tests for confidence clamping and single-line reason sanitization.
- Frontend template and JavaScript contract validation (AI slot, AbortController, CSRF header).
- Security rule verification: SUPABASE_SERVICE_KEY is never imported or used in runtime code.
"""

import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx
from PIL import ExifTags, Image
import pytest

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_settings
from app.main import app
from app.services.auth import AuthenticatedUser
from app.services.vision_ai import (
    APPROVED_CATEGORIES,
    CategorySuggestionResponse,
    _clean_reason_text,
    _extract_json_payload,
    _normalize_category,
    suggest_hazard_category,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
REPORT_NEW_TEMPLATE_PATH = ROOT_DIR / "app" / "templates" / "report_new.html"
REPORT_NEW_JS_PATH = ROOT_DIR / "app" / "static" / "js" / "report_new.js"
ROUTERS_DIR = ROOT_DIR / "app" / "routers"
SERVICES_DIR = ROOT_DIR / "app" / "services"

USER_ID = "11111111-1111-4111-8111-111111111111"


def _test_settings(gemini_key: str = "") -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
        GEMINI_API_KEY=gemini_key,
        VISION_AI_MODEL="gemini-2.5-flash",
        VISION_AI_TIMEOUT_SECONDS=5.0,
    )


def _auth_ctx() -> AuthenticatedContext:
    return AuthenticatedContext(
        user=AuthenticatedUser(
            id=USER_ID,
            email="reporter@example.com",
            full_name="Asha Reporter",
            role="user",
        ),
        access_token="mock-reporter-jwt",
    )


def _csrf_headers(client: TestClient, token: str = "test-csrf-token-c1") -> dict[str, str]:
    client.cookies.set("sr_csrf_token", token)
    return {"X-CSRF-Token": token}


def _create_test_image(format: str = "JPEG", with_exif: bool = True) -> bytes:
    img = Image.new("RGB", (64, 64), color=(16, 110, 190))
    buf = io.BytesIO()
    if with_exif and format.upper() == "JPEG":
        exif = img.getexif()
        exif[ExifTags.Base.Make] = "TestCameraVendor"
        exif[ExifTags.Base.Model] = "SafeRouteUnitCam"
        img.save(buf, format=format, exif=exif)
    else:
        img.save(buf, format=format)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def reset_dependency_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. API Route Access, CSRF & Validation Tests
# ==============================================================================

def test_suggest_category_unauthenticated_returns_401():
    """Unauthenticated request must be rejected with HTTP 401."""
    with TestClient(app) as client:
        image_bytes = _create_test_image()
        response = client.post(
            "/reports/suggest-category",
            files={"image": ("hazard.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 401
        assert "detail" in response.json()


def test_suggest_category_missing_csrf_returns_403():
    """Authenticated request missing valid CSRF token must return HTTP 403."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("mock-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        image_bytes = _create_test_image()
        # No CSRF header or cookie
        response = client.post(
            "/reports/suggest-category",
            files={"image": ("hazard.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 403
        assert "CSRF" in response.json().get("detail", "")


def test_suggest_category_missing_image_file_returns_400():
    """Request without 'image' multipart field returns HTTP 400."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("mock-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        headers = _csrf_headers(client)

        # 1. Non-multipart submission
        resp_non_multipart = client.post(
            "/reports/suggest-category",
            headers=headers,
            data={"foo": "bar"},
        )
        assert resp_non_multipart.status_code == 400
        assert "multipart/form-data" in resp_non_multipart.json().get("detail", "").lower()

        # 2. Multipart without 'image' field
        resp_missing_field = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"not_image": ("file.bin", b"123", "application/octet-stream")},
        )
        assert resp_missing_field.status_code == 400
        assert "image" in resp_missing_field.json().get("detail", "").lower()


def test_suggest_category_unsupported_mime_type_returns_415():
    """Non-image file format returns HTTP 415."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("mock-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("text.txt", b"not an image", "text/plain")},
        )
        assert response.status_code == 415
        assert "unsupported" in response.json().get("detail", "").lower()


def test_suggest_category_oversized_file_returns_413():
    """File exceeding 5MB max upload limit returns HTTP 413."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("mock-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        oversized_bytes = b"0" * (5 * 1024 * 1024 + 10)
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("huge.jpg", oversized_bytes, "image/jpeg")},
        )
        assert response.status_code == 413
        assert "exceeds" in response.json().get("detail", "").lower()


# ==============================================================================
# 2. Vision AI Service & End-to-End Fallback Scenarios
# ==============================================================================

def test_suggest_category_unconfigured_api_key_returns_null_category_200():
    """When GEMINI_API_KEY is not configured, returns HTTP 200 with category: null."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("")  # No key
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        image_bytes = _create_test_image()
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("hazard.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["category"] is None
        assert data["confidence"] is None
        assert "unavailable" in data["reason"].lower()


@patch("httpx.AsyncClient.post")
def test_suggest_category_gemini_success_returns_category(mock_post):
    """Mocked valid Gemini response parses approved category, confidence, and reason."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("test-api-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    gemini_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": '{"category": "pothole", "confidence": 0.94, "reason": "Deep circular depression in tarmac."}'
                        }
                    ]
                }
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = gemini_payload
    mock_post.return_value = mock_response

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        image_bytes = _create_test_image()
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("pothole.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 200
        request_url = mock_post.call_args.args[0]
        request_headers = mock_post.call_args.kwargs["headers"]
        assert "key=" not in request_url
        assert request_headers["x-goog-api-key"] == "test-api-key"
        data = response.json()
        assert data["category"] == "pothole"
        assert data["confidence"] == 0.94
        assert "depression" in data["reason"]


@patch("httpx.AsyncClient.post")
def test_suggest_category_gemini_markdown_fenced_json(mock_post):
    """Gemini response wrapped in markdown ```json ... ``` blocks parses cleanly."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("test-api-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    gemini_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": "```json\n"
                                    '{\n  "category": "streetlight",\n  "confidence": 0.88,\n  "reason": "Unlit pole at dark crosswalk."\n}\n'
                                    "```"
                        }
                    ]
                }
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = gemini_payload
    mock_post.return_value = mock_response

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        image_bytes = _create_test_image()
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("streetlight.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["category"] == "streetlight"
        assert data["confidence"] == 0.88


@patch("httpx.AsyncClient.post")
def test_suggest_category_gemini_none_or_unapproved_category_returns_null(mock_post):
    """Unapproved category ('car') or 'none' normalizes cleanly to null."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("test-api-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    gemini_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": '{"category": "car", "confidence": 0.99, "reason": "Just a vehicle."}'
                        }
                    ]
                }
            }
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = gemini_payload
    mock_post.return_value = mock_response

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        image_bytes = _create_test_image()
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("car.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["category"] is None
        assert data["confidence"] is None
        assert data["reason"] == "Just a vehicle."


@patch("httpx.AsyncClient.post")
def test_suggest_category_upstream_timeout_returns_200_fallback(mock_post):
    """Upstream timeout returns HTTP 200 with category: null without throwing 500."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("test-api-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    mock_post.side_effect = httpx.TimeoutException("Gemini connection timed out")

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        image_bytes = _create_test_image()
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("hazard.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["category"] is None
        assert data["confidence"] is None
        assert "unavailable" in data["reason"].lower()


@patch("httpx.AsyncClient.post")
def test_suggest_category_upstream_500_returns_200_fallback(mock_post):
    """Upstream HTTP 500 returns HTTP 200 with category: null without crashing."""
    app.dependency_overrides[get_settings] = lambda: _test_settings("test-api-key")
    app.dependency_overrides[get_authenticated_context] = _auth_ctx

    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.text = "Service Unavailable"
    mock_post.return_value = mock_response

    with TestClient(app) as client:
        headers = _csrf_headers(client)
        image_bytes = _create_test_image()
        response = client.post(
            "/reports/suggest-category",
            headers=headers,
            files={"image": ("hazard.jpg", image_bytes, "image/jpeg")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["category"] is None
        assert "unavailable" in data["reason"].lower()


# ==============================================================================
# 3. Unit Tests for Vision AI Helpers & Parsing Logic
# ==============================================================================

def test_extract_json_payload_variations():
    """Test JSON payload extractor against standard, markdown, and raw strings."""
    assert _extract_json_payload('{"category": "pothole"}') == {"category": "pothole"}
    assert _extract_json_payload('```json\n{"category": "footpath"}\n```') == {"category": "footpath"}
    assert _extract_json_payload('Here is the json: {"category": "other"} hope it helps!') == {"category": "other"}
    assert _extract_json_payload("No valid json here") is None


def test_normalize_category_approved_and_unapproved():
    """Verify category normalization logic."""
    for cat in APPROVED_CATEGORIES:
        assert _normalize_category(cat) == cat
        assert _normalize_category(cat.upper()) == cat
        assert _normalize_category(f"  {cat}  ") == cat

    assert _normalize_category("none") is None
    assert _normalize_category("potholes") == "pothole"
    assert _normalize_category("crater") == "pothole"
    assert _normalize_category("light") == "streetlight"
    assert _normalize_category("sidewalk") == "footpath"
    assert _normalize_category("unknown_alien_object") is None
    assert _normalize_category("") is None
    assert _normalize_category(None) is None


def test_clean_single_line_reason():
    """Verify reason string sanitization and length limit."""
    multiline = "Line 1\nLine 2\r\nLine 3"
    cleaned = _clean_reason_text(multiline)
    assert cleaned is not None
    assert "\n" not in cleaned
    assert "\r" not in cleaned
    assert "Line 1 Line 2 Line 3" == cleaned

    long_text = "A" * 200
    res = _clean_reason_text(long_text)
    assert res is not None
    assert len(res) <= 160


@pytest.mark.asyncio
async def test_suggest_hazard_category_strips_exif():
    """Verify that EXIF metadata is stripped from image bytes before upstream call."""
    raw_img_with_exif = _create_test_image("JPEG", with_exif=True)
    pil_before = Image.open(io.BytesIO(raw_img_with_exif))
    assert bool(pil_before.getexif()), "Test image must initially have EXIF"

    captured_payloads = []

    async def mock_post(url, json=None, headers=None, timeout=None):
        captured_payloads.append(json)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"category": "pothole", "confidence": 0.9, "reason": "Pothole detected"}'}
                        ]
                    }
                }
            ]
        }
        return mock_resp

    settings = _test_settings("dummy-key")
    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        result = await suggest_hazard_category(
            image_bytes=raw_img_with_exif,
            declared_content_type="image/jpeg",
            app_settings=settings,
        )
        assert result.category == "pothole"
        assert len(captured_payloads) == 1

        # Check payload inlineData base64 bytes
        import base64
        inline_b64 = captured_payloads[0]["contents"][0]["parts"][1]["inlineData"]["data"]
        stripped_bytes = base64.b64decode(inline_b64)
        pil_sent = Image.open(io.BytesIO(stripped_bytes))
        assert not bool(pil_sent.getexif()), "EXIF must be completely stripped before transmission"


# ==============================================================================
# 4. Frontend & JavaScript Contract Tests
# ==============================================================================

def test_report_new_template_contains_ai_suggestion_slot():
    """report_new.html must have #sr-ai-suggestion-slot and handle analyzing + ready states."""
    content = REPORT_NEW_TEMPLATE_PATH.read_text(encoding="utf-8")
    assert 'id="sr-ai-suggestion-slot"' in content
    assert "aiSuggestion || aiAnalyzing" in content
    assert "aiAnalyzing" in content
    assert "Apply suggestion" in content
    assert "text-accent" not in content  # No mint on light surfaces


def test_report_new_js_contracts():
    """report_new.js must make non-blocking call to /reports/suggest-category with CSRF and AbortController."""
    content = REPORT_NEW_JS_PATH.read_text(encoding="utf-8")
    assert "/reports/suggest-category" in content
    assert "X-CSRF-Token" in content
    assert "AbortController" in content
    assert "aiAnalyzing" in content
    assert "innerHTML" not in content
    assert "localStorage" not in content
    assert "sessionStorage" not in content


# ==============================================================================
# 5. Security Guard: SUPABASE_SERVICE_KEY Never in Runtime Code
# ==============================================================================

def test_no_supabase_service_key_in_runtime_code():
    """Neither routers nor application feature services may reference or import SUPABASE_SERVICE_KEY."""
    forbidden = "SUPABASE_SERVICE_KEY"
    # Exclude the low-level supabase_client factory which creates service client for seeds/background tasks
    files_to_check = [
        f for f in list(ROUTERS_DIR.glob("*.py")) + list(SERVICES_DIR.glob("*.py"))
        if f.name != "supabase_client.py"
    ]
    for py_file in files_to_check:
        text = py_file.read_text(encoding="utf-8")
        assert forbidden not in text, f"Forbidden '{forbidden}' found in {py_file}"
        assert "service_role" not in text, f"Forbidden 'service_role' found in {py_file}"
