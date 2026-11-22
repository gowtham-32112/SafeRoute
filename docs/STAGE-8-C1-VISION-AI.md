# Stage 8: Vision AI Category Suggestion (C1 / C2)

## 1. Overview & Mission

In Stage 8, SafeRoute integrates AI Vision capabilities to streamline crowdsourced road-hazard reporting. When a citizen uploads an evidence photograph on `/reports/new`, the system non-intrusively suggests the most likely hazard category (e.g., Pothole, Road break, Streetlight, Footpath, or Other hazard) using Google's Gemini Vision API (`gemini-2.5-flash`), with 100% privacy protection and fail-safe fallbacks.

**Assigned Issues**:
- Issue #28: `C1 · Vision AI hazard category suggestion API and report form integration`
- Assignee: `kommukuriabhinav-crypto` (Member C — AI, Routing & Analytics)

---

## 2. Architecture & Design Principles

```
  +------------------+         +----------------------------+
  |  Citizen Browser |         | SafeRoute Backend (FastAPI)|
  |  (/reports/new)  |         +----------------------------+
  +--------+---------+                       |
           | 1. Selects Photo                |
           |    (JPEG/PNG/WebP, <=5MB)       |
           |                                 |
           | 2. POST /reports/suggest-category
           |    [Multipart + CSRF Token]     |
           +-------------------------------->|
                                             | 3. Strip 100% EXIF & GPS
                                             |    Metadata (Pillow)
                                             |
                                             | 4. Call Gemini 2.5 Flash
                                             |    (Strict JSON schema,
                                             |     timeout <= 8.0s)
                                             |
                                             | 5. Normalize category &
                                             |    clamp confidence
                                             |<------------------------+
           | 6. HTTP 200 JSON                |
           |    {"category": "pothole", ...} |
           |<--------------------------------+
           |
           | 7. Display Suggestion Chip
           |    "Apply suggestion"
           v
```

### Key Pillars:
1. **User Privacy Guaranteed**: All EXIF metadata (GPS coordinates, camera model, date/time) is stripped in memory before the image is transmitted to the AI provider. Live user coordinates are never sent.
2. **Strict Non-Blocking Fallback**: Road-hazard reporting must never fail due to AI issues. If `GEMINI_API_KEY` is unset, the network times out, upstream returns HTTP 5xx, or an unrecognizable image is submitted, the endpoint returns HTTP 200 with `{"category": null, "confidence": null, "reason": "..."}`. The user can manually select any category without hindrance.
3. **Strict Category Normalization**: The model output is strictly normalized to the 5 locked SafeRoute hazard categories (`pothole`, `road_break`, `streetlight`, `footpath`, `other`) or `null`. Unapproved categories or "none" are mapped to `null`.
4. **Light-Theme Design System & Accessibility**: The suggestion UI adheres to SafeRoute's locked color palette (`#106EBE` primary, `#E6F1FA` primary-soft, `#0F2A43` ink) with rounded-xl cards and rounded-full chips. Zero mint on light surfaces.

---

## 3. Configuration

Configured via environment variables in `app/config.py`:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | `""` | Google Gemini API key (supports fallback to `GOOGLE_API_KEY`) |
| `VISION_AI_MODEL` | `gemini-2.5-flash` | Gemini model variant for vision classification |
| `VISION_AI_TIMEOUT_SECONDS` | `8.0` | Upstream HTTP timeout for the vision inference call |

---

## 4. API Specification

### `POST /reports/suggest-category`

- **Authentication**: Required (`create_authenticated_supabase_client` session context).
- **CSRF**: Double-submit CSRF protection enforced via `X-CSRF-Token` header.
- **Content-Type**: `multipart/form-data`
- **Payload**:
  - `image`: Uploaded image file (JPEG, PNG, WebP; max 5 MB).

#### Response Schema (`CategorySuggestionResponse`):
```json
{
  "category": "pothole",
  "confidence": 0.94,
  "reason": "Deep crater with cracked asphalt perimeter."
}
```

*When no category could be recognized or AI is unavailable*:
```json
{
  "category": null,
  "confidence": null,
  "reason": "AI category suggestion unavailable."
}
```

---

## 5. Frontend Integration (`/reports/new`)

- **Automatic Trigger**: When a valid evidence photo is selected or dropped onto the dropzone, a background `fetch()` request is initiated via `AbortController`.
- **Analyzing State**: Displays an unobtrusive loading spinner with `"Analyzing photo for hazard category..."` above the category picker.
- **Ready State**: Displays a soft-blue card (`#sr-ai-suggestion-slot`) with the suggested category, confidence percentage badge, concise one-line reason, and an `"Apply suggestion"` button.
- **User Action**: Clicking `"Apply suggestion"` selects the corresponding category card and shows a notification toast.
- **Cancellation**: Removing the photo cancels in-flight requests and clears the suggestion slot.

---

## 6. Verification & Test Coverage

- **Pytest Suite**: 18 comprehensive tests in `tests/test_stage8_c1_vision_ai.py` covering:
  - Unauthenticated 401 and CSRF 403 checks.
  - Multipart validation (missing image 400, non-image 415, oversized 413).
  - EXIF stripping verification before upstream transmission.
  - JSON and markdown code block parsing from Gemini responses.
  - Unknown/unapproved category handling.
  - Upstream timeout / 500 / network exception resilience.
  - Frontend DOM and JavaScript contract checks.
  - Static security rule enforcement (no `SUPABASE_SERVICE_KEY` in runtime code).
- **Live Headless Chrome CDP Verification**:
  - Tested across 3 viewports: Desktop (1280px), Tablet (768px), and Mobile (375px).
  - 0 horizontal overflow (`scrollWidth <= clientWidth`).
  - 0 console errors and 0 page exceptions.
