"""Automated unit and API integration tests for Stage 7 Issue #22 (A9).

Covers:
- Unauthenticated 401 rejection on voting and comment endpoints.
- CSRF double-submit 403 protection on state-changing POST requests.
- Invalid UUID format (400) and nonexistent report (404) handling.
- Self-vote rejection (403) and duplicate vote conflict (409) handling.
- Valid confirm, dispute, and fixed votes with authoritative trigger metrics.
- Two-user fixed threshold transitioning status to 'fixed'.
- Optional after-photo on fixed votes with EXIF stripping and size validation.
- Comment creation (empty rejected, length capped, privacy/hide_name support).
- Chronological comment listing without exposing sensitive user data.
- Strict security requirement: SUPABASE_SERVICE_KEY is never used.
"""

from datetime import datetime, timezone
import io
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch
import uuid

from fastapi.testclient import TestClient
from PIL import ExifTags, Image
import pytest

from app.config import Settings
from app.deps import AuthenticatedContext, get_authenticated_context, get_settings
from app.main import app
from app.services.auth import AuthenticatedUser
from app.services.images import MAX_IMAGE_SIZE_BYTES

USER_REPORTER_ID = "11111111-1111-4111-8111-111111111111"
USER_VOTER_1_ID = "22222222-2222-4222-8222-222222222222"
USER_VOTER_2_ID = "33333333-3333-4333-8333-333333333333"
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


def _auth_ctx(user_id: str = USER_VOTER_1_ID, full_name: str = "Test Voter") -> AuthenticatedContext:
    return AuthenticatedContext(
        user=AuthenticatedUser(
            id=user_id,
            email=f"{user_id[:8]}@example.com",
            full_name=full_name,
            role="user",
        ),
        access_token="mock-voter-jwt",
    )


def _csrf_headers(client: TestClient, token: str = "stage7-a9-csrf-token") -> dict[str, str]:
    client.cookies.set("sr_csrf_token", token)
    return {"X-CSRF-Token": token}


def _make_jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (32, 32), color=(16, 110, 190))
    exif = img.getexif()
    exif[ExifTags.Base.Make] = "TestCamera"
    exif[ExifTags.Base.Model] = "AfterPhotoCam"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


class MockSupabaseTable:
    def __init__(self, table_name: str, state: dict[str, Any]):
        self.table_name = table_name
        self.state = state
        self._action = "select"
        self._filters: list[tuple[str, str, Any]] = []
        self._in_filters: list[tuple[str, list[Any]]] = []
        self._payload: Any = None
        self._order_field: Optional[str] = None
        self._order_desc: bool = False

    def select(self, *args, **kwargs):
        self._action = "select"
        return self

    def insert(self, payload: Any):
        self._action = "insert"
        self._payload = payload
        return self

    def eq(self, column: str, value: Any):
        self._filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values: list[Any]):
        self._in_filters.append((column, values))
        return self

    def order(self, field: str, desc: bool = False):
        self._order_field = field
        self._order_desc = desc
        return self

    def maybe_single(self):
        return self

    def single(self):
        return self

    def execute(self):
        resp = MagicMock()
        if self._action == "insert":
            if self.table_name == "verifications":
                report_id = self._payload["report_id"]
                user_id = self._payload["user_id"]
                vote = self._payload["vote"]
                # Enforce primary key (report_id, user_id, vote)
                existing = [
                    v for v in self.state["verifications"]
                    if v["report_id"] == report_id and v["user_id"] == user_id and v["vote"] == vote
                ]
                if existing:
                    raise Exception('duplicate key value violates unique constraint "verifications_pkey" (23505)')
                new_v = {"report_id": report_id, "user_id": user_id, "vote": vote, "created_at": datetime.now(timezone.utc).isoformat()}
                self.state["verifications"].append(new_v)
                # Recompute parent metrics simulating DB trigger
                report = self.state["reports"].get(report_id)
                if report:
                    all_v = [v for v in self.state["verifications"] if v["report_id"] == report_id]
                    report["confirm_count"] = len([v for v in all_v if v["vote"] == "confirm"])
                    report["dispute_count"] = len([v for v in all_v if v["vote"] == "dispute"])
                    report["fixed_count"] = len([v for v in all_v if v["vote"] == "fixed"])
                    if report["fixed_count"] >= 2:
                        report["status"] = "fixed"
                        report["fixed_at"] = datetime.now(timezone.utc).isoformat()
                    # Recalculate severity score (pothole base 40 + photo 10 + 8*confirms - 10*disputes + recency 10)
                    base = 40
                    photo = 10
                    recency = 10
                    v_score = min(report["confirm_count"] * 8, 24) - (report["dispute_count"] * 10)
                    report["severity_score"] = max(0, min(100, base + photo + v_score + recency))
                    if report["confirm_count"] >= 3 and report["confirm_count"] > report["dispute_count"]:
                        report["confidence"] = "high"
                    elif report["confirm_count"] >= 1:
                        report["confidence"] = "medium"
                    else:
                        report["confidence"] = "low"
                    report["escalation_recommended"] = (report["confirm_count"] >= 5 or report["severity_score"] >= 70)
                resp.data = [new_v]
                return resp

            if self.table_name == "comments":
                c = dict(self._payload)
                c["id"] = c.get("id") or str(uuid.uuid4())
                c["created_at"] = datetime.now(timezone.utc).isoformat()
                self.state["comments"].append(c)
                resp.data = [c]
                return resp

            if self.table_name == "report_images":
                img = dict(self._payload)
                img["id"] = str(uuid.uuid4())
                img["created_at"] = datetime.now(timezone.utc).isoformat()
                self.state["images"].append(img)
                resp.data = [img]
                return resp

        if self._action == "select":
            if self.table_name == "hazard_reports":
                target_id = None
                for op, col, val in self._filters:
                    if op == "eq" and col == "id":
                        target_id = val
                report = self.state["reports"].get(target_id)
                resp.data = dict(report) if report else None
                return resp

            if self.table_name == "verifications":
                rows = list(self.state["verifications"])
                for op, col, val in self._filters:
                    if op == "eq":
                        rows = [r for r in rows if r.get(col) == val]
                resp.data = rows
                return resp

            if self.table_name == "comments":
                rows = list(self.state["comments"])
                for op, col, val in self._filters:
                    if op == "eq":
                        rows = [r for r in rows if r.get(col) == val]
                if self._order_field:
                    rows.sort(key=lambda x: str(x.get(self._order_field) or ""), reverse=self._order_desc)
                resp.data = rows
                return resp

            if self.table_name == "profiles":
                rows = list(self.state["profiles"].values())
                for op, col, val in self._filters:
                    if op == "eq":
                        rows = [r for r in rows if r.get(col) == val]
                for col, vals in self._in_filters:
                    rows = [r for r in rows if r.get(col) in vals]
                resp.data = rows
                return resp

        resp.data = []
        return resp


class MockSupabaseStorageBucket:
    def __init__(self, bucket_name: str, state: dict[str, Any]):
        self.bucket_name = bucket_name
        self.state = state

    def upload(self, path: str, content: bytes, file_options: dict[str, str]):
        self.state["storage"][path] = content
        return {"Key": path}

    def remove(self, paths: list[str]):
        for p in paths:
            self.state["storage"].pop(p, None)
        return [{"name": p} for p in paths]


class MockSupabaseClient:
    def __init__(self, state: dict[str, Any]):
        self.state = state

    def table(self, table_name: str):
        return MockSupabaseTable(table_name, self.state)

    @property
    def storage(self):
        mock_storage = MagicMock()
        mock_storage.from_ = lambda b: MockSupabaseStorageBucket(b, self.state)
        return mock_storage


@pytest.fixture
def mock_db_state():
    return {
        "reports": {
            REPORT_ID: {
                "id": REPORT_ID,
                "reporter_id": USER_REPORTER_ID,
                "category": "pothole",
                "description": "Deep dangerous pothole",
                "location": "POINT(78.4138 17.4849)",
                "status": "open",
                "severity_score": 60,
                "confidence": "low",
                "confirm_count": 0,
                "dispute_count": 0,
                "fixed_count": 0,
                "escalation_recommended": False,
                "fixed_at": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        "verifications": [],
        "comments": [],
        "images": [],
        "profiles": {
            USER_REPORTER_ID: {"id": USER_REPORTER_ID, "full_name": "Reporter Bob"},
            USER_VOTER_1_ID: {"id": USER_VOTER_1_ID, "full_name": "Alice Voter"},
            USER_VOTER_2_ID: {"id": USER_VOTER_2_ID, "full_name": "Charlie Citizen"},
        },
        "storage": {},
    }


# ==============================================================================
# 1. Unauthenticated and CSRF Rejections
# ==============================================================================


def test_unauthenticated_requests_rejected(mock_db_state):
    """Unauthenticated requests to vote or comment endpoints must receive 401."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    try:
        # POST /reports/{id}/vote
        r_vote = client.post(
            f"/reports/{REPORT_ID}/vote",
            json={"vote": "confirm"},
            headers={"Accept": "application/json"},
        )
        assert r_vote.status_code == 401
        assert "authentication required" in r_vote.json()["detail"].lower()

        # GET /reports/{id}/comments
        r_get_c = client.get(
            f"/reports/{REPORT_ID}/comments",
            headers={"Accept": "application/json"},
        )
        assert r_get_c.status_code == 401

        # POST /reports/{id}/comments
        r_post_c = client.post(
            f"/reports/{REPORT_ID}/comments",
            json={"body": "Watch out!"},
            headers={"Accept": "application/json"},
        )
        assert r_post_c.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_csrf_protection_on_vote_and_comment_posts(mock_db_state):
    """Missing or invalid CSRF token on POST vote/comments must return 403."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            # Set auth cookie so request is subject to double-submit CSRF verification
            client.cookies.set("sr_access_token", "mock-rls-jwt")

            # Missing CSRF on vote
            r_no_csrf_vote = client.post(
                f"/reports/{REPORT_ID}/vote",
                json={"vote": "confirm"},
            )
            assert r_no_csrf_vote.status_code == 403
            assert "csrf" in r_no_csrf_vote.json()["detail"].lower()

            # Mismatched CSRF on vote
            client.cookies.set("sr_csrf_token", "cookie-token")
            r_bad_csrf_vote = client.post(
                f"/reports/{REPORT_ID}/vote",
                json={"vote": "confirm"},
                headers={"X-CSRF-Token": "different-token"},
            )
            assert r_bad_csrf_vote.status_code == 403

            # Missing CSRF on comment
            client.cookies.delete("sr_csrf_token")
            r_no_csrf_comment = client.post(
                f"/reports/{REPORT_ID}/comments",
                json={"body": "Test comment"},
            )
            assert r_no_csrf_comment.status_code == 403
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 2. Invalid UUID and Nonexistent Report Handling
# ==============================================================================


def test_invalid_uuid_returns_400():
    """Malformed report_id values must return 400 Bad Request, never 500."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)
    try:
        csrf = _csrf_headers(client)

        r_bad_uuid_vote = client.post("/reports/not-a-valid-uuid/vote", json={"vote": "confirm"}, headers=csrf)
        assert r_bad_uuid_vote.status_code == 400
        assert "invalid report id format" in r_bad_uuid_vote.json()["detail"].lower()

        r_bad_uuid_get = client.get("/reports/not-a-valid-uuid/comments")
        assert r_bad_uuid_get.status_code == 400

        r_bad_uuid_post = client.post("/reports/not-a-valid-uuid/comments", json={"body": "Hello"}, headers=csrf)
        assert r_bad_uuid_post.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_nonexistent_report_returns_404(mock_db_state):
    """Valid UUID format but missing report row must return 404 Not Found."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)
            missing_id = str(uuid.uuid4())

            r_vote = client.post(f"/reports/{missing_id}/vote", json={"vote": "confirm"}, headers=csrf)
            assert r_vote.status_code == 404
            assert "hazard report not found" in r_vote.json()["detail"].lower()

            r_get = client.get(f"/reports/{missing_id}/comments")
            assert r_get.status_code == 404

            r_post = client.post(f"/reports/{missing_id}/comments", json={"body": "Hello"}, headers=csrf)
            assert r_post.status_code == 404
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 3. Self-Voting & Duplicate Vote Protection
# ==============================================================================


def test_self_voting_rejected_with_403(mock_db_state):
    """Users cannot vote on their own hazard reports (must return 403 Forbidden)."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    # Authenticate as USER_REPORTER_ID (owner of REPORT_ID)
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_REPORTER_ID)

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)
            r = client.post(f"/reports/{REPORT_ID}/vote", json={"vote": "confirm"}, headers=csrf)
            assert r.status_code == 403
            assert "cannot vote on their own" in r.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


def test_duplicate_vote_and_conflict_handling(mock_db_state):
    """Recasting the same vote or casting conflicting confirm/dispute votes returns 409 Conflict."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)

            # First confirm vote succeeds
            r1 = client.post(f"/reports/{REPORT_ID}/vote", json={"vote": "confirm"}, headers=csrf)
            assert r1.status_code == 200
            assert r1.json()["confirm_count"] == 1

            # Second identical vote returns 409
            r2 = client.post(f"/reports/{REPORT_ID}/vote", json={"vote": "confirm"}, headers=csrf)
            assert r2.status_code == 409
            assert "already submitted a 'confirm' vote" in r2.json()["detail"].lower()

            # Conflicting dispute vote returns 409
            r3 = client.post(f"/reports/{REPORT_ID}/vote", json={"vote": "dispute"}, headers=csrf)
            assert r3.status_code == 409
            assert "cannot cast conflicting votes" in r3.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 4. Valid Votes & Two-User Fixed Status Transition
# ==============================================================================


def test_valid_votes_and_two_user_fixed_transition(mock_db_state):
    """Voting confirm increments counts; 2 distinct fixed votes transition report to 'fixed'."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)

            # 1. User Voter 1 casts confirm
            app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)
            r_c1 = client.post(f"/reports/{REPORT_ID}/vote", json={"vote": "confirm"}, headers=csrf)
            assert r_c1.status_code == 200
            data_c1 = r_c1.json()
            assert data_c1["confirm_count"] == 1
            assert data_c1["dispute_count"] == 0
            assert data_c1["confidence"] == "medium"
            assert data_c1["status"] == "open"
            assert data_c1["severity_score"] == 68  # 40 + 10 + 8 + 10 = 68
            assert data_c1["severity_band"] == "medium"

            # 2. User Voter 1 marks fixed
            r_f1 = client.post(f"/reports/{REPORT_ID}/votes", json={"vote": "fixed"}, headers=csrf)
            assert r_f1.status_code == 200
            data_f1 = r_f1.json()
            assert data_f1["fixed_count"] == 1
            assert data_f1["status"] == "open"  # Still open: requires 2 distinct users
            assert data_f1["fixed_at"] is None

            # 3. User Voter 2 marks fixed -> triggers transition to 'fixed'
            app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_2_ID)
            r_f2 = client.post(f"/reports/{REPORT_ID}/vote", json={"vote": "fixed"}, headers=csrf)
            assert r_f2.status_code == 200
            data_f2 = r_f2.json()
            assert data_f2["fixed_count"] == 2
            assert data_f2["status"] == "fixed"
            assert data_f2["fixed_at"] is not None
        finally:
            app.dependency_overrides.clear()


def test_vote_via_form_urlencoded(mock_db_state):
    """Voting endpoints accept application/x-www-form-urlencoded payloads seamlessly."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)
            r = client.post(
                f"/reports/{REPORT_ID}/vote",
                data={"vote": "dispute"},
                headers={"Content-Type": "application/x-www-form-urlencoded", **csrf},
            )
            assert r.status_code == 200
            assert r.json()["vote"] == "dispute"
            assert r.json()["dispute_count"] == 1
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 5. Fixed Vote with Optional After-Photo
# ==============================================================================


def test_fixed_vote_with_optional_after_photo(mock_db_state):
    """Fixed vote with valid JPEG strips EXIF and uploads to storage bucket."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)
            jpeg_bytes = _make_jpeg_with_exif()

            r = client.post(
                f"/reports/{REPORT_ID}/vote",
                data={"vote": "fixed"},
                files={"after_photo": ("repaired.jpg", jpeg_bytes, "image/jpeg")},
                headers=csrf,
            )
            assert r.status_code == 200
            assert r.json()["fixed_count"] == 1
            # Verify file was uploaded to mock storage
            assert len(mock_db_state["storage"]) == 1
            uploaded_path = list(mock_db_state["storage"].keys())[0]
            assert uploaded_path.startswith(f"{USER_VOTER_1_ID}/{REPORT_ID}/")
            # Verify EXIF was stripped: check Make tag is no longer present in stored bytes
            stored_img = Image.open(io.BytesIO(mock_db_state["storage"][uploaded_path]))
            stored_exif = stored_img.getexif()
            assert ExifTags.Base.Make not in stored_exif
        finally:
            app.dependency_overrides.clear()


def test_after_photo_validation_failures(mock_db_state):
    """Oversized files, unsupported types, and attaching photo to non-fixed vote must fail."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)

            # 1. Attaching photo to confirm vote returns 400
            r_conf = client.post(
                f"/reports/{REPORT_ID}/vote",
                data={"vote": "confirm"},
                files={"after_photo": ("proof.jpg", _make_jpeg_with_exif(), "image/jpeg")},
                headers=csrf,
            )
            assert r_conf.status_code == 400
            assert "after-photo can only be attached to a 'fixed' vote" in r_conf.json()["detail"].lower()

            # 2. Unsupported file type (text/plain) returns 400 or 415
            r_txt = client.post(
                f"/reports/{REPORT_ID}/vote",
                data={"vote": "fixed"},
                files={"after_photo": ("note.txt", b"fixed already", "text/plain")},
                headers=csrf,
            )
            assert r_txt.status_code in (400, 415)
            assert "unsupported" in r_txt.json()["detail"].lower() or "image" in r_txt.json()["detail"].lower()

            # 3. Oversized file (> 5 MB) returns 400 or 413
            big_fake_image = b"\xff\xd8\xff" + b"0" * (MAX_IMAGE_SIZE_BYTES + 50)
            r_big = client.post(
                f"/reports/{REPORT_ID}/vote",
                data={"vote": "fixed"},
                files={"after_photo": ("huge.jpg", big_fake_image, "image/jpeg")},
                headers=csrf,
            )
            assert r_big.status_code in (400, 413)
            assert (
                "5 mb" in r_big.json()["detail"].lower()
                or "size" in r_big.json()["detail"].lower()
                or "exceeds" in r_big.json()["detail"].lower()
            )
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 6. Comments Creation & Listing
# ==============================================================================


def test_comment_validation_and_creation(mock_db_state):
    """Empty comments rejected; valid comments created with author name or privacy option."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID, full_name="Alice Voter")

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            csrf = _csrf_headers(client)

            # 1. Empty body returns 400
            r_empty = client.post(f"/reports/{REPORT_ID}/comments", json={"body": "   "}, headers=csrf)
            assert r_empty.status_code == 400
            assert "cannot be empty" in r_empty.json()["detail"].lower()

            # 2. Oversized body (>1000 chars) returns 400
            r_oversized = client.post(
                f"/reports/{REPORT_ID}/comments",
                json={"body": "x" * 1001},
                headers=csrf,
            )
            assert r_oversized.status_code == 400
            assert "1000 characters" in r_oversized.json()["detail"].lower()

            # 3. Valid named comment
            r_named = client.post(
                f"/reports/{REPORT_ID}/comments",
                json={"body": "Pothole filled with gravel this morning.", "hide_name": False},
                headers=csrf,
            )
            assert r_named.status_code == 201
            d_named = r_named.json()
            assert d_named["author_name"] == "Alice Voter"
            assert d_named["is_anonymous"] is False
            assert d_named["user_id"] == USER_VOTER_1_ID
            assert d_named["body"] == "Pothole filled with gravel this morning."

            # 4. Valid anonymous comment (hide_name=True)
            r_anon = client.post(
                f"/reports/{REPORT_ID}/comments",
                json={"body": "Water is pooling here again.", "hide_name": True},
                headers=csrf,
            )
            assert r_anon.status_code == 201
            d_anon = r_anon.json()
            assert d_anon["author_name"] == "Community Member"
            assert d_anon["is_anonymous"] is True
            assert d_anon["user_id"] is None
        finally:
            app.dependency_overrides.clear()


def test_comment_listing_chronological_and_privacy(mock_db_state):
    """GET /reports/{id}/comments returns comments in created_at ASC order with safe labels."""
    client = TestClient(app)
    app.dependency_overrides[get_settings] = _test_settings
    app.dependency_overrides[get_authenticated_context] = lambda: _auth_ctx(USER_VOTER_1_ID)

    mock_db_state["comments"] = [
        {
            "id": "c1111111-1111-4111-8111-111111111111",
            "report_id": REPORT_ID,
            "user_id": USER_VOTER_1_ID,
            "body": "First observation",
            "created_at": "2026-11-20T10:00:00Z",
        },
        {
            "id": "c2222222-2222-4222-8222-222222222222",
            "report_id": REPORT_ID,
            "user_id": USER_VOTER_2_ID,
            "body": "Second observation",
            "created_at": "2026-11-21T12:00:00Z",
        },
    ]

    mock_client = MockSupabaseClient(mock_db_state)
    with patch("app.services.community.create_authenticated_supabase_client", return_value=mock_client):
        try:
            r = client.get(f"/reports/{REPORT_ID}/comments")
            assert r.status_code == 200
            data = r.json()
            assert data["report_id"] == REPORT_ID
            assert data["total"] == 2
            comments = data["comments"]
            assert comments[0]["body"] == "First observation"
            assert comments[0]["author_name"] == "Alice Voter"
            assert comments[1]["body"] == "Second observation"
            assert comments[1]["author_name"] == "Charlie Citizen"
            # Ensure email or internal secrets are never returned in comments payload
            for c in comments:
                assert "email" not in c
                assert "password" not in c
                assert "token" not in c
        finally:
            app.dependency_overrides.clear()


# ==============================================================================
# 7. Security Guardrail: Service Role Key Never Used
# ==============================================================================


def test_service_role_key_is_never_used_in_a9():
    """Verify SUPABASE_SERVICE_KEY or service_role is never referenced in community.py."""
    svc_path = Path(__file__).resolve().parent.parent / "app" / "services" / "community.py"
    content = svc_path.read_text(encoding="utf-8")

    assert "SUPABASE_SERVICE_KEY" not in content
    assert "service_role" not in content
    # Confirms create_authenticated_supabase_client is used
    assert "create_authenticated_supabase_client" in content
