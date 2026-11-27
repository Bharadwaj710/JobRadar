# JobRadar: Isolated Demo Environment Guide
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Date:** 2026-11-27
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Database Path:** [data/demo/jobradar_demo.db](file:///D:/JB/JobRadar/data/demo/jobradar_demo.db)
**Status:** VERIFIED & READY FOR RECORDING

---

## 1. Overview & Architectural Isolation

This guide provides instructions to initialize, run, reset, and navigate the isolated JobRadar demonstration environment designed for the Spektra Systems technical evaluation.

### Core Isolation Guarantees
- **Zero Production Database Modification:** The demonstration runs against an isolated SQLite database file: [data/demo/jobradar_demo.db](file:///D:/JB/JobRadar/data/demo/jobradar_demo.db). The production database ([data/jobradar.db](file:///D:/JB/JobRadar/data/jobradar.db)) is protected by runtime guards and remains 100% untouched.
- **Zero Live ATS Scraper Dependency:** All demonstration scenarios are pre-computed using JobRadar's actual deterministic matching algorithms and eligibility engines. No external network requests, career board rate limits, or changing postings can disrupt the demo.
- **Synthetic Candidate & Fictional Postings:** All demonstration records use synthetic data. Job postings are clearly marked with `[Demo]` prefixes (e.g. `[Demo] Stripe Technologies`, `[Demo] Vercel Cloud`) to avoid any confusion with live real-world employer vacancies.
- **True Algorithmic Scoring:** Technical scores (0-100) and eligibility verdicts (ELIGIBLE, NEEDS_VERIFICATION, NOT_ELIGIBLE) are computed dynamically by `CandidateMatchingService`. No displayed scores or verdicts are hardcoded.

---

## 2. Quick-Start PowerShell Commands

All commands should be executed from the repository root (`D:\JB\JobRadar`) in PowerShell:

### Step 1: Initialize / Reset the Demo Database
```powershell
Set-Location D:\JB\JobRadar

# Deterministically seed the demo database (with clean reset)
uv run python scripts/prepare_demo_data.py --reset
```
*Expected Output:*
- Confirms safety check against production database.
- Creates synthetic candidate Alex Dev (`cand_...`).
- Confirms Profile Version 1.
- Seeds 6 synthetic demonstration job postings into inventory.
- Generates 24 assessments across all 4 J4 career tracks.
- Sets up application tracking and history log.
- Displays session cookie string for direct login.

### Step 2: Launch the Web Application in Demo Mode
```powershell
# Point the database environment variable to the demo database
$env:JOBRADAR_DB_PATH = "data/demo/jobradar_demo.db"

# Start the web dashboard on http://127.0.0.1:8080
uv run jobradar web
```

### Step 3: Access the Candidate Dashboard
Open your web browser and navigate to:
```
http://127.0.0.1:8080/candidate/dashboard
```
*Note on Authentication & Session Initialization:*
The demo seeder deterministically provisions synthetic candidate Alex Dev and signs an HMAC-SHA256 candidate session token (`<candidate_id>.<signature>`) using the local demo web token. The exact session cookie string (`jobradar_candidate_session=<candidate_id>.<signature>`) is output to the console during Step 1.

To initialize and use the demo session:
1. Run `uv run python scripts/prepare_demo_data.py --reset`.
2. Notice the `Session Cookie:` line emitted in the summary table.
3. Open `http://127.0.0.1:8080/candidate/dashboard` in your browser.
4. If testing in an isolated or incognito browser profile, set the cookie in your browser developer tools (Application > Cookies > `jobradar_candidate_session`) or run Playwright tests which automatically inject this cookie.
5. In standard browser sessions, visiting `/candidate/dashboard` automatically resolves the local demo candidate context.

### Step 4: Verify Demo Integrity at Any Time
```powershell
uv run python scripts/prepare_demo_data.py --verify
```

---

## 3. Synthetic Candidate Profile

The demo environment features a complete, realistic fresher candidate profile:

- **Candidate Name:** Alex Dev
- **Email:** `alex.demo@synthetic-test.local`
- **Location:** Bengaluru, Karnataka, India
- **Primary Education:**
  - Degree: B.Tech in Computer Science and Engineering
  - Institution: National Institute of Technology
  - Graduation Batch: Class of 2026 (Fresh Graduate)
  - CGPA: 8.9 / 10
- **Technical Skills:**
  - Core: Python, FastAPI, Docker, PostgreSQL, Redis, PyTorch, Pytest, Git, REST APIs
- **Internship Experience:**
  - Software Engineering Intern at `[Demo] Synthetix Labs` (Jan 2025 - Present)
  - Responsibilities: Built asynchronous microservices with FastAPI and PostgreSQL; implemented Redis caching; authored Pytest integration test suites.
- **Projects:**
  - Distributed Stream Processing Pipeline (Python, FastAPI, Redis, Docker)
  - Neural Semantic Search Service (PyTorch, Python, FastAPI)
- **Target Role Tracks:** AI Backend (`ai_backend`), Software Engineering (`sde`), Full Stack (`full_stack`)

---

## 4. Demonstration Matrix (Six Curated Scenarios)

The demo environment includes 6 synthetic postings designed to showcase the full depth of JobRadar's intelligence:

| Scenario / Job Title | Synthetic Company | Primary Track | Status | What Evaluators See |
|---|---|---|---|---|
| **1. AI Systems Engineer** | `[Demo] Stripe Technologies` | `ai_backend` | **ELIGIBLE** (Confirmed Match) | High technical match score (94%), batch 2026 match, 0-1 yr exp, covered skills badges. Ranks #1 in Best Match. |
| **2. Full Stack Engineer** | `[Demo] Vercel Cloud` | `full_stack` | **ELIGIBLE** (Confirmed Match) | High full-stack alignment (89%), TypeScript/FastAPI fit, batch 2026 match. |
| **3. Distributed Platform Engineer** | `[Demo] Databricks Systems` | `ai_backend` | **NEEDS_VERIFICATION** (Discovery) | High technical relevance (91%), but graduation year omitted by ATS. Surfaced in amber discovery banner so candidate does not miss it. |
| **4. Backend Infrastructure Engineer** | `[Demo] Cloudflare Edge` | `sde` | **NEEDS_VERIFICATION** (Discovery) | Systems engineering role (86% fit) with unspecified experience bounds. Surfaced in amber discovery view. |
| **5. Lead Backend Architect** | `[Demo] Oracle Enterprise` | `sde` | **NOT_ELIGIBLE** (Transparency) | High technical score (85%), but hard-gated out because job requires 5+ years experience and 2020-2022 batch. Displays exact evidence citations in transparency drawer. |
| **6. SDET / Automation Engineer** | `[Demo] Rubrik Core` | `sdet` | **ELIGIBLE** (Track Sorting) | Specializes in automated testing and platform quality. Ranks #1 when SDET career track filter is selected. |

---

## 5. Walkthrough Script for Demo Recording

Follow this recommended 5-minute video presentation sequence:

### Segment 1: Candidate Dashboard & Design System (0:00 - 1:15)
- Open `http://127.0.0.1:8080/candidate/dashboard`.
- Point out the Vercel-inspired aesthetic: Geist typography, 1px border cards, KPI summary cards (4 Tracks, Confirmed Matches, Opportunities Pending Verification, Ineligible, Tracked Apps).
- Toggle between **Light Theme** and **Dark Theme** using the theme toggle in the navigation header to show instant contrast and zero layout shift.
- Point out candidate identity: Alex Dev, NIT Karnataka, Class of 2026.

### Segment 2: Confirmed Matches vs Opportunity Discovery (1:15 - 2:30)
- Highlight the **Confirmed Matches** section:
  - `[Demo] Stripe Technologies: AI Systems Engineer` (Score: 94%, Status: ELIGIBLE).
  - `[Demo] Vercel Cloud: Full Stack Engineer` (Score: 89%, Status: ELIGIBLE).
- Highlight the amber **Opportunities Pending Verification** banner:
  - Explain the GenAI engineering insight: Real-world ATS job posts often omit graduation years. Instead of discarding these roles, JobRadar surfaces them in an amber discovery strip so candidates never miss high-value opportunities.
  - Showcase `[Demo] Databricks Systems` (91% score) and `[Demo] Cloudflare Edge` (86% score).

### Segment 3: Ineligible Job Transparency View (2:30 - 3:30)
- Navigate to `http://127.0.0.1:8080/candidate/dashboard?eligibility=not_eligible` or select "Ineligible Roles (Transparency Log)" from the filter dropdown.
- Inspect `[Demo] Oracle Enterprise: Lead Backend Architect`.
- Explain the architectural value:
  - Technical score is high (85%) because the candidate knows Python and Docker.
  - However, the hard constraints gate it out: "Requires minimum 5 years experience; target graduation batch 2020 to 2022."
  - This demonstrates strict semantic separation between skill matching and eligibility verification.

### Segment 4: Four-Track J4 Career Filters (3:30 - 4:15)
- Return to the dashboard and click the **SDET** track filter pill (`?track=sdet`).
- Observe how the ranking dynamically pivots:
  - `[Demo] Rubrik Core: SDET / Automation Engineer` jumps to #1.
  - Demonstrates that track filters use track-specific technical scoring without cross-track score leakage.

### Segment 5: Deep-Dive Job Fit Breakdown & Application Tracking (4:15 - 5:00)
- Click on `[Demo] Stripe Technologies: AI Systems Engineer` to open `/candidate/jobs/demo_job_01_stripe_ai`.
- Walk through the fit breakdown:
  - Match Score: 94 / 100 with evidence breakdown.
  - Covered skills (green badges): Python, FastAPI, Docker, PostgreSQL.
  - Direct apply link pointing to ATS endpoint.
- Click **Applications** in the navigation bar (`/candidate/applications`):
  - Show the Kanban board: Stripe role is in **Interviewing** column; Vercel role is in **Applied** column.
  - Show history notes: "Technical screening completed; system design round scheduled."
  - Highlight that candidate application tracking is completely isolated and never mutates the global job inventory.

---

## 6. Fresh Demo Screenshots Portfolio

All visual proof screenshots have been freshly captured from the running demo application:

| Screenshot | File Path | What It Shows |
|---|---|---|
| **Dashboard (Light)** | [demo_01_dashboard_light.png](file:///D:/JB/JobRadar/tests/screenshots/demo_01_dashboard_light.png) | Candidate dashboard with KPI tiles, Confirmed Matches, and Needs Verification discovery banner. |
| **Dashboard (Dark)** | [demo_02_dashboard_dark.png](file:///D:/JB/JobRadar/tests/screenshots/demo_02_dashboard_dark.png) | High-contrast dark theme rendering with accessible status badges. |
| **SDET Track Filter** | [demo_03_track_sdet_filter.png](file:///D:/JB/JobRadar/tests/screenshots/demo_03_track_sdet_filter.png) | Track-specific Best Match ranking showing Rubrik SDET role ranked #1. |
| **Ineligible Transparency** | [demo_04_ineligible_drawer.png](file:///D:/JB/JobRadar/tests/screenshots/demo_04_ineligible_drawer.png) | Transparency log showing Oracle job excluded with exact graduation batch and experience evidence. |
| **Job Fit Breakdown** | [demo_05_job_detail_stripe.png](file:///D:/JB/JobRadar/tests/screenshots/demo_05_job_detail_stripe.png) | Detailed job page for Stripe AI role showing requirements, skill coverage, and fit explanation. |
| **Applications Board** | [demo_06_applications_board.png](file:///D:/JB/JobRadar/tests/screenshots/demo_06_applications_board.png) | Kanban board tracking pipeline stages (Interviewing and Applied) with custom notes. |
| **Mobile Viewport** | [demo_07_mobile_dashboard.png](file:///D:/JB/JobRadar/tests/screenshots/demo_07_mobile_dashboard.png) | Responsive mobile rendering (375x812) with bottom navigation tab bar. |

---

## 7. Required Environment Variables

When running in demo mode, JobRadar only requires a web authentication secret. All variables in `.env` are supported:

```ini
# Required for web session signing
JOBRADAR_WEB_TOKEN=

# Optional LLM API keys (matching runs deterministically without external LLM keys)
GEMINI_API_KEY=
GROQ_API_KEY=

# Optional Notification Sinks (disabled during local demo)
SMTP_HOST=
SMTP_PORT=
SMTP_USER=
SMTP_PASSWORD=
EMAIL_TO=

# Optional Scraping Sources
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PHONE=

# Notion Mirroring (disabled by default)
NOTION_API_KEY=
NOTION_DATABASE_ID=
```

---

## 8. Summary Checklist Before Recording

- [x] Demo database seeded at `data/demo/jobradar_demo.db`.
- [x] Production database protected and invariant (SHA-256 confirmed).
- [x] Synthetic candidate Alex Dev confirmed in Profile Version 1.
- [x] All 6 demonstration job scenarios evaluated across 4 tracks.
- [x] Fresh visual screenshots captured in `tests/screenshots/demo_*.png`.
- [x] Light and dark themes verified.
- [x] Mobile responsive layout verified.
- [x] Track-specific Best Match ordering verified.
- [x] Application tracking and history log verified.
