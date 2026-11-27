# 01. Project Overview & Provenance
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks`
**License:** MIT License (Copyright (c) 2026 ThePlator)

---

## 1. Problem Statement

Early-career software engineering candidates in India face severe structural challenges when navigating the technical job market:
1. **Scattered and Ephemeral Postings:** Opportunities appear unpredictably across Telegram channels, LinkedIn posts, company career pages (Greenhouse, Lever, Ashby, Workday), and hiring drive announcements.
2. **Missing and Sparse ATS Metadata:** Employer job postings frequently omit explicit graduation year requirements or describe experience ambiguously, causing naive filtering algorithms to falsely hide high-relevance positions.
3. **Mismatched Candidate Evaluations:** Generic job platforms compute monolithic match scores that conflate fundamental skill relevance with rigid eligibility gates, leading to candidate frustration and high rejection rates.
4. **Data Privacy and Centralized Tracking Risks:** Candidates are forced to share unencrypted personal data with commercial job aggregation platforms without knowing how their profiles are evaluated.

JobRadar was developed to solve these challenges through an open-source, local-first, privacy-preserving AI job intelligence agent.

---

## 2. Upstream Origin & Open-Source Provenance

JobRadar was originally created by **Sameer** (GitHub: `ThePlator`), an Electrical Engineering fresher in India applying to Backend and SWE roles.

- **Upstream Repository:** [https://github.com/ThePlator/JobRadar.git](https://github.com/ThePlator/JobRadar.git)
- **Base Commit:** `fdd739b` ("Merge pull request #113 from ThePlator/dependabot/uv/python-packages-81f22dcca5")
- **License Terms:** Released under the permissive MIT License. Full copyright notice is preserved in [LICENSE](file:///D:/JB/JobRadar/LICENSE).

The original project provided a single-user CLI agent that scraped Telegram channels and company boards, parsed job text using simple heuristics, computed single-profile match scores against a static `config/profile.yaml`, and mirrored job entries to Notion.

---

## 3. Contribution Scope & Architectural Extensions

For the Spektra Systems submission, JobRadar was substantially re-architected into an enterprise-ready, multi-candidate AI engineering platform:

| Functional Area | Upstream Base (ThePlator) | Spektra Systems Contribution |
|---|---|---|
| **Multi-Tenancy** | Single hardcoded user profile (`config/profile.yaml`). | Multi-candidate architecture with HMAC-SHA256 signed session cookies, candidate context resolution, and IDOR protection. |
| **Inventory Mutability** | Job records mutated directly by user actions (`job.status = 'applied'`). | Immutable shared global inventory (`Job`) with candidate-scoped private applications (`CandidateApplication`, `CandidateApplicationHistory`). |
| **Assessment Engine** | 0-100 single keyword score. | Four-Track J4 Career Engine (`ai_backend`, `full_stack`, `sde`, `sdet`) with structured evidence extraction and track-specific Best Match ranking. |
| **Eligibility Reasoning** | Basic filter rules. | Strict semantic separation between Technical Match Score (0-100) and Eligibility Verdict (Eligible, Likely Eligible, Needs Verification, Not Eligible). |
| **Opportunity Discovery** | Postings with missing batch criteria were hidden or discarded. | Real-time Opportunity Discovery surfacing unverified postings in an amber discovery view (`NEEDS_VERIFICATION`). |
| **Onboarding** | Manual text editing of YAML files. | Web-based resume upload (PDF/DOCX), deterministic entity extraction, interactive profile review, and immutable version confirmation. |
| **User Interface** | Basic single-user Jinja templates. | Modern Vercel-inspired responsive UI with Geist typography, KPI metrics, native light/dark theme toggle, and mobile layouts. |
| **Verification & Testing** | ~680 legacy pytest tests. | 994 passed pytest unit/integration tests and 6 Playwright browser E2E tests on isolated databases. |

---

## 4. Architectural Attribution Statement

This project respects and preserves all open-source licensing requirements. In accordance with the MIT License, the original copyright notice remains in all source distributions. All novel multi-candidate tables, career track engines, session security modules, and UI redesign templates represent our explicit contributions developed on branch `feat/j4-career-tracks`.
