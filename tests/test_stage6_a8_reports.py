"""Comprehensive unit and API tests for Stage 6 Issue #9 (A8 · Reports API).

All Supabase PostgREST, Storage, and Realtime interactions are mocked so tests
never mutate the live Supabase project or real storage buckets.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import io
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import Request
from fastapi.testclient import TestClient
from PIL import ExifTags, Image
import pytest

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_settings
from app.main import app
from app.services.auth import AuthenticatedUser
from app.services.images import (
    MAX_IMAGE_SIZE_BYTES,
    ImageTooLargeError,
    UnsupportedImageTypeError,
    build_hazard_storage_path,
    validate_and_strip_exif,
)
from app.services.realtime import (
    build_realtime_ws_url,
    sanitize_hazard_event,
    stream_hazard_updates,
)
from app.services.reports import parse_postgis_point


USER_A_ID = "11111111-1111-4111-8111-111111111111"
USER_B_ID = "22222222-2222-4222-8222-222222222222"
REPORT_1_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _test_settings() -> Settings:
    return Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
    )


def _auth_ctx(user_id: str = USER_A_ID, role: str = "user") -> AuthenticatedContext:
    return AuthenticatedContext(
        user=AuthenticatedUser(
            id=user_id,
            email="user@example.com",
            full_name="Test User",
            role=role,
        ),
        access_token="mock-rls-user-jwt",
    )


def _csrf_headers(client: TestClient, token: str = "stage6-a8-csrf-token") -> dict:
    """Set double-submit CSRF cookie on TestClient and return matching header."""
    client.cookies.set("sr_csrf_token", token)
    return {"X-CSRF-Token": token}


def _make_jpeg_with_exif() -> bytes:
    """Create a real JPEG image containing Camera Make, Model, Software & GPS EXIF tags."""
    img = Image.new("RGB", (64, 48), color=(16, 110, 190))
    exif = img.getexif()
    exif[ExifTags.Base.Make] = "TestCameraMaker"
    exif[ExifTags.Base.Model] = "GPS-Spy-Model"
    exif[ExifTags.Base.Software] = "SafeRouteTestCam"
    gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps_ifd[1] = "N"
    gps_ifd[2] = (17.0, 29.0, 5.0)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _make_png() -> bytes:
    img = Image.new("RGBA", (32, 32), color=(15, 252, 190, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_ewkb_point_hex(lng: float, lat: float) -> str:
    """Construct a little-endian PostGIS EWKB Point(lng, lat) with SRID 4326."""
    header = struct.pack("<BII", 1, 0x20000001, 4326)
    coords = struct.pack("<dd", lng, lat)
    return (header + coords).hex()


@pytest.fixture(autouse=True)
def isolate_overrides():
    app.dependency_overrides[get_settings] = _test_settings
    yield
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Image Validation, EXIF Stripping & Storage Path Tests
# ==============================================================================


def test_exif_metadata_is_stripped_from_jpeg_uploads():
    raw_with_exif = _make_jpeg_with_exif()
    with Image.open(io.BytesIO(raw_with_exif)) as before:
        assert len(before.getexif()) >= 2

    processed = validate_and_strip_exif(raw_with_exif, "image/jpeg")
    assert processed.content_type == "image/jpeg"
    assert processed.extension == "jpg"
    assert processed.width == 64 and processed.height == 48

    with Image.open(io.BytesIO(processed.content)) as after:
        assert len(after.getexif()) == 0
    assert b"TestCameraMaker" not in processed.content
    assert b"GPS-Spy-Model" not in processed.content


def test_image_validation_rejects_oversized_and_unsupported_formats():
    with pytest.raises(ImageTooLargeError):
        validate_and_strip_exif(b"x" * (MAX_IMAGE_SIZE_BYTES + 1), "image/jpeg")

    with pytest.raises(UnsupportedImageTypeError):
        validate_and_strip_exif(_make_png(), "image/gif")

    # Spoofed GIF bytes with image/jpeg content-type rejected by Pillow format check
    gif_buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(gif_buf, format="GIF")
    with pytest.raises(UnsupportedImageTypeError):
        validate_and_strip_exif(gif_buf.getvalue(), "image/jpeg")


def test_storage_path_matches_stage4_ownership_convention():
    path = build_hazard_storage_path(USER_A_ID, REPORT_1_ID, "jpg")
    parts = path.split("/")
    assert len(parts) == 3
    assert parts[0] == USER_A_ID
    assert parts[1] == REPORT_1_ID
    assert parts[2].endswith(".jpg")
    assert ".." not in path


def test_postgis_point_parser_handles_ewkb_geojson_and_wkt():
    ewkb = _make_ewkb_point_hex(78.4138, 17.4849)
    lat, lng = parse_postgis_point(ewkb)
    assert lat == pytest.approx(17.4849)
    assert lng == pytest.approx(78.4138)

    lat2, lng2 = parse_postgis_point({"type": "Point", "coordinates": [78.4138, 17.4849]})
    assert lat2 == pytest.approx(17.4849)
    assert lng2 == pytest.approx(78.4138)

    lat3, lng3 = parse_postgis_point("SRID=4326;POINT(78.4138 17.4849)")
    assert lat3 == pytest.approx(17.4849)
    assert lng3 == pytest.approx(78.4138)


# ==============================================================================
# 2. Report Creation (POST /reports) & Failure Rollback Tests
# ==============================================================================


def _make_report_creation_mock():
    mock_client = MagicMock()
    bucket = mock_client.storage.from_.return_value
    bucket.upload.return_value = {"Key": "uploaded"}
    bucket.create_signed_url.return_value = {"signedURL": "https://signed.example/photo.jpg"}

    def table_router(table_name: str):
        tbl = MagicMock()
        if table_name == "hazard_reports":
            base_row = {
                "id": REPORT_1_ID,
                "reporter_id": USER_A_ID,
                "category": "pothole",
                "description": "Deep crater on main road",
                "location": _make_ewkb_point_hex(78.4138, 17.4849),
                "address_text": "Kukatpally Y Junction",
                "status": "open",
                "escalation_recommended": False,
                "severity_score": 60,
                "confidence": "low",
                "confirm_count": 0,
                "dispute_count": 0,
                "fixed_count": 0,
                "is_seed": False,
                "created_at": "2026-11-18T05:00:00Z",
                "updated_at": "2026-11-18T05:00:00Z",
            }
            tbl.insert.return_value.execute.return_value = SimpleNamespace(data=[base_row])
            tbl.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = (
                SimpleNamespace(data=base_row)
            )
            tbl.delete.return_value.eq.return_value.execute.return_value = SimpleNamespace(data=[])
        elif table_name == "report_images":
            tbl.insert.return_value.execute.return_value = SimpleNamespace(
                data=[
                    {
                        "id": str(uuid.uuid4()),
                        "report_id": REPORT_1_ID,
                        "storage_path": f"{USER_A_ID}/{REPORT_1_ID}/photo.jpg",
                        "kind": "evidence",
                        "created_at": "2026-11-18T05:00:00Z",
                    }
                ]
            )
        return tbl

    mock_client.table.side_effect = table_router
    return mock_client


def test_post_reports_valid_submission_strips_exif_and_ignores_forged_fields():
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    mock_client = _make_report_creation_mock()
    inserted_payloads = []

    original_table = mock_client.table.side_effect

    def spy_table(name: str):
        tbl = original_table(name)
        if name == "hazard_reports":
            orig_insert = tbl.insert

            def capture_insert(payload):
                inserted_payloads.append(payload)
                return orig_insert(payload)

            tbl.insert = capture_insert
        return tbl

    mock_client.table.side_effect = spy_table

    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_client,
    ):
        client = TestClient(app)
        headers = _csrf_headers(client)
        resp = client.post(
            "/reports",
            headers=headers,
            data={
                "category": "pothole",
                "description": "Deep crater on main road",
                "lat": "17.4849",
                "lng": "78.4138",
                "address_text": "Kukatpally Y Junction",
                # Forged fields must be completely ignored!
                "reporter_id": USER_B_ID,
                "status": "escalated",
                "severity_score": "100",
                "confirm_count": "99",
                "ai_confidence": "0.99",
                "is_seed": "true",
            },
            files={"image": ("hazard.jpg", _make_jpeg_with_exif(), "image/jpeg")},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["category"] == "pothole"
    assert body["reporter_id"] == USER_A_ID
    assert body["severity_band"] == "medium"
    assert len(body["images"]) == 1
    assert body["images"][0]["signed_url"] == "https://signed.example/photo.jpg"

    # Verify Storage upload received EXIF-stripped JPEG under <USER_A_ID>/<report_uuid>/<file>.jpg
    upload_args = mock_client.storage.from_.return_value.upload.call_args[0]
    storage_path, uploaded_bytes = upload_args[0], upload_args[1]
    assert storage_path.startswith(f"{USER_A_ID}/")
    assert b"TestCameraMaker" not in uploaded_bytes

    # Verify DB payload never passed forged fields
    assert len(inserted_payloads) == 1
    db_row = inserted_payloads[0]
    assert db_row["reporter_id"] == USER_A_ID
    for forbidden in ("status", "severity_score", "confirm_count", "ai_confidence", "is_seed", "duplicate_of"):
        assert forbidden not in db_row


def test_post_reports_validation_errors_missing_fields_category_coords_and_image():
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    client = TestClient(app)

    # Missing CSRF token on multipart form submission -> 403 Forbidden
    r_no_csrf = client.post("/reports", data={"category": "pothole"})
    assert r_no_csrf.status_code == 403

    headers = _csrf_headers(client)

    # Missing required fields -> 400
    r_missing = client.post("/reports", headers=headers, data={"category": "pothole"})
    assert r_missing.status_code == 400

    # Invalid category -> 400
    r_cat = client.post(
        "/reports",
        headers=headers,
        data={"category": "sinkhole", "description": "Bad", "lat": "17.48", "lng": "78.41"},
        files={"image": ("a.png", _make_png(), "image/png")},
    )
    assert r_cat.status_code == 400

    # Invalid coordinates -> 400
    r_coords = client.post(
        "/reports",
        headers=headers,
        data={"category": "pothole", "description": "Bad", "lat": "95.0", "lng": "78.41"},
        files={"image": ("a.png", _make_png(), "image/png")},
    )
    assert r_coords.status_code == 400

    # Oversized image (>5 MB) -> 413
    r_big = client.post(
        "/reports",
        headers=headers,
        data={"category": "pothole", "description": "Big", "lat": "17.48", "lng": "78.41"},
        files={"image": ("big.jpg", b"0" * (MAX_IMAGE_SIZE_BYTES + 10), "image/jpeg")},
    )
    assert r_big.status_code == 413

    # Unsupported MIME -> 415
    r_mime = client.post(
        "/reports",
        headers=headers,
        data={"category": "pothole", "description": "Txt", "lat": "17.48", "lng": "78.41"},
        files={"image": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert r_mime.status_code == 415


def test_post_reports_rolls_back_cleanly_on_storage_and_db_failures():
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    client = TestClient(app)
    headers = _csrf_headers(client)

    # 1. Storage upload fails -> ZERO DB inserts
    mock_storage_fail = MagicMock()
    mock_storage_fail.storage.from_.return_value.upload.side_effect = RuntimeError("storage down")
    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_storage_fail,
    ):
        r1 = client.post(
            "/reports",
            headers=headers,
            data={"category": "pothole", "description": "Test", "lat": "17.48", "lng": "78.41"},
            files={"image": ("a.png", _make_png(), "image/png")},
        )
    assert r1.status_code == 503
    mock_storage_fail.table.assert_not_called()

    # 2. hazard_reports DB insert fails -> uploaded Storage object is removed (zero orphan)
    mock_db_fail = MagicMock()
    mock_db_fail.table.return_value.insert.return_value.execute.side_effect = RuntimeError("db err")
    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_db_fail,
    ):
        r2 = client.post(
            "/reports",
            headers=headers,
            data={"category": "pothole", "description": "Test", "lat": "17.48", "lng": "78.41"},
            files={"image": ("a.png", _make_png(), "image/png")},
        )
    assert r2.status_code == 503
    mock_db_fail.storage.from_.return_value.remove.assert_called_once()

    # 3. report_images insert fails -> BOTH hazard_reports row and Storage object rolled back
    mock_img_fail = MagicMock()
    hr_tbl = MagicMock()
    hr_tbl.insert.return_value.execute.return_value = SimpleNamespace(data=[{"id": REPORT_1_ID}])
    img_tbl = MagicMock()
    img_tbl.insert.return_value.execute.side_effect = RuntimeError("img insert fail")
    mock_img_fail.table.side_effect = lambda name: hr_tbl if name == "hazard_reports" else img_tbl

    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_img_fail,
    ):
        r3 = client.post(
            "/reports",
            headers=headers,
            data={"category": "pothole", "description": "Test", "lat": "17.48", "lng": "78.41"},
            files={"image": ("a.png", _make_png(), "image/png")},
        )
    assert r3.status_code == 503
    hr_tbl.delete.assert_called_once()
    mock_img_fail.storage.from_.return_value.remove.assert_called_once()


# ==============================================================================
# 3. Duplicate Detection & Same-Hazard Confirmation Tests
# ==============================================================================


def _recent_iso(days_ago: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _make_duplicate_confirm_mock(
    *,
    reporter_id: str = USER_A_ID,
    category: str = "pothole",
    status_val: str = "open",
    days_ago: int = 1,
    nearby_matches: list | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build mocked Supabase client for duplicate confirmation revalidation tests."""
    created_iso = _recent_iso(days_ago)
    mock_client = MagicMock()
    hr_tbl = MagicMock()
    hr_tbl.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = (
        SimpleNamespace(
            data={
                "id": REPORT_1_ID,
                "reporter_id": reporter_id,
                "category": category,
                "status": status_val,
                "created_at": created_iso,
            }
        )
    )
    ver_tbl = MagicMock()
    ver_tbl.insert.return_value.execute.return_value = SimpleNamespace(data=[{}])
    mock_client.table.side_effect = lambda name: hr_tbl if name == "hazard_reports" else ver_tbl

    if nearby_matches is None:
        nearby_matches = [
            {
                "id": REPORT_1_ID,
                "reporter_id": reporter_id,
                "report_category": category,
                "status": status_val,
                "created_at": created_iso,
                "distance_m": 12.5,
            }
        ]
    mock_client.rpc.return_value.execute.return_value = SimpleNamespace(data=nearby_matches)
    return mock_client, hr_tbl, ver_tbl


def test_request_size_limit_middleware_rejects_oversized_content_length_and_chunked_uploads():
    """Finding A: Enforce configurable request body cap BEFORE auth/CSRF/multipart parsing."""
    small_limit_settings = Settings(
        _env_file=None,
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
        MAX_REQUEST_BODY_BYTES=2048,
    )
    app.dependency_overrides[get_settings] = lambda: small_limit_settings
    client = TestClient(app)

    # 1. Oversized Content-Length multipart request rejected with 413 even when unauthenticated
    with patch.object(Request, "form", new_callable=AsyncMock) as spy_form:
        r_unauth_big = client.post(
            "/reports",
            headers={"Accept": "application/json"},
            data={"category": "pothole", "description": "x" * 3000},
        )
    assert r_unauth_big.status_code == 413
    spy_form.assert_not_called()

    # 2. Oversized authenticated multipart request also rejected with 413 before Request.form()
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    headers = _csrf_headers(client)
    with patch.object(Request, "form", new_callable=AsyncMock) as spy_form_auth:
        r_auth_big = client.post(
            "/reports",
            headers=headers,
            data={"category": "pothole", "description": "Test", "lat": "17.48", "lng": "78.41"},
            files={"image": ("big.png", b"A" * 4096, "image/png")},
        )
    assert r_auth_big.status_code == 413
    spy_form_auth.assert_not_called()

    # 3. Unauthenticated bounded POST /reports returns 401 BEFORE Request.form() is invoked
    app.dependency_overrides.pop(get_authenticated_context, None)
    with patch.object(Request, "form", new_callable=AsyncMock) as spy_form_unauth:
        r_unauth_small = client.post(
            "/reports",
            headers={"Accept": "application/json"},
            data={"category": "pothole"},
        )
    assert r_unauth_small.status_code == 401
    spy_form_unauth.assert_not_called()

    # 4. Chunked / streaming request without Content-Length exceeding cap -> 413 mid-stream
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)

    def _oversized_chunks():
        yield b"--boundary\r\nContent-Disposition: form-data; name=\"description\"\r\n\r\n"
        for _ in range(5):
            yield b"Z" * 1024
        yield b"\r\n--boundary--\r\n"

    r_chunked = client.post(
        "/reports",
        headers={
            **headers,
            "Content-Type": "multipart/form-data; boundary=boundary",
        },
        content=_oversized_chunks(),
    )
    assert r_chunked.status_code == 413

    # 5. Spoofed small Content-Length header with oversized streamed body -> 413 via ASGI receive
    async def _call_asgi_spoofed_content_length() -> int:
        sent_messages: list[dict] = []
        chunks = [
            {"type": "http.request", "body": b"X" * 1500, "more_body": True},
            {"type": "http.request", "body": b"Y" * 1500, "more_body": False},
        ]
        idx = 0

        async def fake_receive():
            nonlocal idx
            if idx < len(chunks):
                msg = chunks[idx]
                idx += 1
                return msg
            return {"type": "http.request", "body": b"", "more_body": False}

        async def fake_send(message):
            sent_messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/reports/check-duplicate",
            "raw_path": b"/reports/check-duplicate",
            "query_string": b"",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", b"100"),  # Spoofed small length!
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        await app(scope, fake_receive, fake_send)
        start = next(m for m in sent_messages if m["type"] == "http.response.start")
        return int(start["status"])

    assert asyncio.run(_call_asgi_spoofed_content_length()) == 413


def test_check_duplicate_calls_nearby_reports_rpc_and_maps_matches():
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_B_ID)
    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value = SimpleNamespace(
        data=[
            {
                "id": REPORT_1_ID,
                "reporter_id": USER_A_ID,
                "report_category": "pothole",
                "description": "Nearby pothole",
                "report_lat": 17.4849,
                "report_lng": 78.4138,
                "address_text": "Kukatpally",
                "status": "open",
                "severity_score": 60,
                "confidence": "low",
                "confirm_count": 0,
                "dispute_count": 0,
                "created_at": _recent_iso(1),
                "distance_m": 12.345,
            }
        ]
    )

    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_client,
    ):
        client = TestClient(app)
        resp = client.post(
            "/reports/check-duplicate",
            json={"lat": 17.48495, "lng": 78.41385, "category": "pothole"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_duplicates"] is True
    assert body["radius_m"] == 30.0
    assert body["lookback_days"] == 30
    assert len(body["matches"]) == 1
    assert body["matches"][0]["distance_m"] == 12.35
    mock_client.rpc.assert_called_once_with(
        "nearby_reports",
        {"lat": 17.48495, "lng": 78.41385, "radius_m": 30.0, "category": "pothole"},
    )


def test_check_duplicate_rejects_client_radius_override_and_locks_30m():
    """Finding B: Reject any attempt to override the fixed 30-metre duplicate radius."""
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_B_ID)
    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value = SimpleNamespace(data=[])
    client = TestClient(app)

    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_client,
    ):
        # Attempting to increase or alter radius_m must be rejected with 422 (zero RPC calls)
        for bad_radius in (30.1, 100, 500, 50000, 0, -10, 15):
            r_bad = client.post(
                "/reports/check-duplicate",
                json={
                    "lat": 17.48495,
                    "lng": 78.41385,
                    "category": "pothole",
                    "radius_m": bad_radius,
                },
            )
            assert r_bad.status_code == 422, f"Expected 422 for radius_m={bad_radius}"
        mock_client.rpc.assert_not_called()

        # Explicit 30.0 matches server rule and succeeds
        r_exact = client.post(
            "/reports/check-duplicate",
            json={
                "lat": 17.48495,
                "lng": 78.41385,
                "category": "pothole",
                "radius_m": 30,
            },
        )
        assert r_exact.status_code == 200
        assert r_exact.json()["radius_m"] == 30.0
        assert r_exact.json()["lookback_days"] == 30
        mock_client.rpc.assert_called_once_with(
            "nearby_reports",
            {"lat": 17.48495, "lng": 78.41385, "radius_m": 30.0, "category": "pothole"},
        )


def test_duplicate_rules_are_immutable_against_env_and_settings_overrides(monkeypatch):
    """Approved 30m radius & 30-day lookback cannot be changed by env vars or Settings."""
    # 1. Default Settings reports exact 30m and 30 days
    default_cfg = Settings(_env_file=None)
    assert default_cfg.DUPLICATE_RADIUS_METERS == 30.0
    assert default_cfg.DUPLICATE_WINDOW_DAYS == 30

    # 2. Environment variables and kwargs cannot alter Settings properties
    monkeypatch.setenv("DUPLICATE_RADIUS_METERS", "10")
    monkeypatch.setenv("DUPLICATE_WINDOW_DAYS", "60")
    env_cfg = Settings(_env_file=None)
    assert env_cfg.DUPLICATE_RADIUS_METERS == 30.0
    assert env_cfg.DUPLICATE_WINDOW_DAYS == 30

    kwarg_cfg = Settings(_env_file=None, DUPLICATE_RADIUS_METERS=29.0, DUPLICATE_WINDOW_DAYS=7)
    assert kwarg_cfg.DUPLICATE_RADIUS_METERS == 30.0
    assert kwarg_cfg.DUPLICATE_WINDOW_DAYS == 30

    with pytest.raises(AttributeError):
        env_cfg.DUPLICATE_RADIUS_METERS = 10.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        env_cfg.DUPLICATE_WINDOW_DAYS = 7  # type: ignore[misc]

    # 3. Even a tampered settings object (radius 10/29m, lookback 7/60d) cannot change RPC or rules
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_B_ID)
    client = TestClient(app)
    headers = _csrf_headers(client)

    for fake_radius, fake_days in ((10.0, 7), (29.0, 60)):
        tampered_cfg = SimpleNamespace(
            SUPABASE_URL="https://test-project.supabase.co",
            SUPABASE_ANON_KEY="sb_publishable_test_dummy",
            SUPABASE_SERVICE_KEY="",
            SECRET_KEY="unit-test-secret-key",
            ENVIRONMENT="development",
            ACCESS_COOKIE_NAME="sr_access_token",
            REFRESH_COOKIE_NAME="sr_refresh_token",
            CSRF_COOKIE_NAME="sr_csrf_token",
            MAX_REQUEST_BODY_BYTES=6 * 1024 * 1024,
            DUPLICATE_RADIUS_METERS=fake_radius,
            DUPLICATE_WINDOW_DAYS=fake_days,
        )
        app.dependency_overrides[get_settings] = lambda cfg=tampered_cfg: cfg

        # Duplicate search MUST still pass exact radius_m=30.0 and report 30.0 / 30
        mock_search = MagicMock()
        mock_search.rpc.return_value.execute.return_value = SimpleNamespace(data=[])
        with patch(
            "app.services.duplicates.create_authenticated_supabase_client",
            return_value=mock_search,
        ):
            r_search = client.post(
                "/reports/check-duplicate",
                headers=headers,
                json={"lat": 17.48495, "lng": 78.41385, "category": "pothole"},
            )
        assert r_search.status_code == 200
        assert r_search.json()["radius_m"] == 30.0
        assert r_search.json()["lookback_days"] == 30
        mock_search.rpc.assert_called_once_with(
            "nearby_reports",
            {"lat": 17.48495, "lng": 78.41385, "radius_m": 30.0, "category": "pothole"},
        )

    # 4. With tampered lookback=7d & radius=10m: a 15-day-old report at 29.5m is STILL eligible
    tampered_tight = SimpleNamespace(
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
        ACCESS_COOKIE_NAME="sr_access_token",
        REFRESH_COOKIE_NAME="sr_refresh_token",
        CSRF_COOKIE_NAME="sr_csrf_token",
        MAX_REQUEST_BODY_BYTES=6 * 1024 * 1024,
        DUPLICATE_RADIUS_METERS=10.0,
        DUPLICATE_WINDOW_DAYS=7,
    )
    app.dependency_overrides[get_settings] = lambda: tampered_tight
    created_15d = _recent_iso(15)
    mock_15d, _, ver_15d = _make_duplicate_confirm_mock(
        days_ago=15,
        nearby_matches=[
            {
                "id": REPORT_1_ID,
                "reporter_id": USER_A_ID,
                "report_category": "pothole",
                "status": "open",
                "created_at": created_15d,
                "distance_m": 29.5,
            }
        ],
    )
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_15d,
    ):
        r_confirm_ok = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
    assert r_confirm_ok.status_code == 200
    mock_15d.rpc.assert_called_once_with(
        "nearby_reports",
        {"lat": 17.48495, "lng": 78.41385, "radius_m": 30.0, "category": "pothole"},
    )
    ver_15d.insert.assert_called_once()

    # 5. With tampered lookback=60d & radius=29m: a 35-day-old report is STILL rejected (400)
    tampered_loose = SimpleNamespace(
        SUPABASE_URL="https://test-project.supabase.co",
        SUPABASE_ANON_KEY="sb_publishable_test_dummy",
        SUPABASE_SERVICE_KEY="",
        SECRET_KEY="unit-test-secret-key",
        ENVIRONMENT="development",
        ACCESS_COOKIE_NAME="sr_access_token",
        REFRESH_COOKIE_NAME="sr_refresh_token",
        CSRF_COOKIE_NAME="sr_csrf_token",
        MAX_REQUEST_BODY_BYTES=6 * 1024 * 1024,
        DUPLICATE_RADIUS_METERS=29.0,
        DUPLICATE_WINDOW_DAYS=60,
    )
    app.dependency_overrides[get_settings] = lambda: tampered_loose
    created_35d = _recent_iso(35)
    mock_35d, _, ver_35d = _make_duplicate_confirm_mock(
        days_ago=35,
        nearby_matches=[
            {
                "id": REPORT_1_ID,
                "reporter_id": USER_A_ID,
                "report_category": "pothole",
                "status": "open",
                "created_at": created_35d,
                "distance_m": 10.0,
            }
        ],
    )
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_35d,
    ):
        r_35d_json = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
        r_35d_form = client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": "17.48495",
                "lng": "78.41385",
            },
        )
    assert r_35d_json.status_code == 400
    assert r_35d_form.status_code == 400
    ver_35d.insert.assert_not_called()

    # 6. Distance > 30.0m (e.g. 30.5m) is rejected even under tampered settings
    mock_30_5m, _, ver_30_5m = _make_duplicate_confirm_mock(
        days_ago=1,
        nearby_matches=[
            {
                "id": REPORT_1_ID,
                "reporter_id": USER_A_ID,
                "report_category": "pothole",
                "status": "open",
                "created_at": _recent_iso(1),
                "distance_m": 30.5,
            }
        ],
    )
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_30_5m,
    ):
        r_over_30 = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
    assert r_over_30.status_code == 400
    ver_30_5m.insert.assert_not_called()


def test_same_hazard_creates_confirm_vote_and_blocks_self_and_duplicate_votes():
    client = TestClient(app)
    headers = _csrf_headers(client)

    # 1. Valid confirmation by User B on User A's report (via POST /reports duplicate_action)
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_B_ID)
    mock_client, hr_tbl, ver_tbl = _make_duplicate_confirm_mock()

    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_client,
    ):
        r_ok = client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": "17.48495",
                "lng": "78.41385",
            },
        )
    assert r_ok.status_code == 200
    assert r_ok.json()["action"] == "confirmed_existing"
    assert r_ok.json()["vote"] == "confirm"
    # Never created a second hazard_reports row
    hr_tbl.insert.assert_not_called()
    # Authoritatively revalidated via nearby_reports RPC at fixed 30m
    mock_client.rpc.assert_called_once_with(
        "nearby_reports",
        {"lat": 17.48495, "lng": 78.41385, "radius_m": 30.0, "category": "pothole"},
    )
    ver_tbl.insert.assert_called_once_with(
        {"report_id": REPORT_1_ID, "user_id": USER_B_ID, "vote": "confirm"}
    )

    # 2. Self-confirmation by User A rejected with 403 (zero vote inserted)
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    mock_self, _, ver_self = _make_duplicate_confirm_mock()
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_self,
    ):
        r_self = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
    assert r_self.status_code == 403
    ver_self.insert.assert_not_called()

    # 3. Duplicate vote by User B rejected with 409 Conflict
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_B_ID)
    mock_dup, _, ver_dup = _make_duplicate_confirm_mock()
    ver_dup.insert.return_value.execute.side_effect = RuntimeError(
        "duplicate key value violates unique constraint verifications_pkey (23505)"
    )
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_dup,
    ):
        r_dup = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
    assert r_dup.status_code == 409


def test_confirm_duplicate_revalidates_all_duplicate_criteria_on_both_paths():
    """Finding C: Independently revalidate category, 30m distance, 30d age, status, and inputs."""
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_B_ID)
    client = TestClient(app)
    headers = _csrf_headers(client)

    # 1. Valid same-category report within 30m via POST /reports/confirm-duplicate -> 200
    mock_valid, _, ver_valid = _make_duplicate_confirm_mock()
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_valid,
    ):
        r_valid = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
    assert r_valid.status_code == 200
    ver_valid.insert.assert_called_once()

    # 2. Different category -> 400 on BOTH endpoints, zero vote inserted
    mock_diff_cat, _, ver_diff_cat = _make_duplicate_confirm_mock(category="streetlight")
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_diff_cat,
    ):
        r_cat_1 = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
        r_cat_2 = client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": "17.48495",
                "lng": "78.41385",
            },
        )
    assert r_cat_1.status_code == 400
    assert r_cat_2.status_code == 400
    ver_diff_cat.insert.assert_not_called()

    # 3. Outside 30 metres (nearby_reports returns empty) even with forged client distance_m=5
    mock_far, _, ver_far = _make_duplicate_confirm_mock(nearby_matches=[])
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_far,
    ):
        r_far_1 = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.5000,
                "lng": 78.4500,
                "distance_m": 5.0,  # Forged client-side distance must NEVER be trusted
            },
        )
        r_far_2 = client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": "17.5000",
                "lng": "78.4500",
                "distance_m": "5.0",
            },
        )
    assert r_far_1.status_code == 400
    assert r_far_2.status_code == 400
    ver_far.insert.assert_not_called()

    # 4. Report older than 30 days -> 400 on BOTH endpoints, zero vote inserted
    mock_old, _, ver_old = _make_duplicate_confirm_mock(days_ago=31, nearby_matches=[])
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_old,
    ):
        r_old_1 = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
        r_old_2 = client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": "17.48495",
                "lng": "78.41385",
            },
        )
    assert r_old_1.status_code == 400
    assert r_old_2.status_code == 400
    ver_old.insert.assert_not_called()

    # 5. Fixed report -> 400 on BOTH endpoints, zero vote inserted
    mock_fixed, _, ver_fixed = _make_duplicate_confirm_mock(status_val="fixed", nearby_matches=[])
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_fixed,
    ):
        r_fix_1 = client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={
                "report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": 17.48495,
                "lng": 78.41385,
            },
        )
        r_fix_2 = client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": "17.48495",
                "lng": "78.41385",
            },
        )
    assert r_fix_1.status_code == 400
    assert r_fix_2.status_code == 400
    ver_fixed.insert.assert_not_called()

    # 6. Missing or forged coordinates/category on BOTH endpoints -> 422 / 400, zero DB calls
    mock_unused = MagicMock()
    with patch(
        "app.services.duplicates.create_authenticated_supabase_client",
        return_value=mock_unused,
    ):
        # Missing lat/lng/category on /reports/confirm-duplicate
        assert client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={"report_id": REPORT_1_ID},
        ).status_code == 422
        # Out-of-range lat on /reports/confirm-duplicate
        assert client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={"report_id": REPORT_1_ID, "category": "pothole", "lat": 99.0, "lng": 78.41},
        ).status_code == 422
        # Forged category on /reports/confirm-duplicate
        assert client.post(
            "/reports/confirm-duplicate",
            headers=headers,
            json={"report_id": REPORT_1_ID, "category": "sinkhole", "lat": 17.48, "lng": 78.41},
        ).status_code == 422
        # Missing coordinates/category on POST /reports duplicate_action=same_hazard
        assert client.post(
            "/reports",
            headers=headers,
            data={"duplicate_action": "same_hazard", "existing_report_id": REPORT_1_ID},
        ).status_code == 400
        # Forged non-numeric lat on POST /reports duplicate_action=same_hazard
        assert client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "pothole",
                "lat": "not-a-float",
                "lng": "78.41",
            },
        ).status_code == 400
        # Forged category on POST /reports duplicate_action=same_hazard
        assert client.post(
            "/reports",
            headers=headers,
            data={
                "duplicate_action": "same_hazard",
                "existing_report_id": REPORT_1_ID,
                "category": "sinkhole",
                "lat": "17.48",
                "lng": "78.41",
            },
        ).status_code == 400
    mock_unused.table.assert_not_called()
    mock_unused.rpc.assert_not_called()


# ==============================================================================
# 4. Report Retrieval (GET /reports/{id}) & Map Bounds (GET /api/reports) Tests
# ==============================================================================


def test_get_report_by_id_existing_missing_and_invalid_uuid():
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    client = TestClient(app)

    # Malformed UUID -> 400
    assert client.get("/reports/not-a-uuid").status_code == 400

    # Existing report -> 200
    mock_client = _make_report_creation_mock()
    img_tbl = MagicMock()
    img_tbl.select.return_value.eq.return_value.order.return_value.execute.return_value = (
        SimpleNamespace(
            data=[
                {
                    "id": str(uuid.uuid4()),
                    "storage_path": f"{USER_A_ID}/{REPORT_1_ID}/photo.jpg",
                    "kind": "evidence",
                    "created_at": "2026-11-18T05:00:00Z",
                }
            ]
        )
    )
    orig = mock_client.table.side_effect
    mock_client.table.side_effect = lambda name: img_tbl if name == "report_images" else orig(name)

    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_client,
    ):
        r_ok = client.get(f"/reports/{REPORT_1_ID}")
    assert r_ok.status_code == 200
    assert r_ok.json()["lat"] == pytest.approx(17.4849)
    assert r_ok.json()["lng"] == pytest.approx(78.4138)

    # Missing report -> 404
    mock_missing = MagicMock()
    (
        mock_missing.table.return_value.select.return_value.eq.return_value
        .maybe_single.return_value.execute.return_value
    ) = SimpleNamespace(data=None)
    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_missing,
    ):
        r_404 = client.get(f"/reports/{REPORT_1_ID}")
    assert r_404.status_code == 404


def test_api_reports_bounds_valid_filters_invalid_bounds_and_500_cap():
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    client = TestClient(app)

    # Invalid bounds -> 400
    assert client.get("/api/reports?bounds=17.4,78.4").status_code == 400
    assert client.get("/api/reports?bounds=18.0,78.0,17.0,79.0").status_code == 400
    assert client.get("/api/reports?bounds=95.0,78.0,96.0,79.0").status_code == 400
    assert client.get("/api/reports?bounds=17.4,78.4,17.5,78.5&category=invalid").status_code == 400
    assert client.get("/api/reports?bounds=17.4,78.4,17.5,78.5&status=invalid").status_code == 400

    # Valid bounds with 550 mocked rows -> strictly capped at 500
    sample_row = {
        "id": REPORT_1_ID,
        "reporter_id": USER_A_ID,
        "category": "pothole",
        "description": "Pothole",
        "lat": 17.4849,
        "lng": 78.4138,
        "address_text": "Kukatpally",
        "status": "open",
        "escalation_recommended": False,
        "severity_score": 72,
        "confidence": "high",
        "confirm_count": 3,
        "dispute_count": 0,
        "fixed_count": 0,
        "created_at": "2026-11-18T05:00:00Z",
        "updated_at": "2026-11-18T05:00:00Z",
    }
    mock_client = MagicMock()
    mock_client.rpc.return_value.execute.return_value = SimpleNamespace(data=[sample_row] * 550)

    with patch(
        "app.services.reports.create_authenticated_supabase_client",
        return_value=mock_client,
    ):
        resp = client.get("/api/reports?bounds=17.48,78.41,17.49,78.42&category=pothole&status=open")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 500
    assert body["limit"] == 500
    assert body["reports"][0]["severity_band"] == "high"
    mock_client.rpc.assert_called_once_with(
        "reports_in_bounds",
        {
            "min_lat": 17.48,
            "min_lng": 78.41,
            "max_lat": 17.49,
            "max_lng": 78.42,
            "category_filter": "pothole",
            "status_filter": "open",
        },
    )


# ==============================================================================
# 5. Auth, CSRF & Option R1 SSE Realtime Bridge Security Tests
# ==============================================================================


def test_unauthenticated_and_csrf_protections_on_reports_endpoints():
    client = TestClient(app)

    # Unauthenticated calls rejected with 401 before CSRF or body parsing
    assert client.get("/api/reports?bounds=17.48,78.41,17.49,78.42").status_code == 401
    assert client.get("/api/reports/stream").status_code == 401
    assert client.get(f"/reports/{REPORT_1_ID}", headers={"Accept": "application/json"}).status_code == 401
    assert client.post(
        "/reports",
        headers={"Accept": "application/json"},
        data={"category": "pothole"},
    ).status_code == 401
    assert client.post("/reports/check-duplicate", json={"lat": 17.48, "lng": 78.41, "category": "pothole"}).status_code == 401
    assert client.post("/reports/confirm-duplicate", json={"report_id": REPORT_1_ID}).status_code == 401

    # Cookie-authenticated POST /reports without CSRF rejected with 403
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_A_ID)
    client.cookies.set("sr_access_token", "cookie-jwt")
    r_no_csrf = client.post(
        "/reports/check-duplicate",
        json={"lat": 17.48, "lng": 78.41, "category": "pothole"},
    )
    assert r_no_csrf.status_code == 403

    # Cross-origin rejected with 403
    r_cross = client.post(
        "/reports/check-duplicate",
        json={"lat": 17.48, "lng": 78.41, "category": "pothole"},
        headers={"Origin": "https://evil.example"},
    )
    assert r_cross.status_code == 403


def test_realtime_sse_bridge_authenticates_with_user_jwt_sanitizes_and_cleans_up():
    assert build_realtime_ws_url("https://myaadekzqtubygwhjmab.supabase.co") == (
        "wss://myaadekzqtubygwhjmab.supabase.co/realtime/v1"
    )

    sanitized = sanitize_hazard_event(
        {
            "data": {
                "type": "INSERT",
                "record": {
                    "id": REPORT_1_ID,
                    "category": "pothole",
                    "description": "Live pothole",
                    "location": _make_ewkb_point_hex(78.4138, 17.4849),
                    "status": "open",
                    "severity_score": 70,
                    "confidence": "medium",
                    "confirm_count": 2,
                    "dispute_count": 0,
                    "fixed_count": 0,
                    "access_token": "should-never-appear",
                },
            }
        }
    )
    assert sanitized is not None
    assert sanitized["event_type"] == "INSERT"
    assert sanitized["report"]["lat"] == pytest.approx(17.4849)
    assert sanitized["report"]["severity_band"] == "high"
    assert "access_token" not in str(sanitized)

    # Verify AsyncRealtimeClient lifecycle: connect -> set_auth(user_jwt) -> subscribe -> cleanup
    captured_callback = {}
    fake_channel = MagicMock()
    fake_channel.on_postgres_changes.side_effect = (
        lambda **kwargs: captured_callback.setdefault("cb", kwargs["callback"])
    )
    fake_channel.subscribe = AsyncMock()

    async def _fake_listen():
        await asyncio.sleep(10)

    fake_rt = MagicMock()
    fake_rt.connect = AsyncMock()
    fake_rt.set_auth = AsyncMock()
    fake_rt.channel.return_value = fake_channel
    fake_rt.listen = AsyncMock(side_effect=_fake_listen)
    fake_rt.remove_all_channels = AsyncMock()
    fake_rt.close = AsyncMock()

    async def _run_stream():
        disconnected_calls = 0

        async def _is_disconnected():
            nonlocal disconnected_calls
            disconnected_calls += 1
            return disconnected_calls > 2

        frames = []
        async for frame in stream_hazard_updates(
            access_token="user-jwt-for-rls",
            is_disconnected=_is_disconnected,
            app_settings=_test_settings(),
            heartbeat_seconds=0.05,
            realtime_client_factory=lambda *args, **kwargs: fake_rt,
        ):
            frames.append(frame)
            if "connected" in frame and "cb" in captured_callback:
                captured_callback["cb"](
                    {
                        "data": {
                            "type": "INSERT",
                            "record": {
                                "id": REPORT_1_ID,
                                "category": "pothole",
                                "description": "Live event",
                                "location": _make_ewkb_point_hex(78.4138, 17.4849),
                                "status": "open",
                                "severity_score": 50,
                                "confidence": "low",
                            },
                        }
                    }
                )
        return frames

    frames = asyncio.run(_run_stream())
    joined = "".join(frames)
    assert "event: connected" in joined
    assert "event: hazard_change" in joined
    assert "user-jwt-for-rls" not in joined  # JWT NEVER leaked to SSE stream!
    fake_rt.set_auth.assert_awaited_once_with("user-jwt-for-rls")
    fake_rt.remove_all_channels.assert_awaited_once()
    fake_rt.close.assert_awaited_once()
