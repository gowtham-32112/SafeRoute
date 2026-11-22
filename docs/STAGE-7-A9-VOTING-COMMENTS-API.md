# Stage 7 – A9: Community Verification (Voting) & Comments API (Issue #22)

## Overview

This document summarizes the implementation and verification of **Issue #22 (`A9 · Community voting and comments API`)** on top of the approved database triggers, RLS policies, and reports service foundation.

All changes strictly follow the locked SafeRoute stack (Python 3.11 + FastAPI + Pydantic + Supabase PostgreSQL/Storage under user RLS context) and introduce **zero database schema changes**, **zero modifications** to existing Stage 6 contracts, and **zero usage** of `SUPABASE_SERVICE_KEY`.

---

## Files Added and Updated

- [app/services/community.py](../app/services/community.py) — Dedicated community service module containing `cast_hazard_vote`, `create_report_comment`, `list_report_comments`, and `CommunityServiceError`. Executes strictly under the authenticated user's RLS context via `create_authenticated_supabase_client()`.
- [app/routers/reports.py](../app/routers/reports.py) — Exposes `POST /reports/{report_id}/vote` (and alias `POST /reports/{report_id}/votes`), `GET /reports/{report_id}/comments`, and `POST /reports/{report_id}/comments`. Accepts both JSON and form data (including multipart with optional after-photo) and enforces authentication and double-submit CSRF verification.
- [app/main.py](../app/main.py) — Centralized exception handler for `CommunityServiceError` mapping domain exceptions directly to structured JSON error responses.
- [tests/test_stage7_a9_voting_comments.py](../tests/test_stage7_a9_voting_comments.py) — Comprehensive automated test suite with 13 tests covering unauthenticated rejections, CSRF enforcement, UUID validation, self-voting prevention, duplicate vote conflicts, 2-user fixed status transition, optional after-photo handling, comment creation and listing, and service-role key exclusion.
- [docs/STAGE-7-A9-VOTING-COMMENTS-API.md](./STAGE-7-A9-VOTING-COMMENTS-API.md) — This documentation file.

---

## Endpoint Specifications

### 1. `POST /reports/{report_id}/vote` (and alias `/votes`)
- **Authentication**: Requires valid Supabase session (`Depends(get_authenticated_context)`).
- **CSRF Protection**: Double-submit token required on cookie-authenticated requests (`Depends(require_csrf)`).
- **Payload Formats**:
  - `application/json`: `{"vote": "confirm" | "dispute" | "fixed"}`
  - `application/x-www-form-urlencoded`: `vote=confirm`
  - `multipart/form-data`: `vote=fixed`, with optional `after_photo` / `image` file
- **Business Logic Enforced**:
  - UUID format validation: malformed UUID returns `400 Bad Request`.
  - Report existence: missing report returns `404 Not Found`.
  - Self-vote prevention: reporter cannot vote on their own report (`403 Forbidden`).
  - Duplicate vote prevention: user cannot recast same vote or cast conflicting `confirm`/`dispute` votes (`409 Conflict`).
  - Fixed status threshold: when 2 distinct users mark `fixed`, triggers automatically set `status = 'fixed'` and `fixed_at = NOW()`.
  - Optional after-photo: validates MIME (JPEG/PNG/WebP), caps size at 5 MB, strips EXIF metadata, and uploads to `hazard-photos` storage bucket.
- **Success Response (`200 OK`)**:
  ```json
  {
    "report_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "user_id": "22222222-2222-4222-8222-222222222222",
    "vote": "confirm",
    "status": "open",
    "severity_score": 68,
    "severity_band": "medium",
    "confidence": "medium",
    "confirm_count": 1,
    "dispute_count": 0,
    "fixed_count": 0,
    "escalation_recommended": false,
    "fixed_at": null,
    "created_at": "2026-11-22T08:30:00Z"
  }
  ```

---

### 2. `GET /reports/{report_id}/comments`
- **Authentication**: Requires valid Supabase session (`Depends(get_authenticated_context)`).
- **Ordering**: Chronological (`created_at ASC`).
- **Privacy & Sanitization**: Author names resolved from `profiles.full_name`; anonymous comments or missing profiles display as `"Community Member"`. Never returns email addresses, password hashes, or internal tokens.
- **Success Response (`200 OK`)**:
  ```json
  {
    "report_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "total": 2,
    "comments": [
      {
        "id": "c1111111-1111-4111-8111-111111111111",
        "report_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "user_id": "22222222-2222-4222-8222-222222222222",
        "author_name": "Alice Voter",
        "body": "Pothole filled with gravel this morning.",
        "is_anonymous": false,
        "created_at": "2026-11-20T10:00:00Z"
      },
      {
        "id": "c2222222-2222-4222-8222-222222222222",
        "report_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "user_id": null,
        "author_name": "Community Member",
        "body": "Water is pooling here again.",
        "is_anonymous": true,
        "created_at": "2026-11-21T12:00:00Z"
      }
    ]
  }
  ```

---

### 3. `POST /reports/{report_id}/comments`
- **Authentication**: Requires valid Supabase session (`Depends(get_authenticated_context)`).
- **CSRF Protection**: Double-submit token required on cookie-authenticated requests (`Depends(require_csrf)`).
- **Payload Formats**:
  - `application/json`: `{"body": "string", "hide_name": false}`
  - `application/x-www-form-urlencoded`: `body=string&hide_name=1`
- **Validation**:
  - Non-empty, trimmed comment text (1 to 1000 characters). Empty comments return `400 Bad Request`.
  - Report existence verified (missing report returns `404 Not Found`).
  - Privacy flag (`hide_name`): when `True`, displays as `"Community Member"` and redacts `user_id`.
- **Success Response (`201 Created`)**: Returns created `CommentResponse`.

---

## Security & Architecture Verification

- **Authenticated RLS Context**: All database inserts and queries run through `create_authenticated_supabase_client(access_token, cfg)`. The service-role key (`SUPABASE_SERVICE_KEY`) is never used.
- **Double-Submit CSRF**: Protected by constant-time verification against `X-CSRF-Token` headers and `sr_csrf_token` cookies.
- **Privacy Compliance**: No private email, password, or auth metadata is ever exposed in comment payloads or vote responses.
- **Database Triggers**: Native database triggers compute `severity_score`, `confidence`, and `status = 'fixed'` automatically without trusting client computations.

---

## Verification Summary

### Automated Test Suite
- Command: `python -m pytest tests/ -v`
- Result: **131 passed, 2 skipped, 0 failed**.
- Includes 13 dedicated unit/integration tests in [tests/test_stage7_a9_voting_comments.py](../tests/test_stage7_a9_voting_comments.py).
