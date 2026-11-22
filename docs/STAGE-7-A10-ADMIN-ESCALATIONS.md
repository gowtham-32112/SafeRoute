# SafeRoute Stage 7 — A10 / B7: Admin Escalations API & Dashboard

## 1. Overview & Objective
- **GitHub Issue**: [#26 – A10 · Admin escalations API and dashboard (/admin/escalations)](https://github.com/gowtham-32112/SafeRoute/issues/26)
- **Assignee**: `gowtham-32112`
- **Milestone**: Day 1 – Core loop
- **Dedicated Feature Branch**: `feat/admin-26-escalations-dashboard`

SafeRoute operates on a transparent community awareness model: hazards with sufficient community consensus (`confirm_count >= 5`) or high danger (`severity_score >= 70`) are automatically recommended for escalation (`escalation_recommended = true`). Authorized municipal administrators review these hazards on the `/admin/escalations` dashboard and officially record escalations to assigned government authorities (e.g. GHMC Circle Teams, Traffic Police).

---

## 2. Architecture & Access Control

### 2.1 Admin Dependency (`app/deps.py`)
- `require_admin_context`:
  - Validates active Supabase session token.
  - Verifies user profile role is strictly `'admin'` (`ctx.user.is_admin == True`).
  - Unauthenticated browser requests receive HTTP 303 Redirect to `/login`.
  - Unauthenticated API requests receive HTTP 401 Unauthorized.
  - Authenticated ordinary users (`role == 'user'`) receive HTTP 403 Forbidden.
  - Returns `AuthenticatedContext` (with `user` and `access_token`).

### 2.2 Strict User RLS Context (Zero Service Role Key)
- All database operations in `app/services/admin.py` exclusively invoke:
  `create_authenticated_supabase_client(access_token, app_settings)`
- Queries run under the caller's JWT, governed by Supabase PostgREST Row Level Security policies:
  - `escalations_insert_admin`: requires `public.is_admin() AND escalated_by = auth.uid()`
  - `hazard_reports_update_admin`: requires `public.is_admin()`
- `SUPABASE_SERVICE_KEY` / `service_role` is **NEVER** used in application runtime code.

---

## 3. Endpoints & Data Contracts

### 3.1 Dashboard & Candidate Queue: `GET /admin/escalations`
- **Content Negotiation**:
  - Browser requests (`Accept: text/html`): Renders `admin_escalations.html` with CSRF cookie, KPI metrics, candidate queue, recently escalated audit list, and action modal.
  - API clients (`Accept: application/json`): Returns `EscalationsDashboardResponse` JSON model.
- **Payload (`EscalationsDashboardResponse`)**:
  - `pending_count` (int): Number of open candidate reports awaiting review.
  - `high_severity_count` (int): Number of candidates with `severity_score >= 70`.
  - `total_escalated_count` (int): Total count of logged escalations.
  - `candidates` (list of `EscalationCandidateReport`): Open reports sorted by `severity_score DESC, created_at DESC`.
  - `recent_escalations` (list of `EscalatedReportItem`): Audit trail of escalated hazards.

### 3.2 Authoritative Escalation Action: `POST /reports/{report_id}/escalate`
- Protected by `require_admin_context` and double-submit CSRF `require_csrf`.
- Alias endpoint: `POST /admin/escalations/{report_id}`.
- Validates:
  - Report UUID format (HTTP 400 on malformed input).
  - Report existence (HTTP 404 if not found).
  - Authority name: 1..150 characters, non-empty when trimmed.
  - Escalation note: 1..1000 characters, non-empty when trimmed.
  - Report status: Rejects already `fixed` (HTTP 400) or already `escalated` (HTTP 409) reports.
- **State Changes**:
  1. Inserts audit row into `public.escalations` (`report_id`, `escalated_by`, `authority_name`, `note`, `created_at`).
  2. Updates `public.hazard_reports` (`status = 'escalated'`, `escalated_at = NOW()`).
  3. Database trigger `log_hazard_status_change()` automatically appends transition to `public.status_history`.
- Returns `EscalateReportResponse`.

---

## 4. Frontend UI & Accessibility

### 4.1 Page Layout & Design System (`admin_escalations.html`)
- Extends `base.html`.
- Light-theme only (`color-scheme: only light;`, zero `dark:` classes).
- Plus Jakarta Sans typography.
- Mint (`#0FFCBE`) restricted strictly to dark header surfaces and badge fills with dark ink text.
- Responsive design: Desktop (`1280px`), Tablet (`768px`), and Mobile (`375px`).

### 4.2 Interactive Components (`admin_escalations.js`)
- `window.srAdminEscalations()` controller.
- **Accessible Modal (`role="dialog"`, `aria-modal="true"`)**:
  - Managed by Alpine.js with backdrop blur.
  - Closes on `Escape` key or click outside.
  - First input automatically focused on open.
- **Live State Reactivity**:
  - Escalating a report moves it immediately from the Pending Queue into the Recently Escalated audit list.
  - KPI counters update optimistically in real time.
  - Feedback toast confirms assignment without full-page reload.
  - Zero `innerHTML` usage (safe DOM text bindings and Alpine `x-text` only).

---

## 5. Verification Summary

### 5.1 Pytest Suite
- **Total Tests**: **148 passed**, 2 skipped (database PG triggers), 0 failures.
- **New Tests**: `tests/test_stage7_a10_admin_escalations.py`:
  - `test_unauthenticated_requests_rejected`: PASSED
  - `test_non_admin_authenticated_user_rejected_with_403`: PASSED
  - `test_csrf_protection_on_escalate`: PASSED
  - `test_invalid_uuid_returns_400`: PASSED
  - `test_nonexistent_report_returns_404`: PASSED
  - `test_reject_escalating_already_fixed_or_escalated_reports`: PASSED
  - `test_empty_authority_name_or_note_rejected`: PASSED
  - `test_get_admin_escalations_dashboard_json_and_html`: PASSED
  - `test_successful_hazard_escalation`: PASSED
  - `test_form_urlencoded_escalation`: PASSED
  - `test_static_security_and_design_rules`: PASSED

### 5.2 Headless Chrome CDP Verification
- **1280px Desktop**: 0 horizontal overflow (`scrollWidth <= innerWidth`), KPI strip, candidate cards, and audit trail verified.
- **768px Tablet**: 0 horizontal overflow, responsive layout verified.
- **375px Mobile**: 0 horizontal overflow, touch targets $\ge 44\text{px}$ verified.
- **Interactive Escalation Flow**:
  - Modal opened via "Mark as Escalated" button.
  - Inputs filled (`GHMC Road Maintenance Circle 4` + audit note).
  - Escalation recorded: candidate removed from queue, KPI counters updated (`pending: 0`, `total: 2`), toast rendered, and entry appended to audit trail.
  - **Console Errors**: `0` across all runs.
