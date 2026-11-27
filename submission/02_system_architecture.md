# 02. System Architecture & Multi-Candidate Design
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks`

---

## 1. High-Level Architecture Overview

JobRadar employs a decoupled, multi-tiered architecture that separates global job inventory ingestion from private candidate career evaluations and application tracking:

```
+-----------------------------------------------------------------------------------+
|                            SHARED JOB INVENTORY (GLOBAL)                          |
|                                                                                   |
|  Sources: Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Telegram, Email    |
|       |                                                                           |
|       v                                                                           |
|  Ingest -> Expand / Clean Links -> Deduplicate -> Robots.txt Compliant Fetch      |
|       |                                                                           |
|       v                                                                           |
|  Structured Extraction (Deterministic rule engine first, fallback LLM)            |
|       |                                                                           |
|       v                                                                           |
|  Inventory Lifecycle: discovered -> extracted -> new (or hidden if closed/scam)   |
|  * IMMUTABLE TO CANDIDATE ACTIONS *                                               |
+-----------------------------------------------------------------------------------+
                                        |
                   +--------------------+--------------------+
                   |                                         |
                   v                                         v
+--------------------------------------+   +----------------------------------------+
|    CANDIDATE A SCOPE                 |   |    CANDIDATE B SCOPE                   |
|                                      |   |                                        |
| Profile: J4 Confirmed Profile        |   | Profile: J4 Confirmed Profile          |
| Tracks: ai_backend, full_stack, ...  |   | Tracks: sde, sdet, ...                 |
|                                      |   |                                        |
| Matching Service:                    |   | Matching Service:                      |
| -> Technical match (0-100)           |   | -> Technical match (0-100)             |
| -> Role alignment (J4 tracks)        |   | -> Role alignment (J4 tracks)          |
| -> Requirements verification         |   | -> Requirements verification           |
| -> Multi-track fit & explanations    |   | -> Multi-track fit & explanations      |
|                                      |   |                                        |
| Storage:                             |   | Storage:                               |
| -> candidate_job_assessment          |   | -> candidate_job_assessment            |
| -> candidate_application             |   | -> candidate_application               |
| -> candidate_application_history     |   | -> candidate_application_history       |
|                                      |   |                                        |
| Sinks & Notifications:               |   | Sinks & Notifications:                 |
| -> Alerts, Digests, Follow-ups       |   | -> Alerts, Digests, Follow-ups         |
| -> 4-part HMAC action tokens         |   | -> 4-part HMAC action tokens           |
+--------------------------------------+   +----------------------------------------+
```

---

## 2. Shared Inventory vs Candidate-Scoped Storage

In Stage 3C, the core schema was redesigned to enforce strict immutability of shared job inventory:

### A. Shared Global Tables
1. **`job`:** Represents crawled job postings from applicant tracking systems and web pages. Columns include `id`, `canonical_url`, `role`, `company`, `data_json` (raw JSON posting), and `status` (`discovered`, `extracted`, `new`, `hidden`). Candidate actions (shortlisting, applying) **never** modify `job.status`.
2. **`feed_board` & `watched_page`:** ATS endpoints and web URLs polled on schedule.
3. **`task_record`:** Durable SQLite-backed task queue ensuring zero task loss across restarts.

### B. Candidate-Scoped Tables
1. **`candidate`:** Core candidate identity entity (`id`, `name`, `email`, `created_at`).
2. **`candidate_profile`:** Versioned candidate profile records (`version`, `review_status`, `profile_data`). Confirmation creates immutable Version 1.
3. **`candidate_job_assessment`:** Isolated evaluations computed across the 4 J4 tracks (`track`, `technical_score`, `eligibility_status`, `eligibility_reason`, `explanation`).
4. **`candidate_application`:** Current application status (`saved`, `applied`, `interviewing`, `offered`, `rejected`, `skipped`) and candidate notes.
5. **`candidate_application_history`:** Audit log recording every status transition with timestamp and notes.

---

## 3. The Four-Track J4 Career Engine

Rather than computing a single generic score, JobRadar evaluates every posting against 4 canonical technical career tracks:

```
                  +-----------------------------------+
                  |   Job Posting & Extracted Text    |
                  +-----------------------------------+
                                    |
          +-------------------------+-------------------------+
          |                         |                         |
          v                         v                         v
+-------------------+     +-------------------+     +-------------------+
|    ai_backend     |     |    full_stack     |     |        sde        |
|  LLMs, PyTorch,   |     | TypeScript, React |     | Systems, C++, OS, |
|  FastAPI, Python  |     | Next.js, FastAPI  |     |  High Throughput  |
+-------------------+     +-------------------+     +-------------------+
                                    |
                                    v
                          +-------------------+
                          |       sdet        |
                          | Pytest, Selenium, |
                          | Playwright, CI/CD |
                          +-------------------+
```

### Algorithmic Evaluation Pipeline
1. **Track Role Alignment:** Examines job titles and responsibilities to establish primary and secondary track relevance (e.g. `strong`, `possible`, `unrelated`).
2. **Deterministic Skill Overlap:** Compares candidate skills against required and preferred skills with relation logic (`all` vs `any`).
3. **Hard Constraint Eligibility Verification:**
   - Evaluates graduation batch year against candidate profile.
   - Evaluates experience bounds (e.g. rejecting 5+ year roles for fresher candidates).
   - Evaluates degree disciplines (e.g. Computer Science, Electrical Engineering).
4. **Semantic Separation:** A candidate can have a 95% technical score on skills but receive an `ELIGIBLE = False` (`NOT_ELIGIBLE`) verdict due to graduation year constraints.

---

## 4. Security Architecture & Boundary Defense

JobRadar implements enterprise security patterns to guarantee candidate data privacy and prevent cross-tenant access:

### A. Cryptographic Session Management
- **Cookie Name:** `jobradar_candidate_session`
- **Signing Algorithm:** HMAC-SHA256 producing tokens formatted as `<candidate_id>.<signature_hex[:32]>`.
- **Validation:** Every HTTP request verifies signature integrity using constant-time comparison (`hmac.compare_digest`).

### B. Insecure Direct Object Reference (IDOR) Defense
- All candidate endpoints (e.g. `/onboarding/review/{profile_id}`, `/candidate/dashboard`, `/candidate/jobs/{job_id}/status`) verify profile ownership using `verify_profile_ownership(candidate_id, profile)`.
- If Candidate B attempts to inspect Candidate A's profile or change their application status, the endpoint immediately terminates with **HTTP 403 Forbidden**.

### C. CSRF Protection
- Stateful mutations (profile confirmation, status updates) require a session-bound CSRF token: `candidate_csrf_token(candidate_id, secret)`.
- Initial unauthenticated uploads enforce origin verification (`urlsplit(origin).hostname in hosts`) and onboarding page tokens.

### D. Network and Host Hardening
- Dashboard binds exclusively to `127.0.0.1` (local and Tailscale serve access only; foreign Host headers refused).
- Strict Content Security Policy (CSP): `script-src 'self'`, no inline scripts, and SVG-attribute-based charting without inline style injection.
