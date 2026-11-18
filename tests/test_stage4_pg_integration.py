"""Real PostgreSQL multi-connection integration & concurrency tests for Stage 4.

To execute against a disposable local PostgreSQL + PostGIS database (never the
production Supabase project):

    set TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/saferoute_test
    python -m pytest tests/test_stage4_pg_integration.py -v

When TEST_DATABASE_URL is not set or psycopg is unavailable, these tests are
explicitly skipped rather than faked so CI/local runs transparently distinguish
static verification from live PostgreSQL execution.
"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import uuid
import pytest

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "").strip()

try:
    import psycopg  # type: ignore
except ImportError:  # pragma: no cover
    psycopg = None

pytestmark = pytest.mark.skipif(
    not TEST_DB_URL or psycopg is None,
    reason=(
        "Live PostgreSQL integration/concurrency tests require a disposable "
        "PostgreSQL+PostGIS database in TEST_DATABASE_URL and psycopg installed."
    ),
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _connect():
    return psycopg.connect(TEST_DB_URL, autocommit=False)


def _as_user(cur, user_id: str, role: str = "authenticated"):
    cur.execute(f"SET LOCAL ROLE {role}")
    cur.execute("SELECT set_config('request.jwt.claim.sub', %s, TRUE)", (user_id,))
    cur.execute("SELECT set_config('request.jwt.claim.role', %s, TRUE)", (role,))


@pytest.fixture(scope="module")
def pg_schema_ready():
    """Apply Stage 3 + Stage 4 SQL files to a disposable test database."""
    with _connect() as conn:
        with conn.cursor() as cur:
            for rel in [
                "supabase/schema.sql",
                "supabase/functions.sql",
                "supabase/policies.sql",
                "supabase/triggers.sql",
            ]:
                cur.execute((REPO_ROOT / rel).read_text(encoding="utf-8"))
        conn.commit()
    yield


def test_pg_guc_bypass_ai_fields_and_timestamps(pg_schema_ready):
    """Verify GUC bypass fails, AI/duplicate fields are locked, and timestamps are forced."""
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    report_admin_id = None

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth.users (id, instance_id, aud, role, email, raw_user_meta_data, created_at, updated_at)
                VALUES
                    (%s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', %s, '{"full_name":"A","role":"admin"}'::jsonb, NOW(), NOW()),
                    (%s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', %s, '{"full_name":"B"}'::jsonb, NOW(), NOW())
                """,
                (user_a, f"{user_a}@test.local", user_b, f"{user_b}@test.local"),
            )
            cur.execute(
                """
                INSERT INTO public.hazard_reports (reporter_id, category, description, location, ai_confidence)
                VALUES (%s, 'pothole', 'Seeded ref', extensions.ST_SetSRID(extensions.ST_MakePoint(78.4138, 17.4849), 4326)::extensions.geography, 0.85)
                RETURNING id
                """,
                (user_a,),
            )
            report_admin_id = str(cur.fetchone()[0])

            _as_user(cur, user_a)

            # Forged AI / duplicate fields rejected on INSERT
            for col, val in [
                ("ai_confidence", "0.99"),
                ("ai_suggested_category", "'pothole'"),
                ("duplicate_of", f"'{report_admin_id}'::uuid"),
            ]:
                cur.execute("SAVEPOINT sp_ins")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        f"""
                        INSERT INTO public.hazard_reports (reporter_id, category, description, location, {col})
                        VALUES (%s, 'pothole', 'forged', extensions.ST_SetSRID(extensions.ST_MakePoint(78.41, 17.48), 4326)::extensions.geography, {val})
                        """,
                        (user_a,),
                    )
                cur.execute("ROLLBACK TO SAVEPOINT sp_ins")

            # Past and future created_at forced to NOW()
            for offset in ("-60 days", "+30 days"):
                cur.execute(
                    """
                    INSERT INTO public.hazard_reports (reporter_id, category, description, location, created_at)
                    VALUES (%s, 'other', 'ts test', extensions.ST_SetSRID(extensions.ST_MakePoint(78.41, 17.48), 4326)::extensions.geography, NOW() + %s::interval)
                    RETURNING ABS(EXTRACT(EPOCH FROM (NOW() - created_at)))
                    """,
                    (user_a, offset),
                )
                assert cur.fetchone()[0] < 5.0

            # Attempt custom GUC bypass on all 10 protected columns + AI/duplicate columns
            cur.execute("SELECT set_config('saferoute.internal_metrics_update', 'on', TRUE)")
            protected_updates = [
                "severity_score = 100",
                "confidence = 'high'",
                "confirm_count = 10",
                "dispute_count = 2",
                "fixed_count = 2",
                "escalation_recommended = TRUE",
                "status = 'escalated'",
                "escalated_at = NOW()",
                "fixed_at = NOW()",
                "is_seed = TRUE",
                "ai_confidence = 0.99",
                "ai_suggested_category = 'pothole'",
                f"duplicate_of = '{report_admin_id}'::uuid",
            ]
            for assignment in protected_updates:
                cur.execute("SAVEPOINT sp_upd")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute(
                        f"UPDATE public.hazard_reports SET {assignment} WHERE id = %s",
                        (report_admin_id,),
                    )
                cur.execute("ROLLBACK TO SAVEPOINT sp_upd")
        conn.rollback()


def test_pg_concurrent_verifications_and_fixed_threshold(pg_schema_ready):
    """Verify FOR UPDATE serialization under real concurrent multi-connection votes."""
    reporter = str(uuid.uuid4())
    voters = [str(uuid.uuid4()) for _ in range(5)]
    report_id = None

    # Create committed fixture rows in disposable DB
    with _connect() as conn:
        with conn.cursor() as cur:
            for uid in [reporter, *voters]:
                cur.execute(
                    """
                    INSERT INTO auth.users (id, instance_id, aud, role, email, raw_user_meta_data, created_at, updated_at)
                    VALUES (%s, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', %s, '{"full_name":"Voter"}'::jsonb, NOW(), NOW())
                    """,
                    (uid, f"{uid}@test.local"),
                )
            cur.execute(
                """
                INSERT INTO public.hazard_reports (reporter_id, category, description, location)
                VALUES (%s, 'pothole', 'Concurrency test report', extensions.ST_SetSRID(extensions.ST_MakePoint(78.4138, 17.4849), 4326)::extensions.geography)
                RETURNING id
                """,
                (reporter,),
            )
            report_id = str(cur.fetchone()[0])
        conn.commit()

    try:
        # 1. Five concurrent confirmations across 5 separate DB connections
        barrier_5 = threading.Barrier(5)

        def cast_confirm(uid: str):
            with _connect() as c:
                with c.cursor() as cur:
                    _as_user(cur, uid)
                    barrier_5.wait()
                    cur.execute(
                        "INSERT INTO public.verifications (report_id, user_id, vote) VALUES (%s, %s, 'confirm')",
                        (report_id, uid),
                    )
                c.commit()

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(cast_confirm, voters))

        with _connect() as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT confirm_count, confidence, severity_score, escalation_recommended FROM public.hazard_reports WHERE id = %s",
                    (report_id,),
                )
                confirms, confidence, severity, esc_rec = cur.fetchone()
                assert confirms == 5
                assert confidence == "high"
                assert severity == 74  # pothole(40) + confirms_cap(24) + <=7d(10)
                assert esc_rec is True

        # 2. Two concurrent fixed votes -> status transitions to fixed
        barrier_2 = threading.Barrier(2)

        def cast_fixed(uid: str):
            with _connect() as c:
                with c.cursor() as cur:
                    _as_user(cur, uid)
                    barrier_2.wait()
                    cur.execute(
                        "INSERT INTO public.verifications (report_id, user_id, vote) VALUES (%s, %s, 'fixed')",
                        (report_id, uid),
                    )
                c.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(cast_fixed, voters[:2]))

        with _connect() as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT fixed_count, status, fixed_at IS NOT NULL FROM public.hazard_reports WHERE id = %s",
                    (report_id,),
                )
                assert cur.fetchone() == (2, "fixed", True)

        # 3. Concurrent fixed-vote DELETE + dispute INSERT -> reverts status to open, dispute_count=1
        barrier_mix = threading.Barrier(2)

        def delete_fixed():
            with _connect() as c:
                with c.cursor() as cur:
                    _as_user(cur, voters[1])
                    barrier_mix.wait()
                    cur.execute(
                        "DELETE FROM public.verifications WHERE report_id = %s AND user_id = %s AND vote = 'fixed'",
                        (report_id, voters[1]),
                    )
                c.commit()

        def insert_dispute():
            with _connect() as c:
                with c.cursor() as cur:
                    _as_user(cur, voters[2])
                    barrier_mix.wait()
                    cur.execute(
                        "INSERT INTO public.verifications (report_id, user_id, vote) VALUES (%s, %s, 'dispute')",
                        (report_id, voters[2]),
                    )
                c.commit()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(delete_fixed)
            f2 = pool.submit(insert_dispute)
            f1.result()
            f2.result()

        with _connect() as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT confirm_count, dispute_count, fixed_count, status, severity_score FROM public.hazard_reports WHERE id = %s",
                    (report_id,),
                )
                assert cur.fetchone() == (5, 1, 1, "open", 64)
    finally:
        # Clean up synthetic disposable test rows
        with _connect() as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM public.hazard_reports WHERE id = %s", (report_id,))
                cur.execute("DELETE FROM auth.users WHERE id = ANY(%s)", ([reporter, *voters],))
            c.commit()
