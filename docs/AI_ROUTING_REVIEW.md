# SafeRoute — AI, Routing & Analytics Engineering Review

> **Document Scope**: Complete end-to-end review of the SafeRoute Artificial Intelligence services, Computer Vision category suggestion, Civic Conversational AI assistant, OSRM hazard-aware route planner, spatial buffer scoring, and civic analytics engine.
> **Team Alignment**: Member C (AI, Routing & Analytics Engineering)
> **Repository Location**: `d:\SafeRoute-2`

---

## 1. Technology Stack & External Services

SafeRoute combines state-of-the-art Multimodal Generative AI, Graph-based Routing Engines, and Geospatial Data Analytics to empower urban commuters with real-time hazard awareness:

| Capability | Technology / Provider | Configuration / Spec | Rationale & Architecture |
| :--- | :--- | :--- | :--- |
| **Vision AI Model** | Google Gemini Vision (`gemini-2.5-flash`) | Google AI Studio REST API | High-speed multimodal visual reasoning for zero-shot hazard classification and confidence assessment. |
| **Conversational AI** | Google Gemini + Grounded RAG Engine | Gemini API with live SQL stats injection | Intelligent Civic Assistant answering commuter questions about road conditions, resolution statistics, and danger zones. |
| **Routing Engine** | OSRM (Open Source Routing Machine) | `router.project-osrm.org` | Open-source road network engine generating driving routes, distance calculations, and GeoJSON polylines. |
| **Geospatial Buffer Intersection** | PostGIS `ST_DWithin` | Spherical buffer ($25\text{ m}$) | Intersects route polylines with live community hazard points to calculate localized road risk. |
| **Density Heatmap** | Leaflet.heat | WebGL/Canvas intensity rendering | Aggregates geographic point density and severity weights to visualize high-risk urban hotspots. |
| **HTTP Client** | HTTPX (Async/Sync) | Timeouts: 8.0s (Vision), 6.0s (OSRM) | Resilient non-blocking network calls with automatic fallback mechanisms. |
| **Image Privacy** | Pillow (PIL) | 100% EXIF/GPS/XMP stripping | Sanitizes citizen photos before transmitting to external AI models. |

---

## 2. Directory & Module Architecture

```
app/
├── routers/
│   ├── ai.py                   # POST /api/ai/assistant (Civic AI Commuter queries)
│   ├── reports.py              # POST /reports/suggest-category (Vision AI endpoint)
│   ├── routes.py               # GET /routes & POST /api/routes/plan (Route planner UI & API)
│   └── analytics.py            # GET /analytics & GET /api/analytics (Dashboard & API)
└── services/
    ├── vision_ai.py            # Image preprocessing, Gemini API prompt engineering, category normalization
    ├── ai_assistant.py         # Grounded civic conversational RAG with live database analytics injection
    ├── routing.py              # OSRM API client, cached demo route fallback, 25m buffer math, risk scoring
    └── analytics.py            # KPI counters, category/status breakdown, top affected areas, heatmap generator
```

---

## 3. Computer Vision Category Suggestion (`app/services/vision_ai.py`)

### Problem Solved
Commuters often struggle to categorize technical road hazards accurately (e.g., distinguishing between a pothole and a structural road break) or abandon manual reporting due to form fatigue.

### Implementation Architecture
1. **Endpoint**: `POST /reports/suggest-category` (Accepts multipart form file `image`).
2. **Metadata Sanitization**:
   - Validates MIME type (`image/jpeg`, `image/png`, `image/webp`) and enforces $\le 5\text{ MB}$ size limit.
   - Passes image through `validate_and_strip_exif(file_bytes)` using Pillow to strip 100% of EXIF, GPS coordinates, device identifiers, and XMP metadata.
3. **Model Invocation**:
   - Converts sanitized image to Base64.
   - Queries Google Gemini Vision API (`gemini-2.5-flash`) via HTTPX with an 8.0-second timeout.
4. **Structured JSON Output Schema**:
   The vision model is constrained via system instructions to output strictly formatted JSON:
   ```json
   {
     "category": "pothole" | "road_break" | "streetlight" | "footpath" | "other" | "none",
     "confidence": 0.0 - 1.0,
     "reason": "One concise sentence describing the visual evidence."
   }
   ```
5. **Normalization & Synonyms**:
   - `_normalize_category()` maps common model colloquialisms into approved database categories:
     - "crater" $\rightarrow$ `pothole`
     - "fissure", "crack", "shoulder collapse" $\rightarrow$ `road_break`
     - "sidewalk", "pavement" $\rightarrow$ `footpath`
     - "street light", "lamp post" $\rightarrow$ `streetlight`
6. **Non-Blocking Fallback Guarantee**:
   - If the API key is missing, network times out, or the image cannot be classified, the endpoint returns:
     ```json
     {"category": null, "confidence": null, "reason": null}
     ```
   - **Critical Rule**: Vision failure **never** throws an HTTP 500 and **never** blocks the user from submitting a report. The user simply selects the category manually.

---

## 4. Grounded Civic AI Assistant (`app/services/ai_assistant.py`)

### Problem Solved
Commuters need quick, factual answers about their local infrastructure (e.g., "Are the potholes near Forum Mall fixed?", "What percentage of road issues have been resolved?"). Standard LLMs hallucinate road facts; SafeRoute grounds answers directly in live database state.

### RAG-Style Implementation Architecture
1. **Endpoint**: `POST /api/ai/assistant` (Accepts JSON `{"query": "string"}`).
2. **Live Context Retrieval (`_get_live_civic_context`)**:
   - Queries `public.hazard_reports` under the authenticated user's RLS session.
   - Computes live aggregated metrics via `compute_analytics()`:
     - Total reports count
     - Open, escalated, and fixed counts
     - Resolution rate percentage: $\frac{\text{fixed}}{\text{total}} \times 100$
     - Average resolution duration in hours
     - Category breakdown
     - Top 5 affected hotspot areas in Kukatpally / Hyderabad
3. **Grounded Generation & Rule-Based Fallback**:
   - Injects the live database context into the Gemini system prompt:
     > *"You are the SafeRoute Civic Assistant. Answer commuter questions accurately using ONLY the verified database statistics provided below. Never invent hazard counts or repair statuses."*
   - Includes a deterministic, offline rule-based synthesizer (`_rule_based_civic_answer`):
     - If the Gemini API key is unset or external quota is exceeded, the engine matches commuter intents (fixed issues, active potholes, resolution rates, dangerous areas) and constructs mathematically exact responses directly from the aggregated data.
4. **Response Structure**:
   ```json
   {
     "reply": "SafeRoute currently tracks 24 reports across Kukatpally. 10 hazards have been completely fixed (41.7% resolution rate). 8 are escalated to civic authorities.",
     "suggested_actions": ["Show Fixed Hazards", "View Escalations", "Check Top Danger Zones"],
     "stats": {
       "total_reports": 24,
       "fixed_count": 10,
       "open_count": 6,
       "escalated_count": 8,
       "resolution_rate_percent": 41.7
     }
   }
   ```

---

## 5. Hazard-Aware Route Planner (`app/services/routing.py`)

### Problem Solved
Standard navigation systems (Google Maps, Apple Maps) optimize strictly for travel time or distance, frequently routing two-wheelers and cars through broken roads, open craters, and unlit streets. SafeRoute balances driving time against community-reported hazard severity.

### Mathematical Risk & Scoring Model

#### 1. Severity Multipliers
Each intersecting active hazard contributes to the route's risk based on its severity band:
- **Low Severity** (Score 0–39): Multiplier $= 0.5$
- **Medium Severity** (Score 40–69): Multiplier $= 0.8$
- **High Severity** (Score 70–100): Multiplier $= 1.0$
*(Note: Resolved hazards with `status = 'fixed'` contribute $0.0$ risk).*

#### 2. Total Route Risk Formula
$$\text{Route Risk} = \sum_{i=1}^{N} \left(\text{Severity Score}_i \times \text{Multiplier}_i\right)$$

#### 3. Composite Route Score Formula
The composite route score balances duration (in minutes) against total road risk:
$$\text{Route Score} = \text{Duration (minutes)} + 0.05 \times \text{Route Risk}$$

### Route Planning Execution Flow
1. **Coordinates Parsing**: User provides Origin `(lat, lng)` and Destination `(lat, lng)`.
2. **OSRM Driving Engine Query**:
   - Queries `https://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}` with `overview=full`, `geometries=geojson`, and `alternatives=true`.
   - Timeout set to 6.0 seconds.
3. **Resilient Fallback Routing**:
   - If OSRM is unreachable or times out, the service automatically serves a deterministic, cached demo route covering Kukatpally Y Junction to Hitec City / JNTU.
   - Sets `is_fallback_route: true` with a clear user notice: *"Showing simulated route due to network timeout."*
4. **Spatial Hazard Intersection (PostGIS)**:
   - Route GeoJSON geometry is passed to PostGIS function `hazards_near_route(route_geojson, buffer_m=25.0)`.
   - PostGIS identifies all active hazard points within a 25-meter spherical corridor around the polyline.
5. **Ranking & Recommendation**:
   - Alternative routes are ranked in ascending order of `Route Score`.
   - The route with the lowest score is flagged `recommended = True`.
6. **Mandatory Legal Disclaimer**:
   Every response includes the locked civic disclaimer:
   > *"Routes are ranked using community-reported hazards only. This does not guarantee any route is safe."*

---

## 6. Civic Analytics & Heatmap Engine (`app/services/analytics.py`)

### Capabilities & Key Metrics Computed

1. **Total Reports**: Total hazard volume recorded across the platform.
2. **Community Verification Rate**: Percentage of reports that have received at least one community verification vote:
   $$\text{Verification Rate} = \frac{\text{Reports with } (\text{confirm} + \text{dispute} + \text{fixed}) \ge 1}{\text{Total Reports}} \times 100$$
3. **Duplicate Detection Rate**: Percentage of reports successfully linked to a primary report via `duplicate_of`.
4. **Escalation Volume**: Count of reports that have been formally escalated to municipal authorities.
5. **Average Time to Fix (MTTR)**: Mean duration in hours between `created_at` and `fixed_at` for all resolved hazards.
6. **Category & Status Breakdown**: Absolute counts and percentages for each category and lifecycle state.
7. **Spatial Clustering (Top 5 Affected Areas)**:
   - Groups hazards into urban sectors (e.g., Kukatpally Y Junction, JNTU Metro, KPHB Phase 1).
   - Computes average severity score and report density per sector.
8. **Leaflet.heat Density Matrix**:
   - Generates an array of `[lat, lng, intensity]` coordinates.
   - Normalized intensity: $\text{intensity} = \frac{\text{severity\_score}}{100}$.
   - Renders a smooth, color-graded heat density map on `/analytics`.
9. **Seed Data Toggle (`exclude_seed`)**: Allows civic analysts to toggle between live production reports and baseline seed benchmarks.

---

## 7. AI, Routing & Analytics Use Cases

### Use Case 1: Fast Citizen Reporting with One-Tap AI Categorization
1. Citizen spots a broken manhole cover while walking in Kukatpally.
2. Uploads a photo; taps "Suggest Category with AI".
3. Backend strips all EXIF metadata, calls Gemini Vision API.
4. Vision model identifies the manhole cover as a collapsed footpath section (`footpath`, confidence 0.92, reason "Broken pedestrian concrete slab").
5. Form selects "Footpath", fills description, and saves the commuter 30 seconds of manual typing.

### Use Case 2: Commuter Natural Language Road Inquiry
1. A commuter wants to know how effective civic repairs have been recently.
2. Opens the Civic AI Assistant widget and types: *"How many hazards have been fixed so far?"*
3. Assistant executes RAG query against database:
   - Total reports: 28, Fixed: 12, Resolution Rate: 42.9%, Avg fix time: 36.4 hours.
4. Assistant replies:
   > *"A total of 12 road hazards have been verified as fixed in our coverage area, representing a 42.9% resolution rate. On average, repairs are confirmed within 36.4 hours of reporting."*
5. Suggests follow-up chips: `Show Fixed Hazards` and `Top Danger Zones`.

### Use Case 3: Pothole-Avoidance Route Planning
1. Two-wheeler commuter inputs route from Kukatpally Y Junction to Forum Mall.
2. OSRM returns two alternatives:
   - **Route 1 (Main Arterial)**: 12 minutes, but intersects 4 severe potholes (Total Risk = 320.0). Composite Score $= 12 + 0.05 \times 320 = 28.0$.
   - **Route 2 (Parallel Ring Road)**: 15 minutes, intersects 0 hazards (Total Risk = 0.0). Composite Score $= 15 + 0.05 \times 0 = 15.0$.
3. SafeRoute displays Route 2 as **"Recommended (Safest)"**, warning the commuter that Route 1 contains 4 high-severity potholes despite being 3 minutes faster.
