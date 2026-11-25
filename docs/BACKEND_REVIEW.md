# SafeRoute — Backend API & Services Architecture Review

> **Document Scope**: Dedicated technical review of the SafeRoute FastAPI application, ASGI middlewares, dependency injection, session and CSRF security, RESTful endpoints, service layer, and real-time event streaming.
> **Repository Location**: `d:\SafeRoute-2`

---

## 1. Core Backend Stack

- **Python**: 3.11 with strict typing (`typing.Optional`, `Union`, `Literal`, `Dict`, `List`).
- **FastAPI**: Modern ASGI framework running on Uvicorn.
- **Pydantic v2**: Request payload validation, settings management, and response serialization.
- **HTTPX**: Non-blocking asynchronous HTTP client for external integrations.
- **Pillow**: Cryptographic EXIF and GPS metadata stripping for image privacy.
- **Starlette**: Low-level ASGI middlewares, streaming responses, and HTTP exceptions.

---

## 2. Architecture & Directory Structure

```
app/
├── main.py                     # Lifespan context, ASGI RequestSizeLimitMiddleware, exception handlers
├── config.py                   # Pydantic BaseSettings, environment variables, default map coordinates
├── deps.py                     # Dependency injection (Auth, CSRF, Admin checks, Settings)
├── routers/
│   ├── auth.py                 # Authentication and session lifecycle
│   ├── reports.py              # Hazard reporting, duplicate check, detail, bounds, SSE realtime
│   ├── admin.py                # Admin escalations, resolutions, user roles, broadcasts
│   ├── routes.py               # Route planning endpoints
│   ├── analytics.py            # Analytics API endpoints
│   ├── ai.py                   # Commuter civic AI assistant endpoint
│   └── live_issues.py          # Searchable live issues table & moderation
└── services/
    ├── admin.py                # Admin authority escalation and repair logic
    ├── auth.py                 # Supabase Auth client integration
    ├── broadcast.py            # Civic emergency alert manager
    ├── community.py            # Community voting and comment threads
    ├── duplicates.py           # 30-meter duplicate detection logic
    ├── images.py               # 5 MB validation and EXIF stripping
    ├── live_issues.py          # Commuter live issues queries
    ├── realtime.py             # Server-Sent Events (SSE) streaming engine
    ├── reports.py              # Core hazard CRUD and bounding box logic
    └── session.py              # HttpOnly cookies and double-submit CSRF cookies
```

---

## 3. Key Backend Systems & APIs

### 1. Request Size & Anti-Abuse Protection
- `RequestSizeLimitMiddleware` intercepts raw ASGI requests before multipart or JSON parsing.
- Enforces a strict 6 MB request body limit (`MAX_REQUEST_BODY_BYTES`) protecting against unbounded buffering and memory exhaustion attacks.

### 2. Authentication & Session Architecture
- Stateless JWT authentication via Supabase Auth.
- Tokens stored in HttpOnly, SameSite=Lax cookies (`sr_access_token`, `sr_refresh_token`).
- Double-Submit CSRF cookie (`sr_csrf_token`) validated on all mutating requests.

### 3. Hazard Reports Engine
- `POST /reports`: Multipart upload supporting category, description, latitude, longitude, and photo.
- Validates coordinates (-90 to +90 lat, -180 to +180 lng).
- Strips 100% of EXIF/GPS tags before storing in private Supabase bucket `hazard-photos`.
- Inserts spatial PostGIS point with initial severity score calculation.
- `GET /reports`: High-performance bounding box query.
- `GET /reports/realtime`: Server-Sent Events (SSE) pushing live updates to map clients.

### 4. Admin & Live Issues Services
- Role-based authorization (`require_admin` dependency).
- Formal civic escalation tracking with external authority handoff (GHMC, Traffic Police).
- Hazard resolution workflow with optional after-fix proof photos.
- User management and system broadcast publishing.
