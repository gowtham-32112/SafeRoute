# Stage 6 – B5: Report Form (`/reports/new`, Issue #14)

## Overview

This document summarizes the implementation and verification of **Issue #14 (`B5 · Report form`)** on top of the approved B1 design system, B2 landing/auth pages, B3 interactive map/location workspace, B4 hazard markers/filters, and A8 hazard reporting/duplicate-detection backend.

All changes strictly follow the locked SafeRoute stack (FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind CSS CDN + Leaflet + OpenStreetMap) and introduce **zero database schema changes**, **zero modifications** to A8 backend contracts or services, and **zero scope creep** into C1/C2 Vision AI models, B6 report detail view, or admin endpoints.

---

## Files Added and Updated

- [app/routers/reports.py](../app/routers/reports.py) — Adds the `GET /reports/new` template route protected by `get_current_user` (unauthenticated browser requests redirect with 303 to `/login`, API calls receive 401). Registered **strictly before** `GET /reports/{report_id}` to avoid UUID path collisions.
- [app/templates/report_new.html](../app/templates/report_new.html) — Full Jinja2 page extending `templates/base.html` containing the prominent driving safety banner, accessible 5-category radiogroup, reserved AI category suggestion slot, photo dropzone with preview, Leaflet interactive pin-picker map, description/landmark fields, and accessible duplicate-found modal.
- [app/static/js/report_new.js](../app/static/js/report_new.js) — Modular Alpine.js component (`srReportForm`) handling reactive state, client-side photo validation (<= 5 MB, JPEG/PNG/WebP), Leaflet draggable pin map, browser geolocation with 5-second timeout and fallback to Kukatpally Y Junction (`17.4849, 78.4138`), duplicate checking via `POST /reports/check-duplicate`, duplicate confirmation via `POST /reports/confirm-duplicate`, multipart submission via `POST /reports`, and safe external hook `window.SafeRouteReportForm.setAiSuggestion(...)`.
- [app/static/css/saferoute.css](../app/static/css/saferoute.css) — Scoped styling for `.sr-category-card`, `.sr-category-card--selected`, `.sr-photo-dropzone`, `.sr-picker-canvas`, and `.sr-modal-backdrop` adhering strictly to light theme (`color-scheme: only light;`, zero `dark:` classes) and the mint contrast rule (no mint text on light surfaces).
- [tests/test_stage6_b5_report_form.py](../tests/test_stage6_b5_report_form.py) — Automated test suite covering route ordering, access control & redirect, acceptance markup & accessibility roles, static security/design guardrails, and CSRF protection.
- [docs/STAGE-6-B5-REPORT-FORM.md](./STAGE-6-B5-REPORT-FORM.md) — This documentation file.

---

## Implemented Capabilities

### 1. Mandatory Driving Safety Warning
- Prominently rendered at the top of the form layout:
  > *"Never photograph hazards while driving or riding. Stop safely first."*
- Displayed with an amber caution shield icon and high-contrast alert styling for maximum user visibility.

### 2. Category Selection & Reserved AI Suggestion Slot
- 5 Spec-defined categories with icons and descriptive helper text:
  - `pothole` (Pothole)
  - `road_break` (Road Break)
  - `streetlight` (Streetlight Out)
  - `footpath` (Damaged Footpath)
  - `other` (Other Road Hazard)
- Formatted as an accessible radiogroup (`role="radiogroup"`) with keyboard navigation and clear active selection states.
- **Reserved AI Suggestion Slot** (`#sr-ai-suggestion-slot`): Ready for Stage 6 / C2 integration. Controlled via `window.SafeRouteReportForm.setAiSuggestion({ category, confidence, reason })`. Provides an accessible one-click **"Apply suggestion"** button that auto-selects the predicted category card.

### 3. Photo Upload Dropzone with Client-Side Validation
- Interactive drag-and-drop zone with click-to-browse file input.
- Enforces strict client-side validation:
  - Allowed MIME types: `image/jpeg`, `image/png`, `image/webp`.
  - Maximum file size: **5 MB**. Files exceeding 5 MB are rejected immediately with a user-friendly error message before upload.
- Live thumbnail preview with file details (name and size) and a **"Remove photo"** button that cleanly resets the input.

### 4. Interactive Pin-Picker Map with Fallback
- Encapsulated Leaflet map (`#sr-picker-map`) with OpenStreetMap tiles and a draggable location marker.
- Clicking on the map or dragging the marker updates the form's `lat` and `lng` coordinates in real time.
- **"Use my current location"** button uses `navigator.geolocation.getCurrentPosition` with a **5-second timeout**.
- If geolocation is denied, times out, or fails, the map centers on the default fallback coordinates at **Kukatpally Y Junction (`17.4849, 78.4138`)** with an informative banner.

### 5. Duplicate Hazard Detection & Modal
- Before final submission, triggers `POST /reports/check-duplicate` (20-meter radius check).
- If a matching duplicate is found within 20m, displays the accessible `#sr-duplicate-modal` with existing hazard details (category, distance in meters, existing confirmation count).
- Offers the user two clear Spec-compliant options:
  1. **"Same hazard, add my confirmation"**: Calls `POST /reports/confirm-duplicate`, increments existing verification, and redirects to map or detail page.
  2. **"Different, submit anyway"**: Bypasses the warning and submits the new report via multipart `POST /reports`.

### 6. Multipart Form Submission & Error Handling
- Builds `FormData` with `category`, `lat`, `lng`, optional `description`, optional `landmark`, and `image` file.
- Sends payload to `POST /reports` with CSRF protection header (`X-CSRF-Token`).
- Manages clear UI states:
  - **Submitting state**: Disables submit button, displays loading spinner, and updates text to *"Submitting report..."*.
  - **Error state**: Displays accessible alert banner with specific error guidance.
  - **Success state**: Redirects user to `/map` (or `/reports/{id}` once detail view is available).

### 7. Route Ordering & Access Control
- In `app/routers/reports.py`, `GET /reports/new` is registered **strictly prior** to `GET /reports/{report_id}` so that the string `"new"` is never evaluated as a UUID.
- Enforces authenticated access via `Depends(get_current_user)`: unauthenticated browser navigation redirects with `303 See Other` to `/login?next=%2Freports%2Fnew`.

---

## Security & Privacy Safeguards

- **Location Privacy**: The pin-picker map only captures the chosen hazard location. It never logs, tracks, or stores the user's ongoing physical coordinates in `localStorage` or `sessionStorage`.
- **EXIF Stripping**: Verified backend pipeline (A8 `sanitize_image`) strips all EXIF metadata from uploaded images before storage.
- **XSS Prevention**: All dynamic text (error messages, duplicate details, AI suggestion text) is rendered using safe DOM APIs (`document.createElement` + `.textContent`) and Alpine `x-text` bindings; zero `innerHTML`.
- **CSRF Protection**: All state-changing requests (`POST /reports`, `POST /reports/check-duplicate`, `POST /reports/confirm-duplicate`) send and validate double-submit CSRF cookies and headers.
- **Design System Fidelity**: Strictly light-themed (`color-scheme: only light;`, zero `dark:` utility classes) and strictly adheres to high-contrast rules (mint is never placed on light surfaces).

---

## Verification Summary

### Automated Pytest Suite
- Command: `python -m pytest tests/ -v`
- Result: **118 passed, 2 skipped, 0 failed**.
- Includes 4 comprehensive B5 unit/integration tests in [tests/test_stage6_b5_report_form.py](../tests/test_stage6_b5_report_form.py).

### Live Headless Chrome Verification (7-Stage Test Across 3 Viewports)
Executed against the live FastAPI application served via Uvicorn with Chrome DevTools Protocol:
1. **Initial Load**: Authenticated page loads successfully with 200 OK at `/reports/new`.
2. **Safety Warning**: Driving safety banner is prominent and visible.
3. **Category Selection & AI Hook**: Selecting category cards updates form state; calling `window.SafeRouteReportForm.setAiSuggestion` successfully renders recommendation and "Apply suggestion" button selects category.
4. **Interactive Map Pin & Geolocation**: Map loads, marker is draggable, clicking map updates coordinates, and "Use my location" button updates coordinates cleanly.
5. **Photo Dropzone & File Cap**: Valid image loads preview; file > 5 MB triggers immediate client-side error and resets file input.
6. **Duplicate Detection & Confirmation**: 20m duplicate check opens modal; confirming duplicate triggers confirmation flow.
7. **Submit Anyway & Multipart Submission**: Submitting unique hazard sends multipart form data to `POST /reports` and redirects cleanly.
8. **Responsive Viewport Audit**: Verified at **1280px (Desktop)**, **768px (Tablet)**, and **375px (Mobile)** with **0 horizontal overflow**, **0 console errors**, and **0 uncaught exceptions**.

---

## Mocked vs. Live Integration Notes

- **Verified Locally**: Frontend layout, validation, interactions, CSRF tokens, and mock backend route handling.
- **Live Supabase Verification Post-Merge**:
  - Live Supabase Storage bucket (`hazard-photos`) image upload and public URL generation.
  - Live PostGIS spatial duplicate detection RPC (`check_duplicate_hazard`).
