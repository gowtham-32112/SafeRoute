# SafeRoute — Database & PostGIS Spatial Architecture Review

> **Document Scope**: Dedicated technical review of the SafeRoute PostgreSQL schema, PostGIS spatial engine, Row-Level Security (RLS) policies, concurrency triggers, and spatial stored procedures.
> **Repository Location**: `d:\SafeRoute-2`

---

## 1. Core Database Stack

- **PostgreSQL**: 15+ (Hosted on Supabase).
- **PostGIS**: 3.3+ geospatial extension.
- **Supabase Auth**: Managed user identity and JWT issuance.
- **Supabase Storage**: Private S3-compatible asset bucket (`hazard-photos`).
- **Supabase Realtime**: PostgreSQL Change Data Capture (CDC) publication.

---

## 2. Directory & Schema Files

```
supabase/
├── schema.sql                  # 7 core tables, constraints, GIST & B-tree indexes, auth signup trigger
├── functions.sql               # Spatial RPC functions: reports_in_bounds, nearby_reports, hazards_near_route
├── triggers.sql                # Concurrency-safe severity calculation, self-vote guard, status history logging
├── policies.sql                # Strict Row-Level Security (RLS) on all 7 tables & storage policies
├── seed.sql                    # Realistic seed data for Kukatpally, Hyderabad (hazards, votes, comments)
└── migrations/                 # Incremental schema migration scripts
```

---

## 3. Seven Core Tables & Constraints

1. **`public.profiles`**: Commuter profiles linked to `auth.users(id)` with roles (`user`, `admin`).
2. **`public.hazard_reports`**: Master hazard table with `location geography(Point, 4326)`, category, status (`open`, `escalated`, `fixed`), severity score (0–100), confidence, confirm/dispute/fixed counts, and AI suggestion fields.
3. **`public.report_images`**: Evidence and after-fix images linked to reports and stored in `hazard-photos`.
4. **`public.verifications`**: Community votes (`confirm`, `dispute`, `fixed`) with compound primary key `(report_id, user_id, vote)` preventing duplicate votes.
5. **`public.comments`**: Threaded citizen comments on hazard reports.
6. **`public.escalations`**: Formal administrative escalations to municipal authorities (GHMC, Police).
7. **`public.status_history`**: Immutable audit log of all report status transitions.

---

## 4. PostGIS Spatial Functions (`supabase/functions.sql`)

All functions use `SECURITY INVOKER` and isolated `search_path = public, extensions`:

1. **`reports_in_bounds(min_lat, min_lng, max_lat, max_lng, category_filter, status_filter)`**:
   - Queries map bounding box using `ST_MakeEnvelope` and `ST_Intersects`.
   - Utilizes spatial GIST index `idx_hazard_reports_location_gist`.
2. **`nearby_reports(lat, lng, radius_m, category)`**:
   - Detects duplicate hazards within 30m created in the last 30 days using `ST_DWithin`.
3. **`hazards_near_route(route_geojson, buffer_m)`**:
   - Evaluates driving route safety by intersecting an OSRM GeoJSON LineString with active hazard points within a 25m spherical buffer.

---

## 5. Security Policies (RLS) & Triggers

- **Zero Anon Access**: Anonymous users have no read/write permissions.
- **Self-Vote Prevention**: Trigger `trg_verifications_prevent_self_vote` raises exception `P0001` if a user attempts to vote on their own report.
- **Serialized Metric Recomputation**: Trigger `recompute_report_metrics()` acquires a row-level lock (`FOR UPDATE`) on `hazard_reports` and recomputes severity score, confidence, and status (auto-resolves when `fixed_count >= 2`).
- **Storage Isolation**: Bucket `hazard-photos` restricts uploads to `<auth.uid()>/<report_id>/<file>`.
