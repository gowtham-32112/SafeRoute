# SafeRoute Stage 7 — B6: Report Detail & Verification Page

## 1. Overview & Objective
GitHub Issue: [#24 – B6 · Report detail and verification page](https://github.com/gowtham-32112/SafeRoute/issues/24)  
Milestone: **Day 1 – Core loop**  
Assignee: **Member B (BhargavaSai-Github)**  
Target Branch: `feat/frontend-24-report-detail-page`

This implementation delivers the user-facing Report Detail and Community Verification view at `GET /reports/{report_id}`. It provides full transparency into hazard status, photographic evidence, the mathematical severity score breakdown, interactive peer verification (confirming, disputing, or marking fixed), a chronological status timeline, and a community discussion thread with anonymous posting support.

---

## 2. Architecture & Design System Compliance

### 2.1 Technology Stack
- **Backend**: Python 3.11, FastAPI, Jinja2 template engine, Supabase PostgreSQL/Storage under authenticated user context.
- **Frontend**: HTML5, Alpine.js (`srReportDetail` controller), Tailwind CSS (CDN), SafeRoute Design Tokens (`saferoute.css`).
- **Zero prohibited frameworks**: No React, Next.js, Vue, Angular, Node.js build steps, or external state containers.

### 2.2 Design System Tokens & Contrast Rules
- **Light theme only**: Strictly enforced `color-scheme: only light;` with zero `dark:` CSS utility classes.
- **High-contrast typography**: Plus Jakarta Sans throughout.
- **Contrast safety**: Accent mint (`#0FFCBE`) is never rendered as text on light surfaces; high-severity hazards utilize deep red `#D92D20` text with text labels (`High · 72/100`).
- **Responsive geometry**: Mobile-first layout with smooth fluid adaptations across desktop (`1280px`), tablet (`768px`), and mobile (`375px`).

---

## 3. Implemented Components & Features

### 3.1 Content Negotiation on `GET /reports/{report_id}`
- If request includes `text/html` in `Accept` (standard browser navigation), renders `report_detail.html`.
- If request does not include `text/html` (e.g., API clients, B4 map popup fetch, curl), returns the validated Pydantic model `ReportDetailResponse` (maintaining 100% backwards compatibility with existing clients and tests).
- Unauthenticated browser navigation redirects with HTTP 303 to `/login`.

### 3.2 Header & Freshness Banner
- Categorical hazard title (e.g., "Pothole", "Road Break").
- Address text and rounded 4-decimal coordinates (`lat, lng`).
- Status badge (Open, Escalated, or Fixed) and recency badge (e.g., "Reported today", "Reported 2 days ago").
- Prominent amber banner when hazard is escalated to municipal authorities.
- Stale warning banner when report has had no activity for over 30 days.

### 3.3 Photographic Evidence Gallery
- High-resolution thumbnails of original evidence photo uploaded with the report.
- Dedicated section for community-submitted after-repair proof photos.
- Graceful fallbacks for missing or loading photos.

### 3.4 Transparent Severity Score Breakdown (Spec Section 8)
Visual calculation card displaying each term of the authoritative formula:
$$\text{severity\_score} = \text{clamp}(\text{base\_weight} + \text{photo\_bonus} + \text{ai\_bonus} + \text{confirm\_bonus} - \text{dispute\_penalty} + \text{recency\_bonus}, 0, 100)$$
- **Category Base Weight**: Pothole (+50), Road Break (+50), Footpath (+45), Streetlight (+35), Other (+25).
- **Photo Evidence**: +10 if photograph attached.
- **AI Vision Classification**: +10 if confidence > 0.7.
- **Community Confirmations**: +8 per confirmation (capped at +24).
- **Community Disputes**: -10 per dispute penalty.
- **Recency Factor**: $\le 7$ days (+10), 8–30 days (+5), $> 30$ days (0).
- **Final Clamped Score**: Dynamically reflects community input in real time.

### 3.5 Community Verification (Voting) Panel
- **Peer Verification Protection**: If the current user is the report author, voting buttons are disabled with an explicit notice:
  > *"You submitted this report. Peer verification is reserved for other citizens to maintain integrity."*
- **Live Metric Counters**: Displays confirmation, dispute, and fixed counts.
- **Interactive Action Buttons**:
  - `Confirm – Still Here`: Casts a confirmation vote and immediately increments counter.
  - `Dispute – Not A Hazard`: Casts a dispute vote with penalty deduction.
  - `Mark Fixed`: Opens accessible modal with optional after-repair photo upload ($\le 5\text{ MB}$, JPEG/PNG/WebP, EXIF-stripped).
- Seamless updates via Alpine.js without full-page reloads.

### 3.6 Status Timeline
Vertical chronological timeline tracking:
1. Initial hazard report creation.
2. Escalation event (if escalation criteria met).
3. Status history records (old status $\rightarrow$ new status).
4. Resolved / Fixed transition.

### 3.7 Community Comments Thread & Anonymous Posting
- Chronological list of user comments.
- Comment submission form with 1,000 character maximum.
- **Privacy Support**: "Hide my name" checkbox allowing users to post anonymously as *"Community Member"* without revealing personal identity.
- Safe DOM rendering via Alpine `x-text` (zero `innerHTML` on untrusted input).

---

## 4. Security & Privacy Guarantees

1. **Strict User RLS Context**:
   - Backend queries exclusively use `create_authenticated_supabase_client(access_token)` under the caller's JWT.
   - `SUPABASE_SERVICE_KEY` is NEVER used.
2. **CSRF Protection**:
   - Double-submit CSRF cookie & header pattern enforced on all state-changing POST requests (`/vote`, `/comments`).
3. **EXIF Metadata Stripping**:
   - All uploaded after-repair photos have GPS and device metadata completely stripped on the server before storage.
4. **Self-Voting Prevention**:
   - Enforced at database trigger level, backend API level (HTTP 403), and frontend UI level (disabled controls).

---

## 5. Verification Results

### 5.1 Automated Test Suite
- Total tests: **137 passing**, 2 skipped (live Supabase PG triggers), 0 failures.
- `tests/test_stage7_b6_report_detail.py`:
  - `test_content_negotiation_json_vs_html`: PASSED
  - `test_unauthenticated_browser_request_redirects_to_login`: PASSED
  - `test_report_detail_template_renders_all_spec_components`: PASSED
  - `test_self_report_disables_voting_with_notice`: PASSED
  - `test_escalated_and_fixed_badges`: PASSED
  - `test_static_security_and_design_rules`: PASSED

### 5.2 Live Headless Chrome Responsive CDP Verification
Verified via Chrome DevTools Protocol against live local instance:
- **Desktop (1280x800)**: 0 horizontal overflow (`scrollWidth <= innerWidth`), all elements rendered cleanly.
- **Tablet (768x1024)**: 0 horizontal overflow, responsive two-column grid gracefully adapts.
- **Mobile (375x812)**: 0 horizontal overflow, full touch-friendly button targets ($\ge 44\text{px}$).
- **Interactive Verification**:
  - Confirm vote live counter update: $3 \rightarrow 4$ verified.
  - Mark Fixed modal open and file input toggle: verified.
  - Anonymous comment submission: appended to thread with "Community Member" author: verified.
- **Console errors**: `0` (clean execution without errors or unhandled exceptions).
