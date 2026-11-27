# 08. Known Limitations & Technical Context
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks`

---

## 1. Overview

This document provides transparent technical context on intentional architectural boundaries, environment dependencies, and future engineering milestones for JobRadar.

---

## 2. Technical Limitations & Operational Context

### 1. Windows LaTeX Toolchain Dependency
- **Context:** JobRadar's resume builder compiles tailored LaTeX documents into PDF format using `pdflatex` or `xelatex`.
- **Limitation:** On Windows developer hosts that lack a native MiKTeX or TeXLive distribution, compiling raw PDF files locally requires installing an external distribution.
- **Handling & Mitigation:**
  - In web and CLI workflows, JobRadar detects the absence of a LaTeX compiler and falls back cleanly to formatted HTML and text previews without raising unhandled errors.
  - Automated tests gracefully skip the two compiler-specific checks (`test_resume.py` and `test_resume_ats.py`) while full PDF generation is tested and verified in Linux CI environments.

### 2. TypeSafe Jev Matcher Integration
- **Context:** Architecture was designed to integrate TypeSafe's Jev calibrated confidence evaluator alongside the Gemini/Groq matching providers.
- **Current State:** The provider interface (`jobradar.providers.base.MatchProvider`) is implemented and ready. Live evaluation is deferred until the project owner provides their TypeSafe API key.
- **Handling & Mitigation:** The existing deterministic J4 matcher and LLM fallback function seamlessly and autonomously without external provider keys.

### 3. Profile Confirmation Prerequisite
- **Context:** Automated recommendation matching and email alerts require a verified candidate profile.
- **Limitation:** Candidates whose profiles remain in `draft` status (e.g. immediately after upload, prior to clicking "Confirm Profile & Activate") will not receive automated Match Radar scores.
- **Rationale:** This is an intentional security and accuracy safeguard to prevent unverified or malformed resume extractions from generating false recommendations.

### 4. Notion Mirroring Status
- **Context:** Upstream JobRadar supported mirroring scraped job postings into a Notion workspace.
- **Current State:** Notion integration remains disabled (`notion.enabled: false` in config) in favor of the high-performance local FastAPI web dashboard, eliminating external SaaS latency and rate limit bottlenecks.

### 5. Backward-Compatible Legacy Schema Columns
- **Context:** The SQLite database schema contains several legacy columns from earlier single-user milestones (`job.score`, `job.status`, `job.user_status_at`, and the `status_change` table).
- **Current State:** These fields are retained to ensure backward compatibility with legacy CLI utilities. They are fully decoupled from candidate-facing workflows, which operate exclusively through `candidate_job_assessment` and `candidate_application`.
