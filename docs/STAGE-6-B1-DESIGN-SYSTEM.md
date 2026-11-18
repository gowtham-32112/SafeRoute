# SafeRoute — Stage 6, Issue #10 (B1 · Design System and Base Layout)

**Issue**: `#10` (`B1 · Design system and base layout`)
**Branch**: `feat/frontend-10-design-system-base-layout`
**Scope**: Locked SafeRoute design tokens, light-theme base HTML5 template, role-aware responsive navigation bar, accessible toast system, reusable Jinja2 UI macros, and required loading/empty/error UX states.

---

## 1. Design Tokens & Typography

All tokens strictly match `docs/SPEC.md` Section 5 and are defined in both the Tailwind CSS CDN config in [app/templates/base.html](../app/templates/base.html) and CSS custom properties in [app/static/css/saferoute.css](../app/static/css/saferoute.css).

### Brand Palette (Light Theme Only)

| Token | Tailwind Key | CSS Variable | Hex | Usage |
| :--- | :--- | :--- | :--- | :--- |
| Primary | `primary` | `--sr-primary` | `#106EBE` | Buttons, links, active states, navbar header |
| Primary dark | `primary-dark` | `--sr-primary-dark` | `#0B4F8A` | Hover states, headings on light backgrounds |
| Primary soft | `primary-soft` | `--sr-primary-soft` | `#E6F1FA` | Tinted backgrounds, selected chips |
| Accent (mint) | `accent` | `--sr-accent` | `#0FFCBE` | Accents on **dark blue only**, badge fill with dark `ink` text, focus ring |
| Ink | `ink` | `--sr-ink` | `#0F2A43` | Main text |
| Muted | `muted` | `--sr-muted` | `#5B6B7C` | Secondary text, captions |
| Surface | `surface` | `--sr-surface` | `#FFFFFF` | Card and control backgrounds |
| Canvas | `canvas` | `--sr-canvas` | `#F6FAFD` | Page body background |
| Border | `border` | `--sr-border` | `#D9E4EE` | Borders and card dividers |

### Severity Palette (Always Paired With Text Labels)

| Level | Tailwind Key | CSS Variable | Hex | Text Contrast Treatment |
| :--- | :--- | :--- | :--- | :--- |
| Low | `severity-low` | `--sr-severity-low` | `#2E9E5B` | Indicator dot `#2E9E5B` + AA+ dark green text `#1B6B3B` on `#EBF7F0` |
| Medium | `severity-medium` | `--sr-severity-medium` | `#F5A524` | Indicator dot `#F5A524` + AA+ dark amber text `#8A5300` on `#FEF6E7` |
| High | `severity-high` | `--sr-severity-high` | `#D92D20` | Indicator dot `#D92D20` + AA+ dark red text `#991B1B` on `#FDECEB` |

### Typography & Shape Rules
- **Font**: Google Fonts `Plus Jakarta Sans` (`400`, `500`, `600`, `700`, `800`), `display=swap`. Headings `700–800`, body `400–500`.
- **Theme**: Light theme only (`<meta name="color-scheme" content="light only">`, `color-scheme: light only;`, zero `dark:` classes).
- **Mint Contrast Guard**: Mint (`#0FFCBE`) is never used as text on white or light surfaces.
- **Shapes**: `12px` card radius (`rounded-card`), `999px` pill chip/button radius (`rounded-full` / `rounded-chip`), subtle elevation shadows (`shadow-card`, `shadow-elevated`).

---

## 2. Base Template Structure

[app/templates/base.html](../app/templates/base.html) provides the semantic HTML5 layout extended by all future SafeRoute pages:

- **Script Load Order**: Tailwind CSS CDN + inline `tailwind.config` (head) -> `htmx.org@2.0.4` (`defer`) -> `/static/js/saferoute.js` (`defer`, registers `alpine:init` toast manager & HTMX CSRF header bridge) -> `alpinejs@3.14.8` (`defer`).
- **Child Template Blocks**:
  - `title`, `meta_description`, `head_extra`
  - `navbar` (defaults to `components/navbar.html`)
  - `main_class`, `content` (inside `<main id="main-content" tabindex="-1">`)
  - `footer` (includes SafeRoute road-hazard awareness disclaimer)
  - `scripts_extra`

---

## 3. Navigation Behaviour

[app/templates/components/navbar.html](../app/templates/components/navbar.html) reads the Stage 5 `current_user` (`AuthenticatedUser`) without modifying backend authentication:

- **Anonymous (`current_user` is `None`)**: Renders SafeRoute brand plus `Log in` (`/login`) and `Sign up` (`/signup`) only. App routes (`Map`, `Report`, `Routes`, `Analytics`, `Escalations`) are not exposed.
- **Authenticated User (`role="user"`)**: Renders `Map` (`/map`), `Report` (`/reports/new`), `Routes` (`/routes`), `Analytics` (`/analytics`), user display name, and a CSRF-protected `POST /auth/logout` form (`csrf_token` sourced from context or `sr_csrf_token` cookie). `Escalations` is not rendered.
- **Admin User (`role="admin"` / `is_admin=True`)**: Additionally renders `Escalations` (`/admin/escalations`) and an `Admin` mint pill (`bg-accent text-ink`).
- **Active State**: Driven by `active_nav` context variable or `request.url.path`, setting `aria-current="page"` and an active indicator bar on dark blue.
- **Mobile Navigation**: Alpine.js collapsible drawer (`#sr-mobile-nav`) with accessible toggle (`aria-controls`, `aria-expanded`, `aria-label`) and Escape-key dismissal, usable down to `375px` without horizontal overflow.

---

## 4. Reusable Macros & UX States

### UI Macros ([app/templates/components/ui.html](../app/templates/components/ui.html))
- `button(label, variant, type, href, size, disabled, loading, full_width, aria_label, extra_classes, attrs)`: Supports `primary`, `secondary`, `ghost`, `danger`, `accent` (mint fill + dark `ink` text), explicit button `type`, link mode (`href`), loading spinner (`aria-busy="true"`), and `>= 44px` default touch targets.
- `card(title, subtitle, heading_level, padding, extra_classes, attrs)`: `12px` rounded surface card supporting `{% call card(...) %}` body blocks.
- `chip(label, active, href, type, aria_label, extra_classes, attrs)`: Pill-shaped toggle/link chip with `aria-pressed` or `aria-current`.
- `severity_badge(level, score, show_score, extra_classes)`: Always displays explicit text label (`Low`, `Medium`, `High`), colour dot, optional score, and `aria-label`.
- `badge(label, variant, extra_classes)`: Reusable status/category pill (`neutral`, `primary`, `mint`, `dark`, `escalated`, `fixed`).
- `input_field(...)`, `textarea_field(...)`, `select_field(...)`: Linked `<label for="...">`, required marker, `aria-invalid`, `aria-describedby` wiring for help/error messages, and `role="alert"` inline errors.

### Required UX States ([app/templates/components/states.html](../app/templates/components/states.html))
- `loading_state(...)`: Accessible `role="status"`, `aria-live="polite"`, `aria-busy="true"`, reduced-motion-safe spinner, and human-readable status text.
- `empty_state(...)`: Human-readable empty state with civic awareness limitation reminder and optional action button/slot.
- `error_state(...)`: Accessible `role="alert"`, clear error heading/message without stack traces, and optional retry control.

---

## 5. Toast Notification System

[app/templates/components/toasts.html](../app/templates/components/toasts.html) + [app/static/js/saferoute.js](../app/static/js/saferoute.js):

- Supports `success`, `error`, `warning`, and `info` types, capped at 4 simultaneous visible items so mobile (`375px`) screens stay unobstructed.
- **Sources**: Server-rendered `toasts` or `messages` template context (safely JSON-escaped in `#sr-initial-toasts`), client calls (`window.SafeRouteToast.success(...)` / `saferoute:toast` CustomEvent), and HTMX `HX-Trigger: {"saferoute:toast": ...}` headers.
- **Accessibility & Timing**: Separate `role="status" aria-live="polite"` (success/info) and `role="alert" aria-live="assertive"` (error/warning) regions; auto-dismiss (`5 s` success/info, `7 s` warning, `8 s` error), pauses on hover/focus, dismissible via close button or Escape key.
- **HTMX CSRF Helper**: `saferoute.js` also registers an `htmx:configRequest` listener that attaches `X-CSRF-Token` from the `sr_csrf_token` cookie on non-GET HTMX requests.

---

## 6. Accessibility Considerations

- Skip-to-main-content link (`.sr-skip-link`) targeting `<main id="main-content" tabindex="-1">`.
- High-visibility `:focus-visible` ring (`3px` `#0FFCBE` outline + `5px` dark-blue outer ring) legible on both light and dark blue surfaces.
- Semantic landmarks (`<header>`, `<nav aria-label="...">`, `<main>`, `<footer>`) and logical heading hierarchy (`heading_level` parameter on cards and states).
- Explicit `type="button"` / `type="submit"` on all `<button>` elements, `prefers-reduced-motion: reduce` support, and text labels on every severity indicator and toast notification.

---

## 7. Verification & Known Limitations

### Automated Tests
- Executed `python -m pytest tests/ -v` including [tests/test_stage6_b1_design_system.py](../tests/test_stage6_b1_design_system.py), verifying static asset serving, Spec Section 5 tokens, mint contrast & light-only rules, role-based navbar rendering (anonymous, ordinary user, admin), toast XSS escaping & live regions, all UI macros, and loading/empty/error UX states alongside all existing Stage 1–6 tests.

### Known Limitations
- Because B1 is strictly scoped to the design system and base layout (no page routes or `/login`, `/map`, `/reports/new` views yet), interactive end-to-end page journeys are implemented in subsequent issues (`B2`–`B5`).
- External CDN resources (Google Fonts, Tailwind CSS CDN, HTMX, Alpine.js) require internet access in the browser when rendered live; offline environments will fall back to system sans-serif fonts and the base CSS tokens in `saferoute.css`.
- The two pre-existing local PostgreSQL integration tests in [tests/test_stage4_pg_integration.py](../tests/test_stage4_pg_integration.py) remain skipped locally because `SAFEROUTE_TEST_PG_DSN` is not configured.
