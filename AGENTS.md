# AGENTS.md – SafeRoute AI Development Instructions

> **Notice to AI Agents (Antigravity & Subagents)**:
> This document governs all architectural decisions, coding patterns, workflows, and quality standards for the **SafeRoute** repository. You MUST adhere strictly to these rules alongside `docs/SPEC.md`. Never deviate, invent new frameworks, or skip verification steps.

---

## 1. Project Overview

- **Project Name**: SafeRoute
- **Mission**: An AI-powered Community Road-Hazard Awareness Platform providing crowdsourced road-hazard reporting, interactive map visualization, community verification, AI visual categorization, and hazard-aware route planning.
- **Critical Disclaimer**: SafeRoute provides **awareness only** and does not repair road hazards or guarantee safety.
- **Team Model**: Three-member full-stack engineering team:
  - **Member A**: Backend & Database (Supabase, PostGIS, Auth, Storage, Core APIs, Deploy)
  - **Member B**: Frontend & UI (Jinja2, Tailwind CSS, Alpine.js, HTMX, Leaflet Maps)
  - **Member C**: AI, Routing & Analytics (Vision AI suggestion, OSRM routing & scoring, Analytics, Evaluation)

---

## 2. Locked Technology Stack

Do NOT alter, replace, or add alternatives to this stack:

- **Backend**: Python 3.11, FastAPI, Jinja2 template engine
- **Frontend**: HTML5, HTMX, Alpine.js, Tailwind CSS (via CDN)
- **Database & Backend Services**: Supabase PostgreSQL + PostGIS extension, Supabase Auth, Supabase Storage, Supabase Realtime
- **Maps**: Leaflet.js, OpenStreetMap tiles
- **Routing**: OSRM (`router.project-osrm.org`)
- **Prohibited Technologies**: Absolutely NO React, Next.js, Vue, Angular, Node.js runtime, MongoDB, Firebase, or complex JS build tools (Vite/Webpack). All UI is served via FastAPI Jinja2 + HTMX + Alpine.js.

---

## 3. Architecture Rules

1. **Modular Directory Structure**: Strictly mirror the layout defined in Section 13 of `docs/SPEC.md`:
   - `app/routers/`: HTTP route handlers separated by domain (`auth`, `reports`, `votes`, `comments`, `admin`, `routes`, `analytics`).
   - `app/services/`: Pure business logic and external integrations (`severity`, `duplicates`, `vision`, `routing`, `analytics`).
   - `app/templates/`: Jinja2 templates extending `templates/base.html`.
   - `app/static/`: Static CSS, JS, and image assets.
   - `supabase/`: SQL migration, functions, triggers, policies, and seed data scripts.
   - `tests/`: Automated unit and integration tests.
2. **Separation of Concerns**:
   - Routers handle HTTP request parsing, status codes, and template rendering or JSON output.
   - Services contain pure Python business logic, external API calls, and calculations. No direct Request/Response coupling in services.
   - Database operations interface through Supabase client utilities in `app/deps.py` or dedicated query services.
3. **Stateless Backend**: Sessions are held via secure cookies issued by Supabase Auth; backend services remain stateless.

---

## 4. Python Coding Standards

- **Language Version**: Python 3.11.
- **Typing**: Use standard Python type annotations (`str`, `int`, `float`, `bool`, `list`, `dict`, `tuple`, `Optional`, `Union`) on all function signatures and return types.
- **Formatting & Style**: Follow PEP 8 guidelines. Maximum line length of 100 characters.
- **Docstrings**: Google-style or standard Sphinx docstrings for all non-trivial public functions and classes.
- **Imports**: Cleanly ordered:
  1. Standard library imports (e.g., `os`, `sys`, `typing`)
  2. Third-party library imports (e.g., `fastapi`, `pydantic`, `supabase`)
  3. Local application imports (e.g., `from app.config import settings`)
- **Data Modeling**: Use Pydantic v2 schemas for all request payloads and response models.

---

## 5. FastAPI Development Conventions

- **Lifespan Management**: Use modern `@asynccontextmanager` for FastAPI lifespan events when managing startup/shutdown resources.
- **Dependency Injection**: Utilize `Depends()` for all shared dependencies (settings, database clients, authentication, permissions) defined in `app/deps.py`.
- **Response Format**:
  - Full-page requests render Jinja2 templates via `TemplateResponse`.
  - HTMX partial requests render sub-templates / fragments.
  - API endpoints return validated Pydantic JSON models.
- **Health Endpoint**: Must always expose `GET /health` returning `{"status": "healthy", "application": "SafeRoute"}`.

---

## 6. Frontend Development Conventions

- **HTML5 & Jinja2**: Semantic markup. Every page extends `templates/base.html`.
- **CSS**: Tailwind CSS utility classes loaded via CDN. Use semantic utility classes matching design tokens.
- **Interactivity with HTMX**:
  - Use `hx-get`, `hx-post`, `hx-target`, `hx-swap` for dynamic component updates without full-page reloads.
  - Return partial HTML snippets for HTMX calls (e.g., vote buttons, comments list, duplicate modal).
- **Client State with Alpine.js**:
  - Use Alpine.js (`x-data`, `x-show`, `x-on`) for local client-side state (modals, dropdowns, local tabs, form preview toggles).
- **Leaflet Integration**:
  - Keep map scripts encapsulated in `app/static/js/map.js`.
  - Debounce map panning/zooming (`400 ms`) before querying `reports_in_bounds`.
  - Cluster markers when zoomed out.
- **UX States**: Every interactive page or component MUST handle:
  1. Loading state (spinner or disabled button)
  2. Empty state (helpful message and action prompt)
  3. Error state (user-friendly alert, no cryptic stack traces)
  4. Visible keyboard focus outline

---

## 7. Supabase Integration Rules

- **Client Instantiation**: Initialize client using `SUPABASE_URL` and `SUPABASE_ANON_KEY` for regular user contexts; use `SUPABASE_SERVICE_KEY` strictly for admin-guarded backend operations or background seed operations.
- **PostGIS Points**: All spatial data uses `geography(Point, 4326)`. Coordinates are strictly stored in `(longitude, latitude)` order in PostGIS, converted seamlessly for frontend `[latitude, longitude]`.
- **Storage**: Upload hazard photos and fixed-proof images to bucket `hazard-photos`.
- **Realtime**: Subscribe to `hazard_reports` table changes on `/map` so new/updated reports appear dynamically.

---

## 8. Database Security Requirements

- **Row Level Security (RLS)**:
  - Enabled on ALL tables (`profiles`, `hazard_reports`, `report_images`, `verifications`, `comments`, `escalations`, `status_history`).
  - Anonymous users cannot read or write data.
  - Authenticated users can view reports, images, comments, and create their own reports/votes/comments.
  - Users can ONLY edit or delete their own open reports.
  - Only `admin` role can insert into `escalations`, update report status to `escalated`, or override statuses.
- **Database Integrity**:
  - Prevent self-voting: Users cannot vote on their own reports (enforced by trigger/RLS and API validation).
  - Unique constraint on `verifications(report_id, user_id, vote)`.

---

## 9. API Development Standards

- **Endpoint Naming**: Restful, lowercase, plural nouns (e.g., `/reports`, `/reports/{id}`, `/routes/plan`).
- **Multipart Uploads**: `POST /reports` accepts multipart form data (`category`, `description`, `lat`, `lng`, `image`).
- **Validation**:
  - Latitudes: -90.0 to 90.0
  - Longitudes: -180.0 to 180.0
  - Categories: `pothole`, `road_break`, `streetlight`, `footpath`, `other`
  - Statuses: `open`, `escalated`, `fixed`
- **Error Codes**: Use standard HTTP status codes (`400 Bad Request`, `401 Unauthorized`, `403 Forbidden`, `404 Not Found`, `422 Unprocessable Entity`, `500 Internal Server Error`).

---

## 10. UI Design System

Always use the locked design tokens:

### Color Palette
- `primary`: `#106EBE` (Buttons, links, active states, header)
- `primary-dark`: `#0B4F8A` (Hover states, headings on light background)
- `primary-soft`: `#E6F1FA` (Tinted backgrounds, selected filter chips)
- `accent`: `#0FFCBE` (Mint highlight, badges, focus rings — dark surfaces ONLY)
- `ink`: `#0F2A43` (Main text)
- `muted`: `#5B6B7C` (Secondary text, captions)
- `surface`: `#FFFFFF` (Card backgrounds)
- `canvas`: `#F6FAFD` (Page body background)
- `border`: `#D9E4EE` (Borders, card dividers)

### Severity Colors (Always include text label)
- **Low**: `#2E9E5B`
- **Medium**: `#F5A524`
- **High**: `#D92D20`

### Contrast & Typography Rule
- **NEVER** place mint (`#0FFCBE`) text directly on white/light backgrounds. Mint is reserved for accents on dark blue surfaces, badge fills with dark ink text, or focus rings.
- **Font**: Google Fonts Plus Jakarta Sans (400, 500, 600, 700, 800).
- **Shapes**: Rounded corners (`rounded-xl` / 12px for cards, `rounded-full` / 999px for chips and buttons).

---

## 11. GitHub Collaboration Rules

- **Branch Naming**: Strictly follow `feat/<area>-<issue-number>-short-name` (e.g., `feat/infra-S1-repo-setup`, `feat/backend-8-reports-api`, `feat/frontend-3-map-layout`).
- **One Branch per Issue**: Do not bundle multiple issues into a single branch.
- **Commit Discipline**: Commit small, logical, self-contained commits with descriptive messages.
- **Pull Requests**:
  - Target branch: `main`.
  - Description MUST include `Closes #<issue-number>`.
  - Member A reviews and merges all PRs.
  - Never push directly to `main`.
- **Frequent Synchronization**: Pull `main` before starting every issue. Merge branches at least every 3 hours.
- **No Secrets**: Never commit `.env` or API credentials.
- **Agent Permission Guard**: Antigravity agents MUST NOT execute git push, commit, merge, or PR creation without explicit user permission.

---

## 12. Testing Requirements

- **Test Framework**: `pytest` and `pytest-asyncio`.
- **Scope**:
  - Unit tests for severity score calculation and clamping (`app/services/severity.py`).
  - Unit tests for route score calculation (`app/services/routing.py`).
  - Integration tests for endpoints (`GET /health`, `GET /`, auth checks, API bounds).
- **Verification Before Done**: Run `pytest` and ensure 100% pass rate before marking any issue as done.

---

## 13. Error Handling Standards

- **Graceful Fallbacks**:
  - Geolocation denied/timed out (5s) $\rightarrow$ Centred on Kukatpally Y Junction (`17.4849, 78.4138`).
  - Vision AI failure/timeout $\rightarrow$ Return `{"category": null}`, never crash or block report submission.
  - OSRM route service timeout $\rightarrow$ Serve cached demo route with alert.
- **User Facing Errors**: Clear, constructive messages without exposing internal stack traces.

---

## 14. AI Integration Rules

- **Endpoint**: `POST /reports/suggest-category`.
- **Payload & Model**: Image sent to vision model with strict JSON-only output schema:
  `{"category": "pothole"|"road_break"|"streetlight"|"footpath"|"other"|"none", "confidence": float, "reason": "one sentence"}`
- **Fallback**: On any error, network timeout, or schema parsing failure, return `{"category": null}`.
- **EXIF Stripping**: Always strip all EXIF metadata from uploaded images before storing or sending to vision models to preserve user privacy.

---

## 15. Security and Privacy Requirements

- **Driving Safety Warning**: Report submission form MUST display:
  > *"Never photograph hazards while driving or riding. Stop safely first."*
- **Location Privacy**: Store the hazard location only. Never store or track the user's live position.
- **Anonymity Option**: Allow users to toggle "hide my name" on submitted reports.
- **Route Disclaimer**: Every route view MUST display:
  > *"Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."*
- **Awareness Limitation**: The UI must state that absence of reports in an area does not guarantee it is hazard-free.

---

## 16. Scope Control Instructions

- **Strict Staging**: Build ONLY the current stage and assigned issue.
- **Stage 1 Bounds**: Environment setup, folder tree, documentation (`SPEC.md`, `AGENTS.md`, `README.md`), configuration, base FastAPI app, and `/health` route.
- **Do NOT**:
  - Do NOT create database tables or execute migrations in Stage 1.
  - Do NOT build authentication flows or login forms in Stage 1.
  - Do NOT implement hazard APIs or frontend templates in Stage 1.
  - Do NOT begin Stage 2 until Stage 1 is verified and approved.
