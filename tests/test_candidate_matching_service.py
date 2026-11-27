"""Tests for Day 2: Candidate-Personalized Eligibility & Technical Matching.

Verifies:
1. CandidateProfileAdapter:
   - Maps confirmed CandidateProfile into canonical J4 profile.
   - Enforces confirmation requirement (rejects unconfirmed drafts).
   - Preserves education, skills, experience, projects, and evidence provenance.
   - Validates against J4 profile_extensions.
2. CandidateMatchingService:
   - Single & batch job assessment.
   - Candidate-isolated persistence in `candidate_job_assessment`.
   - Traceability across candidate profile versions.
   - No cross-candidate overwrite.
3. Three-Candidate Synthetic Demonstration:
   - Candidate A: Python Backend (2027, B.Tech CSE)
   - Candidate B: Java Software Engineer (2026, B.Tech IT)
   - Candidate C: QA / SDET Engineer (2027, B.Tech CSE)
   - Evaluated across 5 identical jobs.
   - Demonstrates genuine score divergence, explainability, and mandatory eligibility enforcement.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import pytest

from jobradar.db.repo import Repo
from jobradar.pipeline.candidate_adapter import adapt_candidate_profile
from jobradar.pipeline.eligibility import EligibilityState
from jobradar.pipeline.schemas import JobPosting
from jobradar.profile import profile_extensions
from jobradar.services.matching_service import CandidateMatchingService

AS_OF = date(2026, 11, 24)


# ==============================================================================
# Helper fixtures for job creation
# ==============================================================================


def evidence(sentence: str, section: str = "Requirements") -> dict[str, str]:
    return {"sentence": sentence, "section": section, "source": "page"}


def skill_req(
    *skills: str, relation: Literal["all", "any"] = "all", sentence: str | None = None
) -> dict[str, Any]:
    wording = sentence or f"Required skills: {' or '.join(skills)}."
    return {"skills": list(skills), "relation": relation, "evidence": evidence(wording)}


def role_track(track: str, strength: str = "strong") -> dict[str, Any]:
    return {
        "track": track,
        "strength": strength,
        "evidence": [evidence(f"The responsibilities support the {track} track.", "Role")],
    }


def make_posting(
    job_id: str,
    title: str,
    company: str,
    tracks: list[tuple[str, str]],
    required_skills: list[dict[str, Any]],
    preferred_skills: list[dict[str, Any]] | None = None,
    batch_years: list[int] | None = None,
    min_exp: float = 0.0,
    max_exp: float = 1.0,
) -> tuple[str, JobPosting]:
    reqs: dict[str, Any] = {
        "required_skills": required_skills,
        "preferred_skills": preferred_skills or [],
        "experience": [
            {
                "need": "required",
                "experience_type": "professional",
                "minimum_years": min_exp,
                "maximum_years": max_exp,
                "evidence": evidence(f"{min_exp}-{max_exp} years of experience required."),
            }
        ],
        "experience_status": "known",
    }
    data = {
        "company": company,
        "role": title,
        "employment_type": "internship" if max_exp <= 1.0 else "full_time",
        "batch_years": [2026, 2027] if batch_years is None else batch_years,
        "degrees": ["B.Tech", "BE"],
        "locations": ["Bengaluru, India", "Remote"],
        "work_mode": "hybrid",
        "role_tracks": [role_track(*item) for item in tracks],
        "requirements": reqs,
        "skills_required": [s for item in required_skills for s in item.get("skills", [])],
        "skills_preferred": [
            s for item in (preferred_skills or []) for s in item.get("skills", [])
        ],
        "apply_url": f"https://example.com/jobs/{job_id}",
    }
    posting = JobPosting.model_validate(data)
    return job_id, posting


# ==============================================================================
# 1. CandidateProfileAdapter Tests
# ==============================================================================


def test_adapter_rejects_unconfirmed_draft_by_default(repo: Repo) -> None:
    cand = repo.create_candidate(name="Draft Candidate", email="draft@example.com")
    draft = repo.save_profile_draft(
        candidate_id=cand.id,
        resume_id=None,
        profile_data={"basics": {"name": "Draft Candidate"}, "skills": ["Python"]},
    )
    assert draft.review_status == "draft"

    with pytest.raises(ValueError, match="Only confirmed candidate profiles may be evaluated"):
        adapt_candidate_profile(draft, allow_draft=False)

    # Allowed when allow_draft=True
    canonical = adapt_candidate_profile(draft, allow_draft=True)
    assert canonical["basics"]["name"] == "Draft Candidate"


def test_adapter_converts_confirmed_profile_to_canonical_j4(repo: Repo) -> None:
    cand = repo.create_candidate(name="Alice Engineer", email="alice@example.com")
    draft = repo.save_profile_draft(
        candidate_id=cand.id,
        resume_id=None,
        profile_data={
            "basics": {
                "name": "Alice Engineer",
                "email": "alice@example.com",
                "location": "Bengaluru, India",
            },
            "education": [
                {
                    "degree": "B.Tech",
                    "branch": "Computer Science and Engineering",
                    "institution": "National Institute of Technology",
                    "graduation_year": 2027,
                    "cgpa": "8.8",
                }
            ],
            "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "Git"],
            "experience": [
                {
                    "id": "exp_1",
                    "title": "Backend Engineering Intern",
                    "company": "Tech Labs",
                    "bullets": ["Engineered FastAPI endpoints with PostgreSQL."],
                }
            ],
            "projects": [
                {
                    "id": "proj_1",
                    "name": "Job Agent Service",
                    "tools": ["Python", "FastAPI", "Docker"],
                    "bullets": ["Async worker with task persistence."],
                }
            ],
        },
    )
    confirmed = repo.confirm_profile(draft.id)
    assert confirmed.review_status == "confirmed"

    canonical = adapt_candidate_profile(confirmed)

    # 1. Verify top-level structure
    assert canonical["basics"]["name"] == "Alice Engineer"
    assert canonical["education"][0]["graduation_year"] == 2027
    assert "Python" in canonical["skills"]
    assert "FastAPI" in canonical["skills"]

    # 2. Verify evidence & career tracks validate against J4 profile extensions
    extensions = profile_extensions(canonical)
    assert extensions is not None
    evidence_sec, tracks = extensions
    assert set(tracks.keys()) == {"ai_backend", "full_stack", "sde", "sdet"}

    # 3. Verify skill facts have project/experience evidence
    skill_facts = [f for f in evidence_sec.facts.values() if f.category == "skill"]
    assert len(skill_facts) >= 5
    for sf in skill_facts:
        assert sf.status == "user_confirmed"
        assert len(sf.evidence_refs) > 0


# ==============================================================================
# 2. CandidateMatchingService Tests & Candidate Isolation
# ==============================================================================


def test_matching_service_evaluates_and_persists_isolated_assessments(repo: Repo) -> None:
    service = CandidateMatchingService(repo)

    cand_a = repo.create_candidate(name="Candidate A", email="a@example.com")
    draft_a = repo.save_profile_draft(
        cand_a.id,
        None,
        {
            "basics": {"name": "Candidate A"},
            "education": [{"degree": "B.Tech", "branch": "CSE", "graduation_year": 2027}],
            "skills": ["Python", "FastAPI"],
            "projects": [{"id": "p1", "name": "API Service", "tools": ["Python", "FastAPI"]}],
        },
    )
    prof_a = repo.confirm_profile(draft_a.id)

    cand_b = repo.create_candidate(name="Candidate B", email="b@example.com")
    draft_b = repo.save_profile_draft(
        cand_b.id,
        None,
        {
            "basics": {"name": "Candidate B"},
            "education": [{"degree": "B.Tech", "branch": "IT", "graduation_year": 2026}],
            "skills": ["Java", "Spring Boot"],
            "projects": [{"id": "p2", "name": "Spring Microservice", "tools": ["Java"]}],
        },
    )
    prof_b = repo.confirm_profile(draft_b.id)

    job = make_posting(
        job_id="job_shared_001",
        title="Python Developer Intern",
        company="PyCorp",
        tracks=[("ai_backend", "strong"), ("sde", "strong")],
        required_skills=[skill_req("Python", "FastAPI")],
        batch_years=[2026, 2027],
    )

    # 1. Assess Candidate A on job_shared_001
    results_a = service.assess_candidate_jobs(cand_a.id, [job], as_of=AS_OF)
    assert len(results_a) == 4  # 4 tracks

    # 2. Assess Candidate B on the EXACT same job_shared_001
    results_b = service.assess_candidate_jobs(cand_b.id, [job], as_of=AS_OF)
    assert len(results_b) == 4

    # 3. Verify Candidate A's results were NOT overwritten by Candidate B
    stored_a = repo.list_candidate_assessments(cand_a.id, job_id="job_shared_001")
    stored_b = repo.list_candidate_assessments(cand_b.id, job_id="job_shared_001")
    assert len(stored_a) == 4
    assert len(stored_b) == 4

    ai_match_a = repo.get_candidate_job_assessment(
        cand_a.id, "job_shared_001", prof_a.version, "ai_backend"
    )
    ai_match_b = repo.get_candidate_job_assessment(
        cand_b.id, "job_shared_001", prof_b.version, "ai_backend"
    )
    assert ai_match_a is not None
    assert ai_match_b is not None

    # Candidate A matched Python and FastAPI
    assert "Python" in ai_match_a.matched_skills
    assert ai_match_a.technical_score is not None and ai_match_a.technical_score > 60

    # Candidate B missed Python and FastAPI
    assert "Python" in ai_match_b.missing_required_skills
    assert "FastAPI" in ai_match_b.missing_required_skills


def test_matching_service_preserves_version_history(repo: Repo) -> None:
    service = CandidateMatchingService(repo)
    cand = repo.create_candidate(name="Versioning Candidate", email="ver@example.com")

    # Version 1: Python only
    draft_v1 = repo.save_profile_draft(
        cand.id,
        None,
        {
            "basics": {"name": "Version 1"},
            "education": [{"degree": "B.Tech", "branch": "CSE", "graduation_year": 2027}],
            "skills": ["Python"],
            "projects": [{"id": "p1", "name": "Basic Scripts", "tools": ["Python"]}],
        },
    )
    prof_v1 = repo.confirm_profile(draft_v1.id)
    assert prof_v1.version == 1

    job = make_posting(
        job_id="job_multi_skill",
        title="Python & FastAPI Engineer",
        company="FastCorp",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python"), skill_req("FastAPI")],
    )

    # Assess v1
    service.assess_candidate_job(cand.id, job, profile_version=1, as_of=AS_OF)
    res_v1 = repo.get_candidate_job_assessment(cand.id, "job_multi_skill", 1, "ai_backend")
    assert res_v1 is not None
    assert "FastAPI" in res_v1.missing_required_skills

    # Version 2: Added FastAPI
    prof_v2 = repo.confirm_profile(
        prof_v1.id,
        {
            "basics": {"name": "Version 2"},
            "education": [{"degree": "B.Tech", "branch": "CSE", "graduation_year": 2027}],
            "skills": ["Python", "FastAPI"],
            "projects": [{"id": "p1", "name": "FastAPI App", "tools": ["Python", "FastAPI"]}],
        },
    )
    assert prof_v2.version == 2

    # Assess v2
    service.assess_candidate_job(cand.id, job, profile_version=2, as_of=AS_OF)
    res_v2 = repo.get_candidate_job_assessment(cand.id, "job_multi_skill", 2, "ai_backend")
    assert res_v2 is not None
    assert "FastAPI" in res_v2.matched_skills

    # Traceability: Version 1 record still exists unchanged
    old_res = repo.get_candidate_job_assessment(cand.id, "job_multi_skill", 1, "ai_backend")
    assert old_res is not None
    assert "FastAPI" in old_res.missing_required_skills


# ==============================================================================
# 3. Three-Candidate Synthetic Demonstration (Mandatory Acceptance Criterion)
# ==============================================================================


def test_three_candidate_synthetic_demonstration(repo: Repo) -> None:
    service = CandidateMatchingService(repo)

    # --------------------------------------------------------------------------
    # Candidate A: Python Backend Engineer
    # 2027 grad, B.Tech CSE
    # Skills: Python, FastAPI, PostgreSQL, SQL, REST APIs, Git
    # --------------------------------------------------------------------------
    cand_a = repo.create_candidate(name="Candidate A (Python Backend)", email="cand_a@example.com")
    draft_a = repo.save_profile_draft(
        cand_a.id,
        None,
        {
            "basics": {
                "name": "Candidate A",
                "email": "cand_a@example.com",
                "location": "Bengaluru, India",
            },
            "education": [
                {
                    "degree": "B.Tech",
                    "branch": "Computer Science and Engineering",
                    "institution": "IIT Madras",
                    "graduation_year": 2027,
                    "cgpa": "9.2",
                }
            ],
            "skills": ["Python", "FastAPI", "PostgreSQL", "SQL", "REST APIs", "Git"],
            "projects": [
                {
                    "id": "p_py",
                    "name": "Distributed Job Engine",
                    "tools": ["Python", "FastAPI", "PostgreSQL", "SQL", "REST APIs", "Git"],
                    "bullets": ["Built async REST API handling 10k requests/min."],
                }
            ],
            "experience": [
                {
                    "id": "e_py",
                    "title": "Backend Engineering Intern",
                    "company": "Apex Labs",
                    "bullets": ["Engineered PostgreSQL models and FastAPI endpoints."],
                }
            ],
            "preferences": {"target_roles": ["AI Backend Engineer", "Python Developer"]},
        },
    )
    prof_a = repo.confirm_profile(draft_a.id)

    # --------------------------------------------------------------------------
    # Candidate B: Java Software Engineer
    # 2026 grad, B.Tech IT
    # Skills: Java, Spring Boot, SQL, MySQL, REST APIs, Git, Data Structures
    # --------------------------------------------------------------------------
    cand_b = repo.create_candidate(name="Candidate B (Java SDE)", email="cand_b@example.com")
    draft_b = repo.save_profile_draft(
        cand_b.id,
        None,
        {
            "basics": {
                "name": "Candidate B",
                "email": "cand_b@example.com",
                "location": "Hyderabad, India",
            },
            "education": [
                {
                    "degree": "B.Tech",
                    "branch": "Information Technology",
                    "institution": "NIT Warangal",
                    "graduation_year": 2026,
                    "cgpa": "8.7",
                }
            ],
            "skills": [
                "Java",
                "Spring Boot",
                "SQL",
                "MySQL",
                "REST APIs",
                "Git",
                "Data Structures",
            ],
            "projects": [
                {
                    "id": "p_java",
                    "name": "Enterprise Banking Service",
                    "tools": ["Java", "Spring Boot", "MySQL", "REST APIs", "Git"],
                    "bullets": ["Microservice architecture with Spring Boot & MySQL."],
                }
            ],
            "experience": [
                {
                    "id": "e_java",
                    "title": "Software Engineering Intern",
                    "company": "Global Payments",
                    "bullets": ["Implemented RESTful APIs in Java 17 and Spring Boot."],
                }
            ],
            "preferences": {"target_roles": ["Software Engineer", "Java Developer"]},
        },
    )
    prof_b = repo.confirm_profile(draft_b.id)

    # --------------------------------------------------------------------------
    # Candidate C: QA / SDET Engineer
    # 2027 grad, B.Tech CSE
    # Skills: Python, Selenium, Pytest, API Testing, Postman, SQL, Git
    # --------------------------------------------------------------------------
    cand_c = repo.create_candidate(name="Candidate C (QA SDET)", email="cand_c@example.com")
    draft_c = repo.save_profile_draft(
        cand_c.id,
        None,
        {
            "basics": {
                "name": "Candidate C",
                "email": "cand_c@example.com",
                "location": "Pune, India",
            },
            "education": [
                {
                    "degree": "B.Tech",
                    "branch": "Computer Science and Engineering",
                    "institution": "COEP Pune",
                    "graduation_year": 2027,
                    "cgpa": "8.5",
                }
            ],
            "skills": ["Python", "Selenium", "Pytest", "API testing", "Postman", "SQL", "Git"],
            "projects": [
                {
                    "id": "p_sdet",
                    "name": "Automation Test Harness",
                    "tools": ["Python", "Selenium", "Pytest", "API testing", "Postman", "Git"],
                    "bullets": ["E2E Selenium and Pytest automation suite."],
                }
            ],
            "experience": [
                {
                    "id": "e_sdet",
                    "title": "QA Automation Intern",
                    "company": "QualityFirst Systems",
                    "bullets": ["Automated 200+ regression tests using Pytest."],
                }
            ],
            "preferences": {"target_roles": ["SDET", "QA Automation Engineer"]},
        },
    )
    prof_c = repo.confirm_profile(draft_c.id)

    # --------------------------------------------------------------------------
    # Shared Job Inventory: Exactly 5 Representative Jobs
    # --------------------------------------------------------------------------

    # Job 1: Python Backend Engineer (Batch 2026/2027)
    job1 = make_posting(
        job_id="job_01_py_backend",
        title="Python Backend Engineer Intern",
        company="Stripe",
        tracks=[("ai_backend", "strong"), ("sde", "strong")],
        required_skills=[skill_req("Python", "FastAPI"), skill_req("PostgreSQL", "SQL")],
        preferred_skills=[skill_req("Docker")],
        batch_years=[2026, 2027],
    )

    # Job 2: Java Software Engineer (Batch 2026/2027)
    job2 = make_posting(
        job_id="job_02_java_sde",
        title="Java Software Engineer Intern",
        company="Oracle",
        tracks=[("sde", "strong"), ("full_stack", "possible")],
        required_skills=[skill_req("Java"), skill_req("Spring Boot"), skill_req("MySQL", "SQL")],
        batch_years=[2026, 2027],
    )

    # Job 3: QA Automation / SDET Engineer (Batch 2026/2027)
    job3 = make_posting(
        job_id="job_03_qa_sdet",
        title="SDET / QA Automation Engineer Intern",
        company="BrowserStack",
        tracks=[("sdet", "strong"), ("sde", "possible")],
        required_skills=[skill_req("Pytest", "Selenium"), skill_req("API testing", "Postman")],
        batch_years=[2026, 2027],
    )

    # Job 4: Mandatory Strict Batch 2026 Restriction (Blocks 2027 graduates)
    job4 = make_posting(
        job_id="job_04_batch_2026_strict",
        title="Immediate Graduate Backend Engineer (2026 Batch Only)",
        company="Goldman Sachs",
        tracks=[("sde", "strong"), ("ai_backend", "strong")],
        required_skills=[skill_req("Python", "Java", relation="any"), skill_req("SQL")],
        batch_years=[2026],  # Explicitly excludes 2027!
    )

    # Job 5: Full Stack Engineer with Required & Preferred Skills
    job5 = make_posting(
        job_id="job_05_fullstack_req_pref",
        title="Full Stack Software Engineer",
        company="Uber",
        tracks=[("full_stack", "strong"), ("sde", "strong")],
        required_skills=[skill_req("REST APIs", "SQL")],
        preferred_skills=[skill_req("React", "TypeScript"), skill_req("Docker", "Redis")],
        batch_years=[2026, 2027],
    )

    jobs = [job1, job2, job3, job4, job5]

    # --------------------------------------------------------------------------
    # Execute Evaluations Against All 3 Candidates
    # --------------------------------------------------------------------------
    assessments_a = service.assess_candidate_jobs(cand_a.id, jobs, as_of=AS_OF)
    assessments_b = service.assess_candidate_jobs(cand_b.id, jobs, as_of=AS_OF)
    assessments_c = service.assess_candidate_jobs(cand_c.id, jobs, as_of=AS_OF)

    assert len(assessments_a) == len(jobs) * 4
    assert len(assessments_b) == len(jobs) * 4
    assert len(assessments_c) == len(jobs) * 4

    # --------------------------------------------------------------------------
    # Mandatory Assertions & Explanations
    # --------------------------------------------------------------------------

    # 1. Job 1 (Python Backend):
    # Candidate A should have strong ai_backend technical fit
    # Candidate B should miss Python & FastAPI
    j1_a = repo.get_candidate_job_assessment(cand_a.id, "job_01_py_backend", 1, "ai_backend")
    j1_b = repo.get_candidate_job_assessment(cand_b.id, "job_01_py_backend", 1, "ai_backend")
    j1_c = repo.get_candidate_job_assessment(cand_c.id, "job_01_py_backend", 1, "ai_backend")

    assert j1_a is not None and j1_b is not None and j1_c is not None
    assert "Python" in j1_a.matched_skills
    assert "FastAPI" in j1_a.matched_skills
    assert j1_a.technical_score is not None and j1_a.technical_score >= 70

    assert "FastAPI" in j1_b.missing_required_skills
    assert "Python" in j1_b.missing_required_skills
    assert j1_b.technical_score is None or j1_b.technical_score < j1_a.technical_score

    # 2. Job 2 (Java SDE):
    # Candidate B should have strong sde technical fit (Java + Spring Boot)
    # Candidate A should miss Java & Spring Boot
    j2_a = repo.get_candidate_job_assessment(cand_a.id, "job_02_java_sde", 1, "sde")
    j2_b = repo.get_candidate_job_assessment(cand_b.id, "job_02_java_sde", 1, "sde")
    j2_c = repo.get_candidate_job_assessment(cand_c.id, "job_02_java_sde", 1, "sde")

    assert j2_a is not None and j2_b is not None and j2_c is not None
    assert "Java" in j2_b.matched_skills
    assert "Spring Boot" in j2_b.matched_skills
    assert j2_b.technical_score is not None and j2_b.technical_score >= 70

    assert "Java" in j2_a.missing_required_skills
    assert "Spring Boot" in j2_a.missing_required_skills

    # 3. Job 3 (QA SDET):
    # Candidate C should have strong sdet technical fit (Pytest + Selenium + API Testing)
    # Candidate B should miss Pytest & Selenium
    j3_a = repo.get_candidate_job_assessment(cand_a.id, "job_03_qa_sdet", 1, "sdet")
    j3_b = repo.get_candidate_job_assessment(cand_b.id, "job_03_qa_sdet", 1, "sdet")
    j3_c = repo.get_candidate_job_assessment(cand_c.id, "job_03_qa_sdet", 1, "sdet")

    assert j3_a is not None and j3_b is not None and j3_c is not None
    assert "Pytest" in j3_c.matched_skills
    assert "Selenium" in j3_c.matched_skills
    assert j3_c.technical_score is not None and j3_c.technical_score >= 70

    assert "Selenium" in j3_b.missing_required_skills

    # 4. Job 4 (Mandatory Batch 2026 Strict Exclusion):
    # Candidate A (2027): NOT_ELIGIBLE despite having Python skills!
    # Candidate B (2026): ELIGIBLE (graduates in 2026)
    # Candidate C (2027): NOT_ELIGIBLE!
    # Hard constraint cannot be overridden by technical score!
    j4_a = repo.get_candidate_job_assessment(cand_a.id, "job_04_batch_2026_strict", 1, "sde")
    j4_b = repo.get_candidate_job_assessment(cand_b.id, "job_04_batch_2026_strict", 1, "sde")
    j4_c = repo.get_candidate_job_assessment(cand_c.id, "job_04_batch_2026_strict", 1, "sde")

    assert j4_a is not None and j4_b is not None and j4_c is not None
    assert j4_a.eligibility_status == EligibilityState.NOT_ELIGIBLE.value
    assert any("2026" in r for r in j4_a.eligibility_reasons)
    # Candidate A has technical skills, but eligibility is strictly NOT_ELIGIBLE
    assert "Python" in j4_a.matched_skills

    # Candidate B is batch 2026 -> ELIGIBLE
    assert j4_b.eligibility_status == EligibilityState.ELIGIBLE.value

    # Candidate C is batch 2027 -> NOT_ELIGIBLE
    assert j4_c.eligibility_status == EligibilityState.NOT_ELIGIBLE.value

    # 5. Job 5 (Full Stack Required vs Preferred):
    j5_a = repo.get_candidate_job_assessment(
        cand_a.id, "job_05_fullstack_req_pref", 1, "full_stack"
    )
    j5_b = repo.get_candidate_job_assessment(
        cand_b.id, "job_05_fullstack_req_pref", 1, "full_stack"
    )
    assert j5_a is not None and j5_b is not None
    assert "REST APIs" in j5_a.matched_skills or "SQL" in j5_a.matched_skills

    # Print summary demonstration matrix for execution verification
    print("\n" + "=" * 80)
    print("DAY 2 THREE-CANDIDATE DEMONSTRATION MATRIX (5 JOBS x 3 CANDIDATES)")
    print("=" * 80)
    for j_id, j_post in jobs:
        print(f"\n--- Job: {j_id} ({j_post.role} @ {j_post.company}) ---")
        for cid, label, _cand in [
            (cand_a.id, "Candidate A (Python)", prof_a),
            (cand_b.id, "Candidate B (Java)", prof_b),
            (cand_c.id, "Candidate C (SDET)", prof_c),
        ]:
            assessments = repo.list_candidate_assessments(cid, job_id=j_id, profile_version=1)
            primary_track = j_post.role_tracks[0].track if j_post.role_tracks else "sde"
            matched_row = next((a for a in assessments if a.track == primary_track), assessments[0])
            print(
                f"  [{label}] Track: {matched_row.track:<11} | "
                f"Eligibility: {matched_row.eligibility_status:<18} | "
                f"Score: {matched_row.technical_score!s:<4} | "
                f"Matched: {len(matched_row.matched_skills)} | "
                f"Missing Req: {len(matched_row.missing_required_skills)}"
            )
    print("=" * 80 + "\n")


def test_multi_candidate_independence_with_senior_and_entry_shared_jobs(repo: Repo) -> None:
    """Verifies Task 2.1 multi-candidate isolation on entry-level vs senior shared jobs."""
    # 1. Candidate 1: 2027 Fresher
    cand1 = repo.create_candidate(name="Fresher 2027", email="fresher2027@example.com")
    draft1 = {
        "basics": {"name": "Fresher 2027", "email": "fresher2027@example.com"},
        "education": [{"degree": "B.Tech", "branch": "Computer Science", "graduation_year": 2027}],
        "skills": ["Python", "FastAPI", "SQL", "Git"],
        "experience": [],
        "projects": [{"name": "AI App", "description": "Built FastAPI app"}],
        "preferences": {
            "target_roles": ["Software Engineer"],
            "locations": ["Bengaluru", "Remote"],
        },
    }
    p1 = repo.save_profile_draft(cand1.id, None, draft1)
    prof1 = repo.confirm_profile(cand1.id, p1.id)

    # 2. Candidate 2: Senior Engineer (Graduated 2019, 6 years experience)
    cand2 = repo.create_candidate(name="Senior Dev", email="seniordev@example.com")
    draft2 = {
        "basics": {"name": "Senior Dev", "email": "seniordev@example.com"},
        "education": [{"degree": "B.Tech", "branch": "Computer Science", "graduation_year": 2019}],
        "skills": ["Python", "System Design", "Kubernetes", "AWS", "SQL", "PostgreSQL"],
        "experience": [
            {
                "title": "Lead Software Engineer",
                "company": "Tech Corp",
                "dates": "2019 - Present",
                "bullets": ["Designed scalable distributed systems with 6 years experience."],
            }
        ],
        "projects": [],
        "preferences": {
            "target_roles": ["Lead Software Engineer"],
            "locations": ["Bengaluru", "Remote"],
        },
    }
    p2 = repo.save_profile_draft(cand2.id, None, draft2)
    prof2 = repo.confirm_profile(cand2.id, p2.id)

    # 3. Two shared jobs in inventory:
    # Job Entry: Entry-level / Fresher SWE (0-1 yrs exp, batch 2026-2027)
    j_entry_id, j_entry_post = make_posting(
        "job_entry_swe",
        "Software Engineer (Fresher)",
        "Startup Co",
        [("sde", "strong"), ("ai_backend", "strong")],
        [skill_req("Python", "SQL")],
        batch_years=[2026, 2027],
        min_exp=0.0,
        max_exp=1.0,
    )
    # Job Senior: Senior / Lead SWE (5-10 yrs exp, batch 2018-2020)
    j_senior_id, j_senior_post = make_posting(
        "job_senior_lead",
        "Lead Software Development Engineer",
        "Enterprise Corp",
        [("sde", "strong"), ("ai_backend", "strong")],
        [skill_req("Python", "Kubernetes")],
        batch_years=[2018, 2019, 2020],
        min_exp=5.0,
        max_exp=10.0,
    )

    job_list = [(j_entry_id, j_entry_post), (j_senior_id, j_senior_post)]
    matcher = CandidateMatchingService(repo)
    matcher.assess_candidate_jobs(cand1.id, job_list, profile_version=prof1.version, as_of=AS_OF)
    matcher.assess_candidate_jobs(cand2.id, job_list, profile_version=prof2.version, as_of=AS_OF)

    # Candidate 1 results on Entry Job: ELIGIBLE or NEEDS_VERIFICATION (not rejected)
    c1_entry = repo.get_candidate_job_assessment(cand1.id, j_entry_id, prof1.version, "sde")
    assert c1_entry is not None
    assert c1_entry.eligibility_status in (
        EligibilityState.ELIGIBLE.value,
        EligibilityState.NEEDS_VERIFICATION.value,
    )
    assert c1_entry.technical_score is not None and c1_entry.technical_score >= 70

    # Candidate 1 results on Senior Job: NOT_ELIGIBLE (mandatory 5-year experience deficit)
    c1_senior = repo.get_candidate_job_assessment(cand1.id, j_senior_id, prof1.version, "sde")
    assert c1_senior is not None
    assert c1_senior.eligibility_status == EligibilityState.NOT_ELIGIBLE.value
    assert any(
        "experience" in r.lower() or "batch" in r.lower() for r in c1_senior.eligibility_reasons
    )

    # Candidate 2 results on Senior Job: ELIGIBLE or NEEDS_VERIFICATION (not rejected for experience)
    c2_senior = repo.get_candidate_job_assessment(cand2.id, j_senior_id, prof2.version, "sde")
    assert c2_senior is not None
    assert c2_senior.eligibility_status in (
        EligibilityState.ELIGIBLE.value,
        EligibilityState.NEEDS_VERIFICATION.value,
    )

    # Candidate 2 results on Entry Job: NOT_ELIGIBLE (graduated 2019, but job strictly requires batch 2026-2027)
    c2_entry = repo.get_candidate_job_assessment(cand2.id, j_entry_id, prof2.version, "sde")
    assert c2_entry is not None
    assert c2_entry.eligibility_status == EligibilityState.NOT_ELIGIBLE.value
