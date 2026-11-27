# 05. Selected AI-Agent Engineering Interactions & Case Studies
**Evaluation Target:** Spektra Systems: AI-Native Software Engineer - Trainee
**Repository:** [JobRadar](file:///D:/JB/JobRadar)
**Branch:** `feat/j4-career-tracks`

---

## 1. Overview of Curated Interactions

This document presents 7 authentic case studies drawn from the Antigravity developer-agent engineering sessions during the JobRadar project. Each interaction highlights the practical realities of autonomous agentic software development: discovering edge-case defects, enforcing rigorous negative constraints, diagnosing test harness failures, preventing data leakage, and applying human developer oversight.

---

## 2. Interaction Case Studies

### Case Study 1: Track-Specific Best Match Ranking Defect
- **Problem:** When a user selected a specific career track filter (e.g. `?track=sdet`) on the jobs exploration page, `Repo.find_jobs()` still sorted jobs using `MAX(technical_score)` across all four career tracks. This allowed a posting with a high AI Backend score to rank above an SDET-specific posting, violating track isolation.
- **Developer Instruction:**
  > "Currently, Repo.find_jobs() uses MAX(technical_score) across all tracks even when a specific track filter is selected. With a track filter, rank exclusively using that selected track's technical_score. Never allow another track's score to influence ordering. Create regression tests covering overall Best Match and each track."
- **Selected Agent Interaction:**
  - *Agent Analysis:* Agent inspected the Common Table Expression (CTE) query in `src/jobradar/db/repo.py`:
    ```sql
    SELECT job_id, MAX(technical_score) as best_score
    FROM candidate_job_assessment
    GROUP BY job_id
    ```
  - *Agent Defect Identification:* The CTE grouped by `job_id` globally without filtering by `track`, meaning the `MAX` score returned came from whatever track scored highest.
- **Developer Oversight & Guidance:** The developer required strict deterministic tie-breaking and legacy compatibility when no candidate context exists.
- **Final Outcome:** Agent parameterized the CTE query: when `track` is specified, the join restricts `cja.track = :selected_track`, ensuring that sorting is 100% driven by the selected track.
- **Supporting Evidence:** [tests/test_track_specific_best_match.py](file:///D:/JB/JobRadar/tests/test_track_specific_best_match.py) verifies that an SDET posting ranks #1 under the SDET filter even if an AI Backend posting has a higher overall global score.
- **Engineering Lesson:** Complex SQL CTE rankings must have explicit regression tests for every filter branch to prevent cross-dimensional score pollution.

---

### Case Study 2: Production Database Protection and Invariance
- **Problem:** Running automated test suites, scrapers, or demo seeders risks corrupting or overwriting the primary production database ([data/jobradar.db](file:///D:/JB/JobRadar/data/jobradar.db)).
- **Developer Instruction:**
  > "Never run a changed build against the real data/jobradar.db. Never open the production database for writing. Verify the resolved database path before every seed operation. Refuse to run if the resolved path equals the production database path."
- **Selected Agent Interaction:**
  - *Initial Implementation Risk:* `DEFAULT_DB_PATH = Path("data/jobradar.db")` was hardcoded at the module level in `repo.py`, meaning any script importing `open_store()` opened the production database by default.
- **Developer Oversight & Guidance:** Developer instructed that the database path must be dynamically configurable via environment variables and guarded by runtime refusal checks.
- **Final Outcome:**
  - Implemented dynamic `get_default_db_path()` reading `JOBRADAR_DB_PATH`.
  - Added strict assertion in `prepare_demo_data.py`:
    ```python
    if resolved_target == resolved_prod:
        raise RuntimeError("CRITICAL SAFETY VIOLATION: Target database path equals production database!")
    ```
  - SHA-256 hash was verified before and after runs: `5de19c40567e23c5b95974ccdeed9ca7fd0f4b002f9fe2be862e5ec2b4021f91` (100% invariant).
- **Supporting Evidence:** Demonstrated in [DEMO_VALIDATION_REPORT.md](file:///D:/JB/JobRadar/DEMO_VALIDATION_REPORT.md) Section 5, where the script successfully aborted when passed `--db data/jobradar.db`.
- **Engineering Lesson:** Negative constraints ("do not mutate X") must be enforced by programmatic guards in code, not merely left to agent prompt adherence.

---

### Case Study 3: Candidate Isolation and IDOR Defense
- **Problem:** In a multi-candidate architecture, candidate profile reviews, job assessments, and applications must remain completely private. A malicious actor could tamper with URLs (e.g. `/onboarding/review/{other_candidate_profile_id}`) to view or edit another candidate's data.
- **Developer Instruction:**
  > "Enforce candidate isolation to prevent Insecure Direct Object References (IDOR). Use cryptographic session signing with constant-time verification."
- **Selected Agent Interaction:**
  - *Agent Architecture:* Designed `src/jobradar/web/session.py`.
  - Implemented `sign_candidate_session(candidate_id, secret)` using HMAC-SHA256 producing tokens formatted as `candidate_id.signature`.
  - Implemented `parse_candidate_session()` with `hmac.compare_digest()` to eliminate timing attacks.
  - Implemented `verify_profile_ownership(candidate_id, profile)` returning HTTP 403 Forbidden on ownership mismatches.
- **Developer Oversight & Guidance:** Developer verified that tampered cookies or unauthenticated requests cleanly redirect to onboarding without data leakage.
- **Supporting Evidence:** Automated in [tests/test_candidate_day1.py](file:///D:/JB/JobRadar/tests/test_candidate_day1.py) and [tests/test_phase3_final_e2e_validation.py](file:///D:/JB/JobRadar/tests/test_phase3_final_e2e_validation.py) (`test_step7_restart_persistence_and_candidate_isolation`).
- **Engineering Lesson:** Multi-tenancy in SQLite requires explicit application-level ownership checks on every route, reinforced by cryptographic cookie signing.

---

### Case Study 4: Preserving Semantic Truth in Opportunity Discovery
- **Problem:** During fresh ATS board ingestion, the candidate dashboard displayed zero eligible jobs despite having 119 active postings. Investigation revealed that employers frequently omit graduation years, causing the eligibility checker to fail them.
- **Developer Instruction:**
  > "Do not convert unknown requirements into confirmed eligibility. Keep existing classifications: ELIGIBLE, LIKELY_ELIGIBLE, NEEDS_VERIFICATION, NOT_ELIGIBLE. Do not fabricate graduation-year evidence or infer eligibility without factual basis."
- **Selected Agent Interaction:**
  - *Risk Considered:* Converting missing graduation criteria into `ELIGIBLE` would mislead candidates into applying for jobs that may reject them.
  - *Architectural Solution:* Retained `NEEDS_VERIFICATION` classification, but created an amber **Opportunities Pending Verification** banner on `/candidate/dashboard`. High-scoring jobs with unspecified graduation dates are surfaced transparently with full evidence notes.
- **Developer Oversight & Guidance:** Developer approved the amber banner pattern and verified that neither the LLM nor the rules engine hallucinated graduation years.
- **Supporting Evidence:** Validated in [tests/test_phase2_candidate_discovery.py](file:///D:/JB/JobRadar/tests/test_phase2_candidate_discovery.py) and visual proof [03_dashboard_light.png](file:///D:/JB/JobRadar/submission/screenshots/03_dashboard_light.png).
- **Engineering Lesson:** User experience problems should be resolved by transparency and presentation layers rather than corrupting semantic classification models.

---

### Case Study 5: SQLite In-Memory Concurrency in TestClient
- **Problem:** When developing the Phase 3 dedicated test suite (`test_phase3_final_e2e_validation.py`), the test runner crashed with `sqlite3.OperationalError: no such table: candidate`.
- **Selected Agent Interaction:**
  - *Initial Implementation:* The agent fixture initialized an in-memory SQLite database: `engine = create_engine("sqlite:///:memory:")`.
  - *Root Cause Analysis:* FastAPI's `TestClient` uses background threads for certain request handlers. In standard SQLite `:memory:` connections, each thread receives a distinct, empty database in memory, causing table lookups from the HTTP handler thread to fail.
- **Agent Self-Correction:**
  - Agent replaced `:memory:` with a persistent isolated temporary file database using pytest's `tmp_path`:
    ```python
    @pytest.fixture
    def isolated_repo(tmp_path: Path) -> Repo:
        engine = make_engine(tmp_path / "test_phase3.db")
        init_db(engine)
        return Repo(engine)
    ```
- **Final Outcome:** Database state persisted cleanly across all test client request threads.
- **Supporting Evidence:** [tests/test_phase3_final_e2e_validation.py](file:///D:/JB/JobRadar/tests/test_phase3_final_e2e_validation.py) executed all 8 test steps cleanly in 5.64 seconds.
- **Engineering Lesson:** Avoid in-memory SQLite (`:memory:`) when testing multithreaded ASGI/FastAPI applications; always use isolated temporary file databases.

---

### Case Study 6: TestClient Endpoint Mismatch & Model Attribute Resolution
- **Problem:** During the initial run of `test_phase3_final_e2e_validation.py`, 3 of the 8 tests failed with HTTP 403, 404, and Python `AttributeError`.
- **Diagnostic Process:**
  - *Failure 1:* POST `/onboarding/upload` returned 403. Diagnosis: FastAPI route checks `request.headers.get("origin")`. TestClient had not set the origin header.
  - *Failure 2:* POST `/onboarding/review/{id}/confirm` returned 404. Diagnosis: The actual route in `app.py` was defined as `@app.post("/onboarding/confirm/{profile_id}")`.
  - *Failure 3:* `test_step6` raised `AttributeError: 'CandidateApplicationHistory' object has no attribute 'to_status'`. Diagnosis: In `db/models.py`, the field is named `status`, not `to_status`.
- **Developer Oversight & Guidance:** Fix only the specific route parameters and model attribute calls without weakening application security checks.
- **Final Outcome:** Agent added `headers={"origin": "http://127.0.0.1:8080"}`, corrected the URL to `/onboarding/confirm/{draft.id}`, and accessed `h.status`.
- **Supporting Evidence:** All 8 tests passed in the subsequent run.
- **Engineering Lesson:** Always inspect the actual FastAPI route definitions and SQLModel class declarations rather than assuming endpoint conventions.

---

### Case Study 7: Independent Quality Gate Enforcement (No Verbal Claims)
- **Problem:** In complex multi-turn coding sessions, agents frequently report that tests pass without actually executing them in the current environment.
- **Developer Instruction:**
  > "Do not assume a reported feature works merely because a report says it does. Before PR: uv run ruff format src tests && uv run ruff check src tests && uv run mypy, and uv run pytest -q. Before creating artifacts, verify with real executions."
- **Selected Agent Interaction:**
  - Before writing reports, the agent executed the full suite of automated checks via PowerShell.
  - Discovered that Windows temp directory permissions caused intermittent pytest cache errors, and resolved them by configuring `--basetemp=data/sandbox/pytest_temp`.
  - Observed 994 passed tests and 6 Playwright tests with real terminal execution outputs before authoring validation documentation.
- **Supporting Evidence:** Verified logs documented in [07_testing_and_validation_evidence.md](file:///D:/JB/JobRadar/submission/07_testing_and_validation_evidence.md).
- **Engineering Lesson:** True software verification requires running actual terminal tool executions and quoting real output logs rather than generating plausible text descriptions.
