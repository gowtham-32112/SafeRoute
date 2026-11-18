"""Unit and rendering tests for Stage 6 Issue #10 (B1 · Design system and base layout)."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app, templates
from app.services.auth import AuthenticatedUser

ROOT_DIR = Path(__file__).resolve().parent.parent
CSS_PATH = ROOT_DIR / "app" / "static" / "css" / "saferoute.css"
JS_PATH = ROOT_DIR / "app" / "static" / "js" / "saferoute.js"
TEMPLATES_DIR = ROOT_DIR / "app" / "templates"


def _fake_request(path: str = "/map", csrf_cookie: str = "test-csrf-123") -> SimpleNamespace:
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        cookies={"sr_csrf_token": csrf_cookie} if csrf_cookie else {},
    )


def _render_template(source: str, **context) -> str:
    """Compile and render a Jinja2 template snippet using SafeRoute's template environment."""
    tmpl = templates.env.from_string(source)
    return tmpl.render(**context)


# ==============================================================================
# 1. Static Assets & Existing App Health
# ==============================================================================


def test_app_health_root_and_static_design_assets_serve_cleanly():
    client = TestClient(app)

    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json() == {"status": "healthy", "application": "SafeRoute"}

    r_root = client.get("/")
    assert r_root.status_code == 200

    r_css = client.get("/static/css/saferoute.css")
    assert r_css.status_code == 200
    assert "text/css" in r_css.headers["content-type"]
    assert "--sr-primary: #106EBE;" in r_css.text

    r_js = client.get("/static/js/saferoute.js")
    assert r_js.status_code == 200
    assert "srToastManager" in r_js.text
    assert "X-CSRF-Token" in r_js.text


# ==============================================================================
# 2. Design System Tokens, Typography, Light-Only & Mint Contrast Rules
# ==============================================================================


def test_base_template_contains_exact_spec_section5_tokens_fonts_and_script_order():
    html = templates.env.get_template("base.html").render(
        request=_fake_request("/"),
        current_user=None,
    )

    # HTML5 document foundation, light theme only, viewport meta
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert '<html lang="en" class="light">' in html
    assert 'name="viewport" content="width=device-width, initial-scale=1.0"' in html
    assert 'name="color-scheme" content="only light"' in html
    assert 'content="light only"' not in html

    # Plus Jakarta Sans weights 400, 500, 600, 700, 800
    assert "Plus+Jakarta+Sans:wght@400;500;600;700;800" in html

    # All 12 exact Spec Section 5 hex tokens (brand + severity)
    expected_hex_tokens = [
        "#106EBE",  # primary
        "#0B4F8A",  # primary-dark
        "#E6F1FA",  # primary-soft
        "#0FFCBE",  # accent (mint)
        "#0F2A43",  # ink
        "#5B6B7C",  # muted
        "#FFFFFF",  # surface
        "#F6FAFD",  # canvas
        "#D9E4EE",  # border
        "#2E9E5B",  # severity low
        "#F5A524",  # severity medium
        "#D92D20",  # severity high
    ]
    css_text = CSS_PATH.read_text(encoding="utf-8")
    for hex_code in expected_hex_tokens:
        assert hex_code in html, f"Missing token {hex_code} in base.html Tailwind config"
        assert hex_code in css_text, f"Missing token {hex_code} in saferoute.css"

    # Script loading order: Tailwind CDN -> HTMX -> saferoute.js -> Alpine.js
    idx_tailwind = html.index("https://cdn.tailwindcss.com")
    idx_htmx = html.index("htmx.org")
    idx_sr_js = html.index("/static/js/saferoute.js")
    idx_alpine = html.index("alpinejs")
    assert idx_tailwind < idx_htmx < idx_sr_js < idx_alpine

    # Awareness disclaimer in footer
    assert "awareness only" in html.lower()
    assert "does not repair" in html.lower()


def test_mint_contrast_rule_and_light_theme_only_enforced():
    css_text = CSS_PATH.read_text(encoding="utf-8")
    assert "color-scheme: only light;" in css_text
    assert "color-scheme: light only;" not in css_text
    assert "color: #0FFCBE" not in css_text
    assert "color: var(--sr-accent)" not in css_text

    # No dark-mode Tailwind classes anywhere in B1 templates
    for tmpl_path in TEMPLATES_DIR.rglob("*.html"):
        text = tmpl_path.read_text(encoding="utf-8")
        assert "dark:" not in text, f"Unexpected dark-mode class in {tmpl_path.name}"

        # Mint text is prohibited on white/light surfaces (only allowed on dark blue brand icon)
        if tmpl_path.name != "navbar.html":
            assert "text-accent" not in text, f"Forbidden text-accent in {tmpl_path.name}"


def test_child_template_extends_base_html_cleanly():
    rendered = _render_template(
        """
        {% extends "base.html" %}
        {% block title %}Custom Page — SafeRoute{% endblock %}
        {% block content %}
          <h1>Test Heading</h1>
          <p>Body paragraph content.</p>
        {% endblock %}
        """,
        request=_fake_request("/map"),
        current_user=AuthenticatedUser(
            id="11111111-1111-4111-8111-111111111111",
            email="citizen@example.com",
            full_name="Asha Rao",
            role="user",
        ),
    )
    assert "<title>Custom Page — SafeRoute</title>" in rendered
    assert "<h1>Test Heading</h1>" in rendered
    assert "Body paragraph content." in rendered


# ==============================================================================
# 3. Role-Aware Navigation & Responsive Mobile Drawer
# ==============================================================================


def test_navbar_renders_by_authentication_and_role_without_exposing_admin_links():
    # 1. Anonymous visitor: sees Log in & Sign up only; zero app or admin links
    anon_html = templates.env.get_template("base.html").render(
        request=_fake_request("/"),
        current_user=None,
    )
    assert 'href="/login"' in anon_html
    assert 'href="/signup"' in anon_html
    assert 'href="/map"' not in anon_html
    assert 'href="/reports/new"' not in anon_html
    assert 'href="/routes"' not in anon_html
    assert 'href="/analytics"' not in anon_html
    assert 'href="/admin/escalations"' not in anon_html
    assert "Escalations" not in anon_html

    # 2. Ordinary user: sees Map, Report, Routes, Analytics, Logout; NO Escalations
    ordinary_user = AuthenticatedUser(
        id="11111111-1111-4111-8111-111111111111",
        email="user@example.com",
        full_name="Ravi Kumar",
        role="user",
    )
    user_html = templates.env.get_template("base.html").render(
        request=_fake_request("/routes", csrf_cookie="csrf-user-xyz"),
        current_user=ordinary_user,
    )
    assert 'href="/map"' in user_html
    assert 'href="/reports/new"' in user_html
    assert 'href="/routes"' in user_html
    assert 'href="/analytics"' in user_html
    assert "Ravi Kumar" in user_html
    assert 'action="/auth/logout"' in user_html
    assert 'name="csrf_token" value="csrf-user-xyz"' in user_html
    assert 'href="/admin/escalations"' not in user_html
    assert "Escalations" not in user_html
    # Active state marks Routes with aria-current="page"
    assert 'href="/routes"\n              aria-current="page"' in user_html

    # 3. Admin user: sees Map, Report, Routes, Analytics AND Escalations + Admin badge
    admin_user = AuthenticatedUser(
        id="22222222-2222-4222-8222-222222222222",
        email="admin@saferoute.demo",
        full_name="Demo Admin",
        role="admin",
    )
    admin_html = templates.env.get_template("base.html").render(
        request=_fake_request("/admin/escalations", csrf_cookie="csrf-admin-xyz"),
        current_user=admin_user,
    )
    assert 'href="/map"' in admin_html
    assert 'href="/reports/new"' in admin_html
    assert 'href="/routes"' in admin_html
    assert 'href="/analytics"' in admin_html
    assert 'href="/admin/escalations"' in admin_html
    assert "Escalations" in admin_html
    assert "bg-accent" in admin_html and "text-ink" in admin_html
    # Mobile navigation drawer and accessible toggle present
    assert 'aria-controls="sr-mobile-nav"' in admin_html
    assert 'id="sr-mobile-nav"' in admin_html
    assert 'aria-label="Toggle navigation menu"' in admin_html


# ==============================================================================
# 4. Toast Notification Component
# ==============================================================================


def test_toast_component_renders_accessible_live_regions_and_safely_escapes_payloads():
    server_toasts = [
        {"type": "success", "message": "Hazard report submitted."},
        {"type": "error", "message": "Unable to upload <script>alert(1)</script>."},
        {"type": "warning", "message": "GPS unavailable; using fallback location."},
        {"type": "info", "message": "Live map updates connected."},
    ]
    html = templates.env.get_template("base.html").render(
        request=_fake_request("/map"),
        current_user=None,
        toasts=server_toasts,
    )

    assert 'id="sr-toast-root"' in html
    assert 'x-data="srToastManager()"' in html
    assert 'role="status" aria-live="polite"' in html
    assert 'role="alert" aria-live="assertive"' in html
    assert 'aria-label="Dismiss notification"' in html
    # Raw <script> must be escaped inside JSON payload
    assert "<script>alert(1)</script>" not in html

    # Extract JSON payload and verify all 4 toast messages are preserved cleanly
    marker = '<script id="sr-initial-toasts" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    parsed = json.loads(html[start:end])
    assert len(parsed) == 4
    assert [t["type"] for t in parsed] == ["success", "error", "warning", "info"]


# ==============================================================================
# 5. Reusable UI Macros (Button, Card, Chip, Severity Badge, Inputs)
# ==============================================================================


def test_reusable_ui_macros_render_accessible_semantic_markup():
    rendered = _render_template(
        """
        {% from "components/ui.html" import
           button, card, chip, severity_badge, badge,
           input_field, textarea_field, select_field %}

        {{ button("Submit Report", variant="primary", type="submit") }}
        {{ button("Cancel", variant="secondary", href="/map") }}
        {{ button("Saving...", variant="primary", loading=True) }}

        {% call card(title="Report Details", subtitle="Verified by community") %}
          <p>Card body text</p>
        {% endcall %}

        {{ chip("Pothole", active=True) }}
        {{ chip("Streetlight", active=False) }}

        {{ severity_badge("low") }}
        {{ severity_badge("medium", score=55, show_score=True) }}
        {{ severity_badge("high", score=88, show_score=True) }}

        {{ badge("Suggested: Pothole", variant="mint") }}

        {{ input_field(
          "email",
          "Email address",
          type="email",
          required=True,
          help_text="Use your registered email.",
          error="Please enter a valid email address."
        ) }}

        {{ textarea_field("description", "Hazard description", required=True, maxlength=2000) }}

        {{ select_field(
          "category",
          "Hazard category",
          options=[("pothole", "Pothole"), ("road_break", "Road Break")],
          selected="pothole",
          required=True
        ) }}
        """
    )

    # Buttons
    assert 'type="submit"' in rendered
    assert "Submit Report" in rendered
    assert 'href="/map"' in rendered and "Cancel" in rendered
    assert 'aria-busy="true"' in rendered

    # Card
    assert "rounded-card" in rendered
    assert "Report Details" in rendered
    assert "Card body text" in rendered

    # Chips
    assert "rounded-full" in rendered
    assert 'aria-pressed="true"' in rendered
    assert 'aria-pressed="false"' in rendered

    # Severity badges ALWAYS include explicit text labels + aria-label
    assert 'aria-label="Severity: Low"' in rendered
    assert ">Low<" in rendered
    assert 'aria-label="Severity: Medium (55 out of 100)"' in rendered
    assert ">Medium<" in rendered
    assert 'aria-label="Severity: High (88 out of 100)"' in rendered
    assert ">High<" in rendered

    # Mint badge strictly uses bg-accent + text-ink
    assert "bg-accent text-ink" in rendered

    # Inputs & form controls with accessible labels, error wiring, and select state
    assert '<label for="field-email"' in rendered
    assert 'aria-invalid="true"' in rendered
    assert 'aria-describedby="field-email-help field-email-error"' in rendered
    assert 'id="field-email-error"' in rendered
    assert 'role="alert"' in rendered
    assert '<textarea' in rendered and 'maxlength="2000"' in rendered
    assert '<option value="pothole" selected>' in rendered


# ==============================================================================
# 6. Required UX States (Loading, Empty, Error) & Accessibility Essentials
# ==============================================================================


def test_required_ux_state_macros_and_accessibility_essentials():
    rendered = _render_template(
        """
        {% from "components/states.html" import loading_state, empty_state, error_state %}
        {{ loading_state("Loading nearby hazards...") }}
        {{ empty_state(action_label="Report a hazard", action_href="/reports/new") }}
        {{ error_state("Could not load map", "Please try again.", retry_href="/map") }}
        """
    )

    # Loading state
    assert 'role="status"' in rendered
    assert 'aria-live="polite"' in rendered
    assert 'aria-busy="true"' in rendered
    assert "Loading nearby hazards..." in rendered

    # Empty state with awareness disclaimer and action
    assert "No hazards reported yet" in rendered
    assert "does not guarantee an area is hazard-free" in rendered
    assert 'href="/reports/new"' in rendered and "Report a hazard" in rendered

    # Error state with alert role and retry action
    assert 'role="alert"' in rendered
    assert "Could not load map" in rendered
    assert 'href="/map"' in rendered and "Try again" in rendered

    # Accessibility essentials in base layout and CSS
    base_html = templates.env.get_template("base.html").render(
        request=_fake_request("/"),
        current_user=None,
    )
    css_text = CSS_PATH.read_text(encoding="utf-8")
    assert 'href="#main-content" class="sr-skip-link"' in base_html
    assert 'id="main-content"' in base_html
    assert ":focus-visible" in css_text
    assert "prefers-reduced-motion" in css_text
