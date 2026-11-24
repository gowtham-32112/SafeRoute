# SafeRoute 🚦

**AI-powered Community Road-Hazard Awareness Platform**

SafeRoute is a collaborative civic web platform where citizens report and verify road hazards—such as potholes, road breaks, non-functional streetlights, and blocked walkways—on an interactive live map. The system employs visual AI to suggest hazard categories from uploaded photos, calculates dynamic community severity scores, flags unaddressed critical hazards for municipal escalation, and empowers commuters to plan safer routes that account for active road hazards.

> **Important Civic Disclaimer**: SafeRoute is an informational awareness tool. It does not repair road infrastructure or guarantee route safety. The absence of reported hazards in an area does not guarantee that the roadway is hazard-free.

---

## 1. Project Objectives & Core Features

1. **Crowdsourced Hazard Reporting (`/reports/new`)**: Pinpoint hazards on Leaflet maps with client-side EXIF stripping, multi-category selection, duplicate detection prompts within 30m, and automatic location reverse-geocoding.
2. **AI-Assisted Classification (`POST /reports/suggest-category`)**: Fast multimodal vision AI analyzes uploaded road photos and recommends categories (`pothole`, `road_break`, `streetlight`, `footpath`, `other`) with confidence ratings and fallback safety.
3. **Interactive Map & Live Updates (`/map`)**: Real-time Leaflet visualization with marker clustering, category and status filter chips, and Supabase Realtime synchronization.
4. **Community Verification & Voting (`/reports/{id}`)**: Peer verification (`confirm`, `dispute`, `vote fixed`), civic comments, severity evolution, and the automatic 2-vote fixed resolution rule.
5. **Live Issues (`/live-issues`)**: Authenticated commuters can browse open and closed reports with photos, confirm or dispute open reports, and read admin broadcasts. Only admins can edit or delete reports.
6. **Hazard-Aware Route Planning (`/routes`)**: OSRM routing integration intersecting known road hazards within a 25-metre buffer, computing hazard-weighted route risk scores and recommending the safest travel corridors.
7. **Civic Safety Analytics (`/analytics`)**: Regional hazard hotspots, interactive Leaflet heatmaps, top 5 affected zones, category/status charts, and an "Exclude seed data" toggle.
8. **Municipal Escalation Management (`/admin/escalations`)**: Administrative dashboard allowing civic authorities to prioritize and escalate critical hazards to municipal bodies (GHMC, HMDA, TSSPDCL), and publish commuter broadcasts.

---

## 2. Technology Stack

SafeRoute is built with a strictly locked, modern, lightweight stack:

| Component | Technology | Description |
|---|---|---|
| **Backend** | Python 3.11 + FastAPI | High-performance asynchronous REST API and application server |
| **Templates** | Jinja2 | Clean server-side HTML rendering |
| **Frontend Dynamics** | HTMX + Alpine.js | Modern declarative interactivity without complex SPA build tools |
| **Styling** | Tailwind CSS (CDN) | Utility-first styling configured with SafeRoute design tokens |
| **Database & GIS** | Supabase (PostgreSQL + PostGIS) | Relational database with geospatial indexing and SQL triggers |
| **Authentication** | Supabase Auth | Email & password session management with secure cookies |
| **Storage** | Supabase Storage | Authenticated storage bucket (`hazard-photos`) with EXIF stripping |
| **Realtime** | Supabase Realtime | Live map marker updates on hazard inserts and status changes |
| **Interactive Maps** | Leaflet.js + Leaflet.heat | Smooth client-side mapping, marker clustering, and heatmaps |
| **Charts** | Chart.js | Accessible responsive data visualizations for civic analytics |
| **Routing Engine** | OSRM API | Open Source Routing Machine with custom safety penalty weighting |

---

## 3. Architecture & Data Flow

```
                      +---------------------------------------+
                      |       Client Browser (Desktop/Mobile) |
                      |   HTML5 + Alpine.js + Leaflet + HTMX  |
                      +-------------------+-------------------+
                                          | HTTP / WebSockets
                                          v
                      +---------------------------------------+
                      |           FastAPI Gateway             |
                      |   - Jinja2 Template Views             |
                      |   - Double-Submit CSRF Protection     |
                      |   - REST API & Health Check           |
                      +-------+-----------------------+-------+
                              |                       |
               +--------------+                       +---------------+
               v                                                      v
+-------------------------------+                     +-------------------------------+
|      Supabase Services        |                     |      External Services        |
| - PostgreSQL with PostGIS     |                     | - Vision AI Model (Category)  |
| - Supabase Auth (Sessions)    |                     | - OSRM Routing Engine         |
| - Supabase Storage (Photos)   |                     |                               |
| - Supabase Realtime Pub/Sub   |                     +-------------------------------+
+-------------------------------+
```

---

## 4. Supabase Database Setup & Migration Order

To initialize the PostgreSQL + PostGIS database in Supabase (or any standard PostgreSQL instance), execute the SQL scripts located in `supabase/` strictly in the following sequence:

1. **`supabase/schema.sql`**: Activates PostGIS extension, creates core tables (`profiles`, `hazard_reports`, `report_images`, `verifications`, `comments`, `escalations`, `status_history`), constraints, spatial GIST indexes, and the auth user signup trigger.
2. **`supabase/functions.sql`**: Deploys spatial query RPC functions (`reports_in_bounds`, `hazards_near_route`, `check_nearby_reports`).
3. **`supabase/triggers.sql`**: Deploys automated scoring logic, role escalation guards, self-vote prevention, status history logging, and the 2-vote fixed threshold trigger.
4. **`supabase/policies.sql`**: Enables and enforces Row-Level Security (RLS) on all tables and Storage bucket access rules.
5. **`supabase/seed.sql`**: Seeds deterministic demo accounts (admin and citizens), 20 realistic Kukatpally corridor hazard reports (`is_seed = true`), photos, verifications, comments, and municipal escalations.

For an existing installation, apply `supabase/migrations/20261124_admin_only_hazard_report_edits.sql` after the schema and policies. It removes commuter update/delete access to hazard reports so only admins can edit or delete them.

---

## 5. Installation & Local Setup

### Prerequisites
- Python 3.11+
- Git

### 1. Clone & Navigate to Repository
```bash
git clone https://github.com/gowtham-32112/SafeRoute.git
cd SafeRoute
```

### 2. Create and Activate Virtual Environment
On Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On macOS / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Copy the template and supply your credentials:
```bash
cp .env.example .env
```

Populate `.env` with your project parameters:
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-supabase-anon-key
SECRET_KEY=your-random-32-byte-secret-key
VISION_API_KEY=your-gemini-or-vision-api-key
OSRM_BASE_URL=https://router.project-osrm.org
OSRM_TIMEOUT_SECONDS=6.0
ROUTE_HAZARD_BUFFER_METERS=25.0
KUKATPALLY_FALLBACK_LAT=17.4849
KUKATPALLY_FALLBACK_LNG=78.4138
```

---

## 6. Running the Application

Start the local development server:
```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open your browser to:
- **Landing Page**: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **Live Map**: [http://127.0.0.1:8000/map](http://127.0.0.1:8000/map)
- **Report Hazard**: [http://127.0.0.1:8000/reports/new](http://127.0.0.1:8000/reports/new)
- **Route Planner**: [http://127.0.0.1:8000/routes](http://127.0.0.1:8000/routes)
- **Analytics Dashboard**: [http://127.0.0.1:8000/analytics](http://127.0.0.1:8000/analytics)
- **Admin Escalations**: [http://127.0.0.1:8000/admin/escalations](http://127.0.0.1:8000/admin/escalations)
- **Health Check**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

### Demo Accounts (From `supabase/seed.sql`)
| Role | Email | Password | Access |
|---|---|---|---|
| **Admin** | `admin@saferoute.demo` | *(Configured during seed)* | Full citizen access + `/admin/escalations` dashboard |
| **Citizen 1** | `citizen1@saferoute.demo` | *(Configured during seed)* | Standard reporting & community voting |
| **Citizen 2** | `citizen2@saferoute.demo` | *(Configured during seed)* | Standard reporting & community voting |

---

## 7. Running Automated Tests

Run the complete test suite with `pytest`:
```bash
pytest tests/ -v
```

The automated test suite verifies:
- API endpoint health & authentication guards
- PostGIS SQL schemas, functions, and trigger logic
- Severity calculation and confidence scaling
- Duplicate detection & spatial clustering
- AI category suggestions & graceful fallbacks
- OSRM route penalty algorithms
- Analytics KPI metrics & zero-division safety
- Security compliance (Zero `SUPABASE_SERVICE_KEY` in runtime app, zero `dark:` classes)

---

## 8. Cloud Deployment

### Deployment with Render
A secret-free `render.yaml` and `Procfile` are included in the repository.

1. Connect your GitHub repository to [Render](https://render.com).
2. Create a new **Blueprint** service pointing to `render.yaml`.
3. In the Render Dashboard, fill in the secret environment variables (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `VISION_API_KEY`).
4. Render builds with Python 3.11.9 and runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

---

## 9. Design System & Accessibility

SafeRoute follows strict accessibility and contrast standards:
- **Palette**: `primary` (`#106EBE`), `primary-dark` (`#0B4F8A`), `accent` mint (`#0FFCBE`, dark surfaces only), `ink` (`#0F2A43`), `surface` (`#FFFFFF`).
- **Severity Colors**: Low (`#2E9E5B`), Medium (`#F5A524`), High (`#D92D20`).
- **Typography**: Plus Jakarta Sans.
- **Rules**: Strictly light-mode only (`color-scheme: only light;`, 0 `dark:` classes), no mint text on light surfaces, visible keyboard focus rings, and safe DOM binding (zero `innerHTML` on user input).
