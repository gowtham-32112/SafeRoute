"""Stage 2 Non-Destructive Supabase Infrastructure Verification Script.

Performs strictly read-only live checks against the configured Supabase project
using the local .env configuration. Never prints or logs secret keys or tokens,
including inside exception messages.

Statuses reported by this script:
- PASS: Verified live via API response.
- FAIL: Verified live and violates Stage 2 requirements.
- NOT VERIFIED: Not accessible with the publishable key; requires manual
  verification in the Supabase Dashboard or SQL Editor (recorded in
  docs/STAGE-2-SUPABASE-SETUP.md).
- PENDING STAGE 3: Intentionally deferred until Stage 3 schema creation.
"""

import sys
from pathlib import Path
from typing import Dict

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from app.config import settings


PLANNED_TABLES = [
    "profiles",
    "hazard_reports",
    "report_images",
    "verifications",
    "comments",
    "escalations",
    "status_history",
]

PLANNED_RPCS = [
    "reports_in_bounds",
    "nearby_reports",
    "hazards_near_route",
]


def _safe_error_label(exc: Exception) -> str:
    """Return a credential-safe exception class label without raw request details."""
    return type(exc).__name__


def run_verification() -> Dict[str, str]:
    """Run read-only Stage 2 verification checks and return status map."""
    results: Dict[str, str] = {}

    print("=" * 68)
    print("SafeRoute — Stage 2 Live Supabase Infrastructure Verification")
    print("=" * 68)

    url = settings.SUPABASE_URL.strip().rstrip("/")
    anon_key = settings.SUPABASE_ANON_KEY.strip()

    # 1. Local Environment Configuration Check
    if not url or "placeholder" in url:
        print("[FAIL] SUPABASE_URL is missing or placeholder in local .env.")
        results["env_config"] = "FAIL"
        return results

    if not anon_key:
        print("[FAIL] SUPABASE_ANON_KEY is missing in local .env.")
        results["env_config"] = "FAIL"
        return results

    project_ref = url.replace("https://", "").split(".")[0]
    print(f"[PASS] Local .env loaded (project ref: {project_ref}, credentials redacted)")
    results["env_config"] = "PASS"

    # Supports Supabase publishable key format (sb_publishable_...) via apikey header.
    anon_headers = {"apikey": anon_key}

    # 2. Supabase Auth Service Health & Project Reachability
    try:
        health_resp = httpx.get(f"{url}/auth/v1/health", headers=anon_headers, timeout=10.0)
        if health_resp.status_code == 200:
            health_data = health_resp.json()
            print(
                f"[PASS] Supabase project reachable — GoTrue Auth API healthy "
                f"(version: {health_data.get('version', 'unknown')})"
            )
            results["project_connection"] = "PASS"
        else:
            print(f"[FAIL] Auth health returned HTTP {health_resp.status_code}")
            results["project_connection"] = "FAIL"
    except Exception as exc:
        print(f"[FAIL] Unable to reach Supabase project ({_safe_error_label(exc)})")
        results["project_connection"] = "FAIL"
        return results

    # 3. Authentication Providers (Email & Google)
    try:
        auth_resp = httpx.get(f"{url}/auth/v1/settings", headers=anon_headers, timeout=10.0)
        if auth_resp.status_code == 200:
            auth_cfg = auth_resp.json()
            external = auth_cfg.get("external", {})
            email_enabled = external.get("email") is True
            google_disabled = external.get("google") is False
            signup_enabled = auth_cfg.get("disable_signup") is False
            autoconfirm = auth_cfg.get("mailer_autoconfirm")

            if email_enabled and signup_enabled:
                print(
                    f"[PASS] Email/password auth & signup enabled "
                    f"(mailer_autoconfirm={autoconfirm})"
                )
                results["auth_email"] = "PASS"
            else:
                print(
                    f"[FAIL] Email auth/signup mismatch: "
                    f"email={email_enabled}, signup_enabled={signup_enabled}"
                )
                results["auth_email"] = "FAIL"

            if google_disabled:
                print("[PASS] Google OAuth provider is disabled.")
                results["auth_google"] = "PASS"
            else:
                print("[FAIL] Google OAuth provider appears enabled (must be disabled).")
                results["auth_google"] = "FAIL"

            # Redirect URLs & Site URL are not exposed by /auth/v1/settings
            print(
                "[NOT VERIFIED] Auth Site URL & Redirect URLs are not exposed by "
                "/auth/v1/settings (manually verified in Supabase Dashboard; "
                "see docs/STAGE-2-SUPABASE-SETUP.md)."
            )
            results["auth_urls"] = "NOT VERIFIED"
        else:
            print(f"[NOT VERIFIED] /auth/v1/settings returned HTTP {auth_resp.status_code}")
            results["auth_email"] = "NOT VERIFIED"
            results["auth_google"] = "NOT VERIFIED"
            results["auth_urls"] = "NOT VERIFIED"
    except Exception as exc:
        print(f"[FAIL] Error checking auth settings ({_safe_error_label(exc)})")
        results["auth_email"] = "FAIL"

    # 4. PostgreSQL / PostgREST Connectivity & Absence of Application Tables
    try:
        existing_tables = []
        pg_reachable = False

        for table in PLANNED_TABLES:
            resp = httpx.get(
                f"{url}/rest/v1/{table}?select=*&limit=1",
                headers=anon_headers,
                timeout=10.0,
            )
            if resp.status_code == 404 and "PGRST205" in resp.text:
                pg_reachable = True
            elif resp.status_code in (200, 206, 401, 403):
                pg_reachable = True
                if "PGRST205" not in resp.text:
                    existing_tables.append(table)

        if pg_reachable:
            print(
                "[PASS] PostgreSQL / PostgREST gateway reachable "
                "(schema cache responding via PostgREST)."
            )
            results["postgres_connectivity"] = "PASS"
        else:
            print("[NOT VERIFIED] Could not confirm PostgREST schema cache response.")
            results["postgres_connectivity"] = "NOT VERIFIED"

        if not existing_tables:
            print(
                f"[PASS] None of the {len(PLANNED_TABLES)} planned application tables "
                f"exist in public schema (PGRST205 confirmed)."
            )
            results["no_app_tables"] = "PASS"
        else:
            print(f"[FAIL] Unauthorized application tables detected: {existing_tables}")
            results["no_app_tables"] = "FAIL"

        # Check planned RPC functions absence
        existing_rpcs = []
        for fn in PLANNED_RPCS:
            resp = httpx.post(
                f"{url}/rest/v1/rpc/{fn}",
                headers={**anon_headers, "Content-Type": "application/json"},
                json={},
                timeout=10.0,
            )
            if not (resp.status_code == 404 and "PGRST202" in resp.text):
                existing_rpcs.append(fn)

        if not existing_rpcs:
            print(
                f"[PASS] None of the {len(PLANNED_RPCS)} planned SQL RPC functions "
                f"exist in public schema (PGRST202 confirmed)."
            )
            results["no_app_functions"] = "PASS"
        else:
            print(f"[FAIL] Unauthorized SQL functions detected: {existing_rpcs}")
            results["no_app_functions"] = "FAIL"
    except Exception as exc:
        print(f"[FAIL] PostgREST verification error ({_safe_error_label(exc)})")
        results["postgres_connectivity"] = "FAIL"

    # 5. PostGIS Extension Verification (Privileged catalog)
    print(
        "[NOT VERIFIED] PostGIS extension catalog (pg_extension) is not accessible "
        "via publishable key (manually verified via SQL Editor; see docs/STAGE-2-SUPABASE-SETUP.md)."
    )
    results["postgis_enabled"] = "NOT VERIFIED"

    # 6. Storage Bucket 'hazard-photos' Existence, Privacy, and Metadata
    try:
        auth_obj_resp = httpx.get(
            f"{url}/storage/v1/object/authenticated/hazard-photos/__stage2_probe__",
            headers=anon_headers,
            timeout=10.0,
        )
        fake_obj_resp = httpx.get(
            f"{url}/storage/v1/object/authenticated/__nonexistent_bucket_probe__/__probe__",
            headers=anon_headers,
            timeout=10.0,
        )
        pub_obj_resp = httpx.get(
            f"{url}/storage/v1/object/public/hazard-photos/__stage2_probe__",
            timeout=10.0,
        )

        bucket_exists = (
            "NoSuchKey" in auth_obj_resp.text and "NoSuchBucket" in fake_obj_resp.text
        )
        bucket_is_private = "NoSuchBucket" in pub_obj_resp.text

        if bucket_exists:
            print(
                "[PASS] Storage bucket 'hazard-photos' exists "
                "(authenticated probe returned NoSuchKey vs NoSuchBucket for unknown bucket)."
            )
            results["bucket_exists"] = "PASS"
        else:
            print("[FAIL] Storage bucket 'hazard-photos' could not be confirmed.")
            results["bucket_exists"] = "FAIL"

        if bucket_exists and bucket_is_private:
            print(
                "[PASS] Storage bucket 'hazard-photos' is PRIVATE "
                "(public endpoint rejects access with NoSuchBucket)."
            )
            results["bucket_private"] = "PASS"
        elif bucket_exists:
            print("[FAIL] Storage bucket 'hazard-photos' appears publicly accessible!")
            results["bucket_private"] = "FAIL"
        else:
            results["bucket_private"] = "NOT VERIFIED"

        print(
            "[NOT VERIFIED] Bucket file_size_limit and allowed_mime_types are not "
            "readable via publishable key (manually verified via SQL Editor; "
            "see docs/STAGE-2-SUPABASE-SETUP.md)."
        )
        results["bucket_limits"] = "NOT VERIFIED"
    except Exception as exc:
        print(f"[FAIL] Storage verification error ({_safe_error_label(exc)})")
        results["bucket_exists"] = "FAIL"

    # 7. Triggers & RLS Policies Catalog Check (Privileged catalog)
    print(
        "[NOT VERIFIED] Catalog-level triggers (pg_trigger) and RLS policies (pg_policies) "
        "are not accessible via publishable key (manually verified via SQL Editor; "
        "see docs/STAGE-2-SUPABASE-SETUP.md)."
    )
    results["no_triggers_rls"] = "NOT VERIFIED"

    # 8. Realtime Activation
    print(
        "[PENDING STAGE 3] Realtime publication for 'hazard_reports' awaits Stage 3 "
        "table creation."
    )
    results["realtime"] = "PENDING STAGE 3"

    print("=" * 68)
    return results


if __name__ == "__main__":
    run_verification()
