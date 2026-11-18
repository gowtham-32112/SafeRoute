# SafeRoute — Stage 6, Issue #11 (B2 · Landing, Login and Signup Pages)

**Issue**: `#11` (`B2 · Landing, login and signup pages`)
**Branch**: `feat/frontend-11-landing-auth-pages`
**Scope**: Public landing page (`GET /`), login page (`GET /login`), signup page (`GET /signup`), and Alpine.js client-side form validation wired to existing Stage 5 `/auth/login` and `/auth/signup` endpoints.

---

## 1. Files Created / Modified

| File | Purpose |
| :--- | :--- |
| [app/templates/index.html](../app/templates/index.html) | Public landing page: hero section, Login/Sign up CTAs, three core feature cards, and safety/awareness disclosure (zero map or protected report content for logged-out visitors) |
| [app/templates/login.html](../app/templates/login.html) | Login page with email/password validation, show/hide toggle, CSRF hidden input, loading spinner, inline error alert, toast integration, and `?confirm_email=1` notice |
| [app/templates/signup.html](../app/templates/signup.html) | Signup page matching Stage 5 `SignupPayload` (`full_name` optional, `email`, `password` min 8 chars) plus client-side password confirmation, loading feedback, inline error alert, and email-confirmation success state |
| [app/static/js/auth_forms.js](../app/static/js/auth_forms.js) | Alpine.js `srLoginForm` and `srSignupForm` controllers submitting strictly to existing `POST /auth/login` and `POST /auth/signup` via same-origin `HttpOnly` cookies + `X-CSRF-Token` |
| [app/main.py](../app/main.py) | Adds HTML content negotiation on `GET /` (preserving JSON for API callers) plus `GET /login` and `GET /signup` routes issuing double-submit CSRF cookies and redirecting authenticated users to `/map` |
| [tests/test_stage6_b2_landing_auth.py](../tests/test_stage6_b2_landing_auth.py) | 7 automated tests covering landing page structure, login/signup rendering, CSRF/cookie compatibility, invalid-submission errors, and security/design guardrails |

---

## 2. Authentication, CSRF & Security Compatibility

- **Zero Duplicate Auth Endpoints**: `login.html` and `signup.html` submit strictly to the existing Stage 5 endpoints (`POST /auth/login` and `POST /auth/signup`).
- **HttpOnly Cookies Preserved**: Browser JavaScript never reads, stores, or touches Supabase access/refresh tokens (`localStorage` and `sessionStorage` are never used).
- **Double-Submit CSRF Preserved**: `GET /login` and `GET /signup` reuse Stage 5's `issue_csrf_cookie()` to set `sr_csrf_token` and populate `<input type="hidden" name="csrf_token">`. `auth_forms.js` attaches `X-CSRF-Token` on `fetch(..., { credentials: "same-origin" })`, preserving `require_auth_csrf` same-origin and constant-time double-submit validation.
- **Exact Backend Contract**: `signup.html` validates password confirmation strictly client-side and sends only `{ full_name, email, password }` to `POST /auth/signup` (matching `SignupPayload`).
- **Anonymous Isolation**: Logged-out visitors on `/` cannot view `/map`, hazard reports, Leaflet, or admin navigation.

---

## 3. Verification & Limitations

- **Automated Tests (`python -m pytest tests/ -v`)**: All 7 B2 tests pass alongside all 96 existing Stage 1–6 tests (**103 passed, 2 skipped, 0 failed**).
- **Live Headless Chrome Verification**: Executed against live local Uvicorn serving `/`, `/login`, and `/signup` across `1280px`, `768px`, and `375px` viewports (zero horizontal overflow, zero console errors, client-side required/invalid validation verified live).
- **Known Limitations**:
  - The 2 pre-existing PostgreSQL integration tests in [tests/test_stage4_pg_integration.py](../tests/test_stage4_pg_integration.py) remain skipped locally because `SAFEROUTE_TEST_PG_DSN` is not configured.
  - `/map` page UI is implemented in Issue `#12` (B3); successful login redirects to `/map` as specified.
  - Live Supabase Auth sign-in/sign-up against the remote Supabase project was not executed in automated tests (Stage 5/B2 tests mock Supabase Auth).
