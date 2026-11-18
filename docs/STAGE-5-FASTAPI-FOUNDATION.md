# SafeRoute — Stage 5: FastAPI Backend Foundation Documentation (Issue #7 / A7)

> - **Document Version**: 1.0.0
> - **Milestone**: Day 1 – Core Loop
> - **GitHub Issue**: `#7` (`A7 · FastAPI skeleton, auth and admin guard`)
> - **Feature Branch**: `feat/backend-7-fastapi-foundation`
> - **Base Commit**: `fad083f834aaf54f0f6ee1b62c9bc6ab6aba0c35` (Stage 4 merge)

---

## 1. Architecture Overview

Stage 5 implements the modular FastAPI authentication, session, Supabase client, and role-based authorization layer without introducing Stage 6 frontend templates, map views, or hazard APIs:

| Module | Responsibility |
|---|---|
| [app/config.py](../app/config.py) | Centralized Pydantic v2 `Settings`, URL/environment normalization, HTTPS `use_secure_cookies` logic, and Supabase configuration checks. |
| [app/services/supabase_client.py](../app/services/supabase_client.py) | Reusable stateless public/anon client (`persist_session=False`), per-request RLS-bound authenticated client (`Authorization: Bearer <token>`), and strictly isolated internal service-role helper. |
| [app/services/session.py](../app/services/session.py) | HTTP-only cookie management (`sr_access_token`, `sr_refresh_token`), double-submit CSRF cookie (`sr_csrf_token`), constant-time CSRF verification, and session cleanup. |
| [app/services/auth.py](../app/services/auth.py) | Pure authentication business logic (email/password signup, login, token validation via Supabase Auth `get_user`, refresh, stateless logout, and trusted `public.profiles.role` lookup). |
| [app/deps.py](../app/deps.py) | FastAPI dependency injection separating Public, Authenticated (`get_current_user`, `get_optional_user`), Admin-Only (`require_admin`), and CSRF-protected (`require_csrf`) endpoints. |
| [app/routers/auth.py](../app/routers/auth.py) | Backend auth routes (`/auth/signup`, `/auth/login`, `/auth/logout`, `/auth/csrf`, `/auth/me`, `/auth/admin-check`). |
| [app/main.py](../app/main.py) | FastAPI lifespan management, router registration, centralized redacted error handlers (`401`, `403`, `404`, `500`, `503`), and public `GET /` and `GET /health` endpoints. |

---

## 2. Supabase Client Setup & RLS Compatibility

1. **Public / User-Facing Auth Client (`get_supabase_client`)**:
   - Uses `SUPABASE_URL` and `SUPABASE_ANON_KEY` (compatible with both `sb_publishable_...` publishable keys and legacy anon keys).
   - Cached via `functools.lru_cache` with `auto_refresh_token=False, persist_session=False` so concurrent FastAPI requests never leak mutable auth state across users.
2. **Per-Request Authenticated Client (`create_authenticated_supabase_client`)**:
   - Binds the caller's validated Supabase access token via `Authorization: Bearer <access_token>` headers for every PostgreSQL/Storage query, guaranteeing Stage 4 RLS policies enforce `auth.uid()` per request.
3. **Isolated Service-Role Client (`_get_isolated_service_client`)**:
   - Kept private inside `app/services/supabase_client.py`, never injected by `get_supabase()` or ordinary route dependencies, and never exposed to client templates or JavaScript.

---

## 3. Environment Configuration

Required variables in local `.env` (never commit `.env` or actual keys; see [.env.example](../.env.example)):

| Variable | Required | Description |
|---|:---:|---|
| `SUPABASE_URL` | Yes | Supabase project HTTPS URL (`https://<ref>.supabase.co`). |
| `SUPABASE_ANON_KEY` | Yes | Supabase publishable/anon key for user-facing auth and RLS queries. |
| `SUPABASE_SERVICE_KEY` | Backend-only | Isolated service-role key (optional in Stage 5; never exposed to clients). |
| `SECRET_KEY` | Yes | Application secret key (minimum 32 random bytes in production). |
| `VISION_API_KEY` | Later stage | Reserved for Stage 6+ Vision AI categorization. |
| `ENVIRONMENT` | Optional | `development` (default), `staging`, or `production`. |
| `COOKIE_SECURE` | Optional | Overrides `Secure` cookie flag (defaults to `False` in `development`, `True` in `staging`/`production`). |
| `COOKIE_SAMESITE` | Optional | `lax` (default), `strict`, or `none`. |

---

## 4. Authentication Flows & Endpoints

Google OAuth remains disabled; Supabase Auth (email + password) is the sole identity provider. No passwords are stored in application tables.

| Route | Access Level | Behavior |
|---|---|---|
| `GET /` | Public | Welcome response (`status: "active"`). |
| `GET /health` | Public | Health check returning `{"status": "healthy", "application": "SafeRoute"}`. |
| `GET /auth/csrf` | Public | Issues `sr_csrf_token` cookie for browser/HTMX forms. |
| `POST /auth/signup` | Public (CSRF-protected for browsers) | Validates Origin/Referer and pre-auth double-submit CSRF token for browser/form/HTMX/cookie requests, registers via Supabase Auth (never sends `role` metadata), rotates CSRF cookie and sets HTTP-only cookies if immediate session is returned, or reports `email_confirmation_required: true`. |
| `POST /auth/login` | Public (CSRF-protected for browsers) | Validates Origin/Referer and pre-auth double-submit CSRF token for browser/form/HTMX/cookie requests, authenticates via Supabase Auth, loads trusted role from `public.profiles`, rotates CSRF cookie and issues HTTP-only session cookies (or `303 -> /map` for HTML forms). Never returns raw tokens in JSON/HTML. |
| `POST /auth/logout` | CSRF-protected (cookies/forms/HTMX) | Validates Origin/Referer and double-submit CSRF token for cookie/form/HTMX requests (pure Bearer API clients exempt), revokes Supabase session via stateless `/auth/v1/logout`, clears auth/CSRF cookies, and returns `200` (or `303 -> /login` for HTML forms). |
| `GET /auth/me` | Authenticated (`get_current_user`) | Validates Supabase JWT (transparently refreshing expired cookie sessions when possible), loads trusted profile, returns `UserPublicResponse`. Redirects unauthenticated browsers (`Accept: text/html` or `HX-Request: true`) to `/login`; returns `401` for API clients. |
| `GET /auth/admin-check` | Admin-Only (`require_admin`) | Enforces `public.profiles.role == 'admin'`. Returns `403 Forbidden` for ordinary users and `401` / `303 -> /login` for unauthenticated callers. |

---

## 5. Session Security, CSRF & Production Secret Validation

- **Cookie Flags**:
  - `sr_access_token` and `sr_refresh_token`: `HttpOnly=True`, `SameSite=Lax`, `Path=/`, `Secure=True` automatically in `production`/`staging` (`Secure=False` on local HTTP `development` so `http://127.0.0.1:8000` works cleanly).
  - `sr_csrf_token`: `HttpOnly=False`, `SameSite=Lax`, strictly separate from auth tokens (never accepted for authentication), rotated upon successful login/signup to prevent pre-auth CSRF fixation.
- **Defense-in-Depth CSRF (`require_auth_csrf` & `require_csrf`)**:
  - **Origin / Referer Check**: Explicitly rejects cross-origin `Origin`/`Referer` headers and `"null"` origins on `POST /auth/login`, `POST /auth/signup`, `POST /auth/logout`, and future state-changing routes; safely falls through to double-submit token validation when both headers are absent.
  - **Pre-Auth & Post-Auth Double-Submit Token**: Constant-time `hmac.compare_digest` verification (`sr_csrf_token` cookie vs `X-CSRF-Token` header or `csrf_token` form field) is enforced on unauthenticated browser form/HTMX login & signup requests as well as authenticated cookie/form/HTMX requests.
  - **Bearer-Token API Compatibility**: Pure non-browser API clients using `Authorization: Bearer <token>` without auth cookies or cross-origin browser headers are not required to maintain browser CSRF cookies.
- **Production `SECRET_KEY` Guard**: `Settings` rejects empty, default (`dev-secret-key-change-in-production`), placeholder, or `< 32`-character `SECRET_KEY` values whenever `ENVIRONMENT` is `production` or `staging`, without echoing the secret in error messages. Local `development` continues working cleanly out of the box.
- **Role Integrity**: Roles are read solely from `public.profiles` under the user's validated JWT. Missing or invalid profile records fail closed with `403 Forbidden`. No admin email or UUID is hardcoded anywhere in application code.
- **Error Redaction**: Global exception handlers return consistent `401`, `403`, `404`, `500`, and `503` JSON responses without stack traces, raw tokens, or credentials.

---

## 6. Local Startup & Test Commands

```powershell
# Start FastAPI locally
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Run full automated test suite
python -m pytest tests/ -v

# Run Stage 5 authentication & RBAC tests only
python -m pytest tests/test_stage5_auth.py -v
```

---

## 7. Known Limitations & Deferred Verification Plan

1. **Mocked Unit Tests for Stage 5**: `tests/test_stage5_auth.py` uses mocked Supabase Auth and PostgREST responses so tests never mutate the live Supabase project. End-to-end live Supabase email/password signup + cookie login testing will be verified once Member B integrates `/login` and `/signup` UI templates or via a dedicated non-production test user.
2. **Carried-Forward Deferred Stage 4 Checks (Still NOT Verified Live)**:
   - Manual Supabase security assertions (`tests/stage4_manual_verification.sql`).
   - PostgreSQL multi-connection concurrency integration tests (`tests/test_stage4_pg_integration.py` — 2 tests skipped locally without `TEST_DATABASE_URL`).
   - Live Storage permission verification on private bucket `hazard-photos` across anonymous, User A, User B, and Admin.
3. **HTML Auth Pages (`/login`, `/signup`)**: Owned by Member B (`Issue B2` / Stage 6); Stage 5 provides the backend redirect target `/login` and backend `/auth/*` endpoints ready for template integration.
