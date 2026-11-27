# JobRadar: Spektra Systems Submission Package
**Position:** AI-Native Software Engineer - Trainee
**Candidate Submission Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks` (Base HEAD: `fdd739b`)
**Date:** November 27, 2026
**License:** MIT License (Copyright (c) 2026 ThePlator)
**Readiness Status:** READY FOR HUMAN REVIEW & DEMO RECORDING

---

## 1. Executive Summary

This submission package documents the comprehensive transformation of **JobRadar** from an open-source personal job-search script into an enterprise-grade, multi-candidate, AI-native career intelligence platform for Spektra Systems.

JobRadar autonomously discovers hiring vacancies from applicant tracking systems (Greenhouse, Lever, Ashby, Workday, SmartRecruiters), reads postings, performs deterministic and LLM-assisted requirement extraction, and evaluates candidates across four specialized career tracks (**AI Backend**, **Full Stack**, **SDE / Systems**, and **SDET / QA**).

### Key Technical Achievements
- **Multi-Candidate Isolation:** Cryptographically signed session tokens (HMAC-SHA256) enforce complete boundary protection, zero Insecure Direct Object References (IDOR), and strict candidate privacy.
- **Four-Track J4 Career Engine:** Separates technical skill alignment (0-100 score) from non-negotiable eligibility criteria (graduation year, degree, experience ceiling).
- **Opportunity Discovery:** High-potential opportunities that omit graduation batch metadata are surfaced in an amber discovery strip (`NEEDS_VERIFICATION`) rather than dropped, solving real-world ATS sparsity.
- **Vercel-Inspired UI Redesign:** Responsive, accessible design system featuring Geist typography, native light/dark theme toggle, and mobile layout.
- **Exhaustive Quality Gates:** 994 passed pytest unit/integration tests, 6 Playwright browser E2E tests, clean Ruff linting and formatting across 180 files, and clean Mypy strict typing across 102 source files.
- **Production Database Invariance:** The production database ([data/jobradar.db](file:///D:/JB/JobRadar/data/jobradar.db)) remained 100% untouched throughout all testing (SHA-256: `5de19c40567e23c5b95974ccdeed9ca7fd0f4b002f9fe2be862e5ec2b4021f91`).

---

## 2. Submission Documentation Structure

This dossier is organized into 10 structured reference documents:

| Document | Description |
|---|---|
| [01_project_overview.md](file:///D:/JB/JobRadar/submission/01_project_overview.md) | Project identity, problem statement, upstream provenance, and contribution scope. |
| [02_system_architecture.md](file:///D:/JB/JobRadar/submission/02_system_architecture.md) | Multi-candidate architecture, data model, J4 career tracks, and security isolation. |
| [03_end_to_end_pipeline.md](file:///D:/JB/JobRadar/submission/03_end_to_end_pipeline.md) | Lifecycle from ATS discovery to ingestion, extraction, matching, and application tracking. |
| [04_ai_agent_engineering_methodology.md](file:///D:/JB/JobRadar/submission/04_ai_agent_engineering_methodology.md) | AI-native engineering discipline, prompt constraints, and human-in-the-loop oversight. |
| [05_selected_agent_conversations.md](file:///D:/JB/JobRadar/submission/05_selected_agent_conversations.md) | Seven curated authentic developer-agent engineering interactions and lessons learned. |
| [06_code_review_and_quality_gates.md](file:///D:/JB/JobRadar/submission/06_code_review_and_quality_gates.md) | Ruff, Mypy, test automation frameworks, and Git security audit results. |
| [07_testing_and_validation_evidence.md](file:///D:/JB/JobRadar/submission/07_testing_and_validation_evidence.md) | Pytest logs, Playwright browser test outputs, and production database invariance proofs. |
| [08_known_limitations.md](file:///D:/JB/JobRadar/submission/08_known_limitations.md) | Known technical constraints, toolchain dependencies, and planned architectural evolutions. |
| [09_demo_walkthrough.md](file:///D:/JB/JobRadar/submission/09_demo_walkthrough.md) | Step-by-step 5-minute evaluator presentation script and quick-launch commands. |
| [SCREENSHOT_INDEX.md](file:///D:/JB/JobRadar/submission/SCREENSHOT_INDEX.md) | Catalogue of the 9 curated visual proof screenshots. |

---

## 3. Quick-Start Demo Commands

To run the verified isolated demonstration environment:

```powershell
Set-Location D:\JB\JobRadar

# 1. Initialize the isolated demo database (never touches production data)
uv run python scripts/prepare_demo_data.py --reset

# 2. Launch the web application pointing to the demo database
$env:JOBRADAR_DB_PATH = "data/demo/jobradar_demo.db"
uv run jobradar web
```

Open `http://127.0.0.1:8080/candidate/dashboard` to explore the live candidate interface.
