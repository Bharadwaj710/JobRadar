# 09. Live Demo Walkthrough Guide & Recording Script
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks`
**Target Duration:** 5 minutes

---

## 1. Quick-Launch Commands

To run the verified isolated demonstration environment:

```powershell
Set-Location D:\JB\JobRadar

# 1. Deterministically seed or reset the isolated demo database
uv run python scripts/prepare_demo_data.py --reset

# 2. Point to the demo database and launch the web server
$env:JOBRADAR_DB_PATH = "data/demo/jobradar_demo.db"
uv run jobradar web
```

Open `http://127.0.0.1:8080/candidate/dashboard` in a browser.

---

## 2. Demonstration Flow & Presenter Script

### Segment 1: Architecture & Design System (0:00 - 1:15)
- **URL:** `http://127.0.0.1:8080/candidate/dashboard`
- **Actions:**
  - Introduce JobRadar as an open-source, AI-native job intelligence agent originally created by Sameer (@ThePlator) and substantially re-architected with multi-candidate isolation and the J4 career tracks engine.
  - Showcase the Vercel-inspired UI: Geist typography, subtle 1px border cards, and KPI summary tiles (4 Tracks, Confirmed Matches, Opportunities Pending Verification, Ineligible Roles, Tracked Apps).
  - Click the **Dark Mode** toggle in the header to demonstrate accessible color contrast and instant theme switching. Return to Light mode.
  - Highlight the active candidate profile: **Alex Dev**, B.Tech CSE Class of 2026, NIT Karnataka.
- **Key Talking Point:** *"JobRadar is multi-tenant and local-first. Candidate actions never mutate the global scraped inventory, and session tokens are cryptographically signed with HMAC-SHA256."*

---

### Segment 2: Confirmed Matches vs Real-Time Opportunity Discovery (1:15 - 2:30)
- **URL:** `http://127.0.0.1:8080/candidate/dashboard`
- **Actions:**
  - Scroll to the **Confirmed Matches** section.
  - Point out `[Demo] Stripe Technologies: AI Systems Engineer` (94% match) and `[Demo] Vercel Cloud: Full Stack Engineer` (89% match). Explain that both have verified 2026 batch eligibility.
  - Draw attention to the eye-catching amber **Opportunities Pending Verification** banner.
  - Highlight `[Demo] Databricks Systems: Distributed Platform Engineer` (91% match) and `[Demo] Cloudflare Edge: Backend Infrastructure Engineer` (86% match).
- **Key Talking Point:** *"In real-world ATS systems, employers frequently forget to specify graduation years. Most automated filters drop these jobs, causing a zero-eligible problem. JobRadar solves this with an Opportunity Discovery layer: we preserve semantic truth by marking them NEEDS_VERIFICATION and surfacing them in an amber discovery strip so candidates never miss high-value postings."*

---

### Segment 3: Ineligible Job Transparency View (2:30 - 3:30)
- **URL:** `http://127.0.0.1:8080/candidate/dashboard?eligibility=not_eligible`
- **Actions:**
  - Select "Ineligible Roles (Transparency Log)" from the filter dropdown.
  - Inspect `[Demo] Oracle Enterprise: Lead Backend Architect`.
  - Point out the match metrics: technical score is 85% because the candidate knows Python and Docker.
  - Point out the red `NOT ELIGIBLE` badge and reason: "Requires minimum 5 years experience; target graduation batch 2020 to 2022."
- **Key Talking Point:** *"We enforce strict semantic separation between technical match scores and hard-constraint eligibility. A fresher candidate who knows all the skills is still disqualified from a senior role. JobRadar explains the exact reason transparently rather than silently hiding the job."*

---

### Segment 4: Four-Track J4 Career Filters (3:30 - 4:15)
- **URL:** `http://127.0.0.1:8080/candidate/dashboard`
- **Actions:**
  - Click the **SDET** career track filter pill (`?track=sdet`).
  - Observe the dynamic re-ranking: `[Demo] Rubrik Core: SDET / Automation Engineer` jumps to #1 with an 88% track score.
  - Switch back to the **AI Backend** track pill to show `[Demo] Stripe Technologies` returning to #1.
- **Key Talking Point:** *"Track filtering is mathematically isolated. Ranking by SDET evaluates only the candidate's testing and QA tools without cross-track score pollution."*

---

### Segment 5: Job Fit Breakdown & Application Tracking (4:15 - 5:00)
- **URL:** `http://127.0.0.1:8080/candidate/jobs/demo_job_01_stripe_ai` and `/candidate/applications`
- **Actions:**
  - Open the job detail page for Stripe AI Systems Engineer.
  - Inspect the requirement coverage: green badges for covered skills (Python, FastAPI, Docker, PostgreSQL) and direct apply link.
  - Click **Applications** in the navigation header to open the Kanban board.
  - Show the progression: Stripe role is under **Interviewing**; Vercel role is under **Applied**.
  - Show candidate notes: "Technical screening completed; system design round scheduled."
- **Key Talking Point:** *"The entire candidate workflow is local, durable, and private. JobRadar gives early-career software engineers the tools to find, evaluate, and track high-impact roles autonomously."*

---

## 3. Pre-Recording Verification Checklist

Before starting video recording:
- [x] Primary monitor resolution set to 1920x1080 (or browser window set to 1280x800).
- [x] Browser context clean with bookmarks bar hidden.
- [x] Demo database seeded and verified (`prepare_demo_data.py --verify`).
- [x] Web server active on `http://127.0.0.1:8080` pointing to demo database.
- [x] Production database protected and invariant.
- [x] Theme toggle tested (Light and Dark).
