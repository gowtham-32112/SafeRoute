# SafeRoute 🚦

**AI-powered Community Road-Hazard Awareness Platform**

SafeRoute is a collaborative web platform where citizens report and verify road hazards—such as potholes, road breaks, non-functional streetlights, and blocked walkways—on a live interactive map. The system employs visual AI to suggest hazard categories from uploaded photos, calculates dynamic community severity scores, flags unaddressed critical hazards for municipal escalation, and empowers commuters to plan safer routes that account for active road hazards.

> **Important Civic Disclaimer**: SafeRoute is an informational awareness tool. It does not repair road infrastructure or guarantee route safety. The absence of reported hazards in an area does not guarantee that the roadway is hazard-free.

---

## 1. Project Objectives

1. **Crowdsourced Hazard Reporting**: Allow registered citizens to pinpoint hazards with photos, categories, and descriptions.
2. **Community Verification**: Enable peer verification (confirm, dispute, or vote fixed) to maintain high data integrity.
3. **AI-Assisted Classification**: Use multimodal vision AI to analyze photos and recommend accurate hazard categories.
4. **Hazard-Aware Route Planning**: Integrate with OSRM to score and suggest routes based on real-time hazard severity.
5. **Municipal Awareness**: Automatically identify and flag severe or highly verified hazards for administrative escalation.
6. **Civic Safety Analytics**: Surface regional hazard hotspots, category distributions, and community resolution metrics.

---

## 2. Locked Technology Stack

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
| **Interactive Maps** | Leaflet.js + OpenStreetMap | Smooth client-side mapping, marker clustering, and bounds filtering |
| **Routing Engine** | OSRM API | Open Source Routing Machine with custom safety penalty weighting |

---

## 3. Architecture Overview

```
                      +---------------------------------------+
                      |       Client Browser (Desktop/Mobile)  |
                      |  HTML5 + HTMX + Alpine.js + Leaflet   |
                      +-------------------+-------------------+
                                          | HTTP / WebSockets
                                          v
                      +---------------------------------------+
                      |           FastAPI Gateway             |
                      |   - Jinja2 Template Responses         |
                      |   - HTMX Partial Fragment Endpoints   |
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

## 4. Project Directory Structure

```
saferoute/
├── AGENTS.md                 # Antigravity AI engineering instructions & rules
├── README.md                 # Project documentation and setup guide
├── requirements.txt          # Python application dependencies
├── .env.example              # Environment configuration template
├── .gitignore                # Git exclusions for secrets, cache, and logs
│
├── docs/
│   └── SPEC.md               # Complete approved Final Build Spec v2
│
├── app/
│   ├── __init__.py           # Application package marker
│   ├── main.py               # FastAPI application entrypoint & health routes
│   ├── config.py             # Settings, environment parser & geo defaults
│   ├── deps.py               # FastAPI dependency injection providers
│   │
│   ├── routers/              # Modular route handlers (Stage 2+)
│   │   └── __init__.py
│   │
│   ├── services/             # Core business logic services (Stage 2+)
│   │   └── __init__.py
│   │
│   ├── templates/            # Jinja2 HTML templates
│   │
│   └── static/               # Client-side static assets
│       ├── css/              # Custom stylesheets
│       ├── js/               # Client scripts & Leaflet integrations
│       └── img/              # Branding and icons
│
├── supabase/                 # Database migrations and seed scripts
│   ├── schema.sql            # Table definitions, PostGIS extension & indexes
│   ├── functions.sql         # Spatial bounds and proximity SQL functions
│   ├── triggers.sql          # Auto-scoring, fixed rule & audit triggers
│   ├── policies.sql          # Row Level Security (RLS) policies
│   └── seed.sql              # Demo data around Kukatpally Y Junction
│
└── tests/                    # Test suite
    ├── __init__.py
    └── test_health.py        # Health and basic endpoint tests
```

---

## 5. Design Tokens

| Token Name | Hex Code | Purpose |
|---|---|---|
| `primary` | `#106EBE` | Action buttons, active navigation, links, header bar |
| `primary-dark` | `#0B4F8A` | Button hover states, headings on light surfaces |
| `primary-soft` | `#E6F1FA` | Tinted background containers, active filter chips |
| `accent` (mint) | `#0FFCBE` | Highlights on dark blue surfaces, focus rings (*Never text on white*) |
| `ink` | `#0F2A43` | Primary typography and high-contrast text |
| `muted` | `#5B6B7C` | Secondary text, timestamps, helper labels |
| `surface` | `#FFFFFF` | Card containers, modals, popovers |
| `canvas` | `#F6FAFD` | Global page body background |
| `border` | `#D9E4EE` | Subtle borders, table dividers |

### Severity Indicators (Always accompany with descriptive text)
- **Low Severity**: `#2E9E5B` (Green)
- **Medium Severity**: `#F5A524` (Amber)
- **High Severity**: `#D92D20` (Red)

**Typography**: Plus Jakarta Sans (Weights: 400 Regular, 500 Medium, 600 SemiBold, 700 Bold, 800 ExtraBold).

---

## 6. Installation & Local Setup

### Prerequisites
- Python 3.11+
- Git

### 1. Clone & Navigate to Repository
```bash
git clone <repository-url>
cd saferoute
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
Copy the template and provide valid credentials:
```bash
cp .env.example .env
```
Populate `.env` with your project parameters:
```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-supabase-anon-key
SUPABASE_SERVICE_KEY=your-supabase-service-role-key
VISION_API_KEY=your-gemini-or-vision-api-key
SECRET_KEY=your-random-32-byte-secret-key
```

---

## 7. Running the Application

Start the local development server with auto-reload:
```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open your browser and navigate to:
- **Welcome Endpoint**: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **Health Check**: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- **Interactive OpenAPI Docs**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

### Verification with curl
```bash
curl http://127.0.0.1:8000/health
```
Expected output:
```json
{
  "status": "healthy",
  "application": "SafeRoute"
}
```

---

## 8. Running Automated Tests

Run the test suite using `pytest`:
```bash
pytest tests/ -v
```

---

## 9. Team Roles & GitHub Collaboration Rules

SafeRoute is developed by a three-member collaborative engineering team:

| Role | Domain Ownership | Responsibilities |
|---|---|---|
| **Member A** | Backend & Database | Supabase setup, PostGIS schema, SQL functions/triggers, Auth, core APIs, deployment |
| **Member B** | Frontend & UI | Base layout, design system components, map UI, report screens, responsive design |
| **Member C** | AI, Routing & Analytics | Vision AI categorization, severity scoring engine, OSRM routing, analytics dashboard |

### Team Workflow Rules
1. **Branch Naming**: `feat/<area>-<issue-number>-short-name` (e.g., `feat/infra-S1-repo-setup`, `feat/backend-8-reports-api`).
2. **Pull Requests Only**: Never push directly to `main`. Every change enters through a Pull Request reviewed and merged by Member A.
3. **Issue Linking**: Include `Closes #<issue-number>` in every PR description.
4. **Fast Merges**: Pull `main` before starting new tasks; merge working branches frequently (at least every 3 hours).
5. **Zero Secrets**: Never commit `.env` files or API credentials to git.

---

## 10. Development Roadmap

- **Milestone 1: Core Loop (Day 1)**
  - `S1`: Repository setup, conventions, and foundation (*Current Stage*)
  - `A1`-`A6`: Database schema, PostGIS functions, triggers, and seed data
  - `B1`-`B5`: Base layout, landing/auth pages, interactive map, and reporting form
  - `A7`-`A8`: Auth guards, reports API with 30m duplicate detection
  - `C1`: Severity scoring service and unit test suite
  - `S2`: Day 1 Integration checkpoint
- **Milestone 2: Smart Features & Production Delivery (Day 2)**
  - `C2`: AI vision category suggestion with fallback
  - `C3`: Hazard-aware route planning with OSRM scoring
  - `C4`: Hotspot analytics and reporting metrics
  - `A9`-`A10`: Votes/comments endpoints, admin escalation API
  - `B6`-`B8`: Report detail view, escalations dashboard, route comparison UI
  - `B9`: Responsive design & accessibility polish
  - `A11`: HTTPS cloud deployment
  - `C5`-`C6`: Field validation run, demo script, and final rehearsal
  - `S3`: Final submission and tag `v1.0`
