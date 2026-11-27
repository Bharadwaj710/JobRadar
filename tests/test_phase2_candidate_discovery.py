"""Phase 2: Candidate Opportunity Discovery & Eligibility Presentation Regression Tests.

Validates that:
1. Relevant internships/jobs with unknown graduation years are discoverable as NEEDS_VERIFICATION in default feed.
2. Jobs explicitly excluding candidate graduation batch remain NOT_ELIGIBLE, excluded from default feed, but visible in transparency log.
3. Jobs explicitly accepting candidate graduation batch qualify as ELIGIBLE.
4. Unstated or unknown requirements remain transparent in verification reasons.
5. Missing technical scores remain None (Unscored) and never convert to zero.
6. Candidate isolation is strictly preserved across recommendations.
7. Closed and hidden jobs are excluded from active recommendations.
8. Track-specific Best Match sorting correctly respects career track filters.
9. Candidate dashboard web route and template correctly separate sections and render banners.
10. Repo.find_jobs() aligns with candidate eligibility filtering.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Literal

from fastapi.testclient import TestClient

from jobradar.clock import to_iso, utcnow
from jobradar.db.models import (
    Candidate,
    CandidateProfile,
    Job,
    JobStatus,
)
from jobradar.db.repo import Repo
from jobradar.pipeline.schemas import JobPosting
from jobradar.services.matching_service import CandidateMatchingService
from jobradar.web.app import create_app
from jobradar.web.session import (
    CANDIDATE_SESSION_COOKIE,
    sign_candidate_session,
)
from tests.conftest import FakeClock
from tests.test_digest import settings

AS_OF = date(2026, 11, 24)
BASE_URL = "http://127.0.0.1:8080"


# ==============================================================================
# Helper fixtures & builders
# ==============================================================================


def evidence(sentence: str, section: str = "Requirements") -> dict[str, str]:
    return {"sentence": sentence, "section": section, "source": "page"}


def skill_req(
    *skills: str, relation: Literal["all", "any"] = "all", sentence: str | None = None
) -> dict[str, Any]:
    wording = sentence or f"Required skills: {' or '.join(skills)}."
    return {"skills": list(skills), "relation": relation, "evidence": evidence(wording)}


def make_test_posting(
    job_id: str,
    title: str,
    company: str,
    tracks: list[tuple[str, str]],
    required_skills: list[dict[str, Any]],
    batch_years: list[int] | None = None,
    min_exp: float = 0.0,
    max_exp: float = 1.0,
    locations: list[str] | None = None,
    experience_status: str = "known",
) -> tuple[str, JobPosting]:
    reqs: dict[str, Any] = {
        "required_skills": required_skills,
        "preferred_skills": [],
        "experience": [
            {
                "need": "required",
                "experience_type": "professional",
                "minimum_years": min_exp,
                "maximum_years": max_exp,
                "evidence": evidence(f"{min_exp}-{max_exp} years of experience required."),
            }
        ]
        if experience_status == "known"
        else [],
        "experience_status": experience_status,
        "degrees": ["B.Tech", "BE"],
    }
    track_roles = [
        {
            "track": trk,
            "strength": st,
            "evidence": [evidence(f"Responsibilities align with {trk}.", "Role")],
        }
        for trk, st in tracks
    ]
    data = {
        "company": company,
        "role": title,
        "employment_type": "internship" if max_exp <= 1.0 else "full_time",
        "batch_years": [] if batch_years is None else batch_years,
        "degrees": ["B.Tech", "BE"],
        "locations": locations or ["Bengaluru, India", "Remote"],
        "work_mode": "hybrid",
        "role_tracks": track_roles,
        "requirements": reqs,
        "skills_required": [s for item in required_skills for s in item.get("skills", [])],
        "skills_preferred": [],
        "apply_url": f"https://example.com/jobs/{job_id}",
    }
    posting = JobPosting.model_validate(data)
    return job_id, posting


def create_test_candidate(
    repo: Repo,
    name: str,
    email: str,
    branch: str = "Computer Science",
    grad_year: int = 2027,
    skills: list[str] | None = None,
) -> tuple[Candidate, CandidateProfile]:
    cand = repo.create_candidate(name=name, email=email)
    skill_list = skills or ["Python", "FastAPI", "SQL"]
    draft_data = {
        "basics": {
            "name": name,
            "email": email,
            "phone": "+91 98765 43210",
            "location": "Bengaluru, India",
        },
        "education": [
            {
                "degree": "B.Tech",
                "branch": branch,
                "institution": "National Institute of Technology",
                "graduation_year": grad_year,
                "cgpa": "8.8",
            }
        ],
        "skills": skill_list,
        "experience": [
            {
                "title": "Software Engineering Intern",
                "company": "Tech Innovations",
                "bullets": ["Developed backend microservices using Python and FastAPI."],
            }
        ],
        "projects": [
            {
                "name": f"{name} Distributed API",
                "technologies": skill_list[:3],
                "description": "Scalable REST APIs built with Python and FastAPI.",
            }
        ],
        "target_roles": ["Backend Engineer", "Software Engineer"],
        "preferences": {
            "locations": ["Bengaluru, India", "Remote"],
            "roles": ["Backend Engineer"],
            "remote": True,
        },
    }
    draft = repo.save_profile_draft(candidate_id=cand.id, resume_id=None, profile_data=draft_data)
    confirmed = repo.confirm_profile(draft.id)
    return cand, confirmed


def seed_job_in_db(
    repo: Repo, job_id: str, posting: JobPosting, status: str = JobStatus.NEW.value
) -> Job:
    now = to_iso(utcnow())
    job = Job(
        id=job_id,
        canonical_url=posting.apply_url or f"https://example.com/jobs/{job_id}",
        role=posting.role,
        company=posting.company,
        status=status,
        score=75,
        score_reason="Evaluated test posting",
        data_json=json.dumps(posting.model_dump()),
        created_at=now,
        updated_at=now,
    )
    with repo.engine.begin() as conn:
        conn.exec_driver_sql(
            """
            INSERT OR REPLACE INTO job (id, canonical_url, role, company, status, score, score_reason, data_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.canonical_url,
                job.role,
                job.company,
                job.status,
                job.score,
                job.score_reason,
                job.data_json,
                job.created_at,
                job.updated_at,
            ),
        )
    return job


def insert_test_assessment(
    repo: Repo,
    candidate_id: str,
    job_id: str,
    profile_id: str,
    profile_version: int,
    track: str,
    technical_score: int | None,
    eligibility_status: str,
    evidence_coverage: float = 0.8,
    eligibility_reasons: list[str] | None = None,
    matched_skills: list[str] | None = None,
) -> None:
    now = to_iso(utcnow())
    with repo.engine.begin() as conn:
        conn.exec_driver_sql(
            """
            INSERT INTO candidate_job_assessment (
                id, candidate_id, job_id, profile_id, profile_version, track,
                technical_score, evidence_coverage, eligibility_status,
                eligibility_reasons_json, matched_skills_json,
                missing_required_skills_json, missing_preferred_skills_json,
                explanation, assessment_json, eligibility_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                candidate_id,
                job_id,
                profile_id,
                profile_version,
                track,
                technical_score,
                evidence_coverage,
                eligibility_status,
                json.dumps(eligibility_reasons or []),
                json.dumps(matched_skills or []),
                json.dumps([]),
                json.dumps([]),
                "Test explanation",
                "{}",
                "{}",
                now,
                now,
            ),
        )


# ==============================================================================
# Tests for Candidate Opportunity Discovery & Presentation
# ==============================================================================


def test_scenario_1_unknown_graduation_year_discoverable_as_needs_verification_in_default_feed(
    repo: Repo,
) -> None:
    """A posting with unknown graduation year is discoverable as NEEDS_VERIFICATION in default feed."""
    cand, prof = create_test_candidate(repo, "Candidate Alpha", "alpha@test.local", grad_year=2027)

    # Job with empty batch_years (employer posting did not state batch)
    job_id, posting = make_test_posting(
        job_id="job_unstated_batch",
        title="Python Backend Intern",
        company="TechCorp",
        tracks=[("ai_backend", "strong"), ("sde", "strong")],
        required_skills=[skill_req("Python", "FastAPI")],
        batch_years=[],  # Unspecified graduation year
    )
    seed_job_in_db(repo, job_id, posting)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(
        cand.id, [(job_id, posting)], profile_version=prof.version, as_of=AS_OF
    )

    # Default feed (eligibility=None) MUST include NEEDS_VERIFICATION
    recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility=None
    )
    assert len(recs) == 1
    rec = recs[0]
    assert rec["job_id"] == "job_unstated_batch"
    assert rec["eligibility_status"] == "NEEDS_VERIFICATION"
    assert rec["technical_score"] is not None and rec["technical_score"] > 0
    # Must have clear verification blockers explaining graduation criteria
    assert any(
        "graduates qualify" in r.lower() or "graduation" in r.lower()
        for r in rec["eligibility_reasons"]
    )


def test_scenario_2_explicitly_excluded_graduation_year_hidden_from_default_feed_but_in_transparency_log(
    repo: Repo,
) -> None:
    """A posting explicitly excluding 2027 is NOT_ELIGIBLE and excluded from default feed, but in transparency log."""
    cand, prof = create_test_candidate(
        repo, "Candidate 2027", "cand2027@test.local", grad_year=2027
    )

    # Job explicitly accepts only 2024 and 2025 graduates
    job_id, posting = make_test_posting(
        job_id="job_senior_only",
        title="Junior Backend Engineer",
        company="LegacyCorp",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python")],
        batch_years=[2024, 2025],
    )
    seed_job_in_db(repo, job_id, posting)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(
        cand.id, [(job_id, posting)], profile_version=prof.version, as_of=AS_OF
    )

    # 1. Default feed must NOT include NOT_ELIGIBLE job
    default_recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility=None
    )
    assert len(default_recs) == 0

    # 2. Transparency log (eligibility='not_eligible') MUST include the job with full reasons
    ineligible_recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility="not_eligible"
    )
    assert len(ineligible_recs) == 1
    rec = ineligible_recs[0]
    assert rec["job_id"] == "job_senior_only"
    assert rec["eligibility_status"] == "NOT_ELIGIBLE"
    assert any(
        "2027" in r or "batch" in r.lower() or "graduation" in r.lower()
        for r in rec["eligibility_reasons"]
    )

    # 3. All evaluated roles (eligibility='all') must also include the job
    all_recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility="all"
    )
    assert len(all_recs) == 1
    assert all_recs[0]["job_id"] == "job_senior_only"


def test_scenario_3_explicitly_accepted_graduation_year_qualifies_as_eligible(repo: Repo) -> None:
    """A posting explicitly specifying candidate batch qualifies as ELIGIBLE."""
    cand, prof = create_test_candidate(
        repo, "Candidate 2027", "match2027@test.local", grad_year=2027
    )

    job_id, posting = make_test_posting(
        job_id="job_batch_match",
        title="Software Engineer Intern",
        company="ModernAI",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python", "FastAPI")],
        batch_years=[2026, 2027],
    )
    seed_job_in_db(repo, job_id, posting)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(
        cand.id, [(job_id, posting)], profile_version=prof.version, as_of=AS_OF
    )

    # Must be ELIGIBLE
    recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility=None
    )
    assert len(recs) == 1
    assert recs[0]["eligibility_status"] == "ELIGIBLE"

    # Filter by 'eligible' also returns it
    filter_recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility="eligible"
    )
    assert len(filter_recs) == 1
    assert filter_recs[0]["job_id"] == "job_batch_match"


def test_scenario_4_unknown_experience_requirements_remain_transparent_in_verification_reasons(
    repo: Repo,
) -> None:
    """When requirements are unknown or unstated, reasons explain the exact uncertainty."""
    cand, prof = create_test_candidate(repo, "Candidate Exp", "exp@test.local", grad_year=2027)

    job_id, posting = make_test_posting(
        job_id="job_unknown_exp",
        title="Full Stack Developer",
        company="StartupX",
        tracks=[("full_stack", "strong")],
        required_skills=[skill_req("Python")],
        batch_years=[],
        experience_status="not_stated",
    )
    seed_job_in_db(repo, job_id, posting)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(
        cand.id, [(job_id, posting)], profile_version=prof.version, as_of=AS_OF
    )

    recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility="needs_verification"
    )
    assert len(recs) == 1
    rec = recs[0]
    assert rec["eligibility_status"] == "NEEDS_VERIFICATION"
    # Verification reasons should have transparent entries
    assert len(rec["eligibility_reasons"]) > 0


def test_scenario_5_missing_technical_score_remains_none_and_unscored(repo: Repo) -> None:
    """Missing or unscored technical fit remains None and is never turned into 0."""
    cand, prof = create_test_candidate(repo, "Candidate Unscored", "unscored@test.local")

    seed_job_in_db(
        repo, "job_no_skills", JobPosting(company="MysteryCorp", role="Unspecified Role")
    )

    insert_test_assessment(
        repo=repo,
        candidate_id=cand.id,
        job_id="job_no_skills",
        profile_id=prof.id,
        profile_version=prof.version,
        track="ai_backend",
        technical_score=None,
        eligibility_status="NEEDS_VERIFICATION",
        eligibility_reasons=["Requirements unstated"],
    )

    recs = repo.get_candidate_recommendations(cand.id, profile_version=prof.version)
    assert len(recs) == 1
    assert recs[0]["technical_score"] is None
    # Must NOT be 0
    assert recs[0]["technical_score"] != 0


def test_scenario_6_candidate_isolation_preserved_across_recommendations(repo: Repo) -> None:
    """Candidate isolation: Candidate A and Candidate B evaluations on same job do not leak or overwrite."""
    cand_a, prof_a = create_test_candidate(
        repo, "Alice 2027", "alice2027@test.local", grad_year=2027
    )
    cand_b, prof_b = create_test_candidate(repo, "Bob 2024", "bob2024@test.local", grad_year=2024)

    # A 2027-only job
    job_id, posting = make_test_posting(
        job_id="job_isolation_test",
        title="AI Engineer Trainee",
        company="NextGen",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python")],
        batch_years=[2027],
    )
    seed_job_in_db(repo, job_id, posting)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(
        cand_a.id, [(job_id, posting)], profile_version=prof_a.version, as_of=AS_OF
    )
    service.assess_candidate_jobs(
        cand_b.id, [(job_id, posting)], profile_version=prof_b.version, as_of=AS_OF
    )

    # Candidate A sees the job in default recommendations
    recs_a = repo.get_candidate_recommendations(cand_a.id, profile_version=prof_a.version)
    assert len(recs_a) == 1
    assert recs_a[0]["eligibility_status"] == "ELIGIBLE"

    # Candidate B does NOT see the job in default recommendations
    recs_b = repo.get_candidate_recommendations(cand_b.id, profile_version=prof_b.version)
    assert len(recs_b) == 0

    # Candidate B sees it in transparency log as NOT_ELIGIBLE
    recs_b_ineligible = repo.get_candidate_recommendations(
        cand_b.id, profile_version=prof_b.version, eligibility="not_eligible"
    )
    assert len(recs_b_ineligible) == 1
    assert recs_b_ineligible[0]["eligibility_status"] == "NOT_ELIGIBLE"


def test_scenario_7_closed_and_hidden_jobs_excluded_from_active_recommendations(repo: Repo) -> None:
    """Jobs marked as HIDDEN or DISCARDED are never shown in candidate recommendations."""
    cand, prof = create_test_candidate(repo, "Active Cand", "active@test.local")

    job_id, posting = make_test_posting(
        job_id="job_hidden_state",
        title="Hidden Role",
        company="GhostCorp",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python")],
        batch_years=[2027],
    )
    # Seed as HIDDEN
    seed_job_in_db(repo, job_id, posting, status=JobStatus.HIDDEN.value)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(
        cand.id, [(job_id, posting)], profile_version=prof.version, as_of=AS_OF
    )

    # Active recommendations should exclude hidden jobs
    recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility="all"
    )
    assert len(recs) == 0


def test_scenario_8_track_specific_best_match_sorting(repo: Repo) -> None:
    """Track-specific Best Match ranking orders exclusively by the selected track."""
    cand, prof = create_test_candidate(repo, "MultiTrack Candidate", "multitrack@test.local")

    # Seed 2 jobs
    for jid, title in [("job_1", "Role 1"), ("job_2", "Role 2")]:
        seed_job_in_db(
            repo,
            jid,
            JobPosting(company="Corp", role=title, locations=["Bengaluru, India"]),
        )

    # Assessment 1: job_1 has ai_backend=95, sdet=40
    # Assessment 2: job_2 has ai_backend=50, sdet=90
    insert_test_assessment(
        repo, cand.id, "job_1", prof.id, prof.version, "ai_backend", 95, "ELIGIBLE"
    )
    insert_test_assessment(repo, cand.id, "job_1", prof.id, prof.version, "sdet", 40, "ELIGIBLE")
    insert_test_assessment(
        repo, cand.id, "job_2", prof.id, prof.version, "ai_backend", 50, "ELIGIBLE"
    )
    insert_test_assessment(repo, cand.id, "job_2", prof.id, prof.version, "sdet", 90, "ELIGIBLE")

    # Filter track='ai_backend' -> job_1 (95) before job_2 (50)
    ai_recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, track="ai_backend"
    )
    assert [r["job_id"] for r in ai_recs] == ["job_1", "job_2"]

    # Filter track='sdet' -> job_2 (90) before job_1 (40)
    sdet_recs = repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, track="sdet"
    )
    assert [r["job_id"] for r in sdet_recs] == ["job_2", "job_1"]


def test_scenario_9_candidate_dashboard_web_rendering(repo: Repo, clock: FakeClock) -> None:
    """Dashboard HTML endpoint renders segmented sections, warning banners, coverage, and unscored state."""
    cand, prof = create_test_candidate(repo, "Web Candidate", "webcand@test.local", grad_year=2027)

    # Job A: Confirmed ELIGIBLE
    job_a_id, posting_a = make_test_posting(
        job_id="job_web_eligible",
        title="AI Engineer",
        company="AlphaAI",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python", "FastAPI")],
        batch_years=[2026, 2027],
    )
    seed_job_in_db(repo, job_a_id, posting_a)

    # Job B: NEEDS_VERIFICATION (batch unspecified)
    job_b_id, posting_b = make_test_posting(
        job_id="job_web_verify",
        title="Backend Intern",
        company="BetaTech",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python")],
        batch_years=[],
    )
    seed_job_in_db(repo, job_b_id, posting_b)

    # Job C: NOT_ELIGIBLE (batch 2024-2025)
    job_c_id, posting_c = make_test_posting(
        job_id="job_web_ineligible",
        title="Senior Lead",
        company="GammaCorp",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python")],
        batch_years=[2024, 2025],
    )
    seed_job_in_db(repo, job_c_id, posting_c)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(
        cand.id,
        [(job_a_id, posting_a), (job_b_id, posting_b), (job_c_id, posting_c)],
        profile_version=prof.version,
        as_of=AS_OF,
    )

    st = settings()
    app = create_app(st, repo, clock=clock)
    token = repo.get_setting("web_token") or "test_token"
    session_token = sign_candidate_session(cand.id, token)

    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: session_token})

    # 1. Default dashboard view:
    res = client.get("/candidate/dashboard")
    assert res.status_code == 200
    html = res.text

    # Confirmed Matches section exists
    assert "Confirmed Matches" in html
    assert "job_web_eligible" in html

    # Opportunities Pending Verification section exists with warning banner
    assert "Opportunities Pending Verification" in html
    assert "Requirements Unspecified:" in html
    assert "job_web_verify" in html

    # NOT_ELIGIBLE job is NOT in the default feed
    assert "job_web_ineligible" not in html

    # Evidence coverage badge is present
    assert "% Evidence" in html

    # 2. Ineligible roles filter:
    res_ineligible = client.get("/candidate/dashboard?eligibility=not_eligible")
    assert res_ineligible.status_code == 200
    html_inelig = res_ineligible.text

    assert "Ineligible Roles (Transparency Log)" in html_inelig
    assert "job_web_ineligible" in html_inelig
    assert "job_web_eligible" not in html_inelig


def test_scenario_10_find_jobs_eligibility_filter_consistency(repo: Repo) -> None:
    """Repo.find_jobs() supports 'eligible' matching ELIGIBLE and LIKELY_ELIGIBLE, and case-insensitivity."""
    cand, prof = create_test_candidate(repo, "Filter Candidate", "filter@test.local")

    for jid, title in [("fj_1", "Job 1"), ("fj_2", "Job 2"), ("fj_3", "Job 3")]:
        seed_job_in_db(
            repo, jid, JobPosting(company="TestCorp", role=title, locations=["Bengaluru, India"])
        )

    insert_test_assessment(
        repo, cand.id, "fj_1", prof.id, prof.version, "ai_backend", 80, "ELIGIBLE"
    )
    insert_test_assessment(
        repo, cand.id, "fj_2", prof.id, prof.version, "ai_backend", 70, "LIKELY_ELIGIBLE"
    )
    insert_test_assessment(
        repo, cand.id, "fj_3", prof.id, prof.version, "ai_backend", 60, "NEEDS_VERIFICATION"
    )

    # find_jobs with eligibility='eligible' returns fj_1 (ELIGIBLE) and fj_2 (LIKELY_ELIGIBLE)
    jobs_eligible, total_el = repo.find_jobs(
        statuses=None,
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version=None,
        track=None,
        eligibility="eligible",
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="newest",
        limit=10,
        offset=0,
        candidate_id=cand.id,
    )
    assert total_el == 2
    assert {j[0].id for j in jobs_eligible} == {"fj_1", "fj_2"}

    # find_jobs with eligibility='needs_verification' (case-insensitive) returns fj_3
    jobs_verify, total_v = repo.find_jobs(
        statuses=None,
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version=None,
        track=None,
        eligibility="needs_verification",
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="newest",
        limit=10,
        offset=0,
        candidate_id=cand.id,
    )
    assert total_v == 1
    assert jobs_verify[0][0].id == "fj_3"
