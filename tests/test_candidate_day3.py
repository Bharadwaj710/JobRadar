"""Tests for Day 3: Candidate-Specific Dashboard & Application Tracking.

Verifies:
1. CandidateApplication and CandidateApplicationHistory models & repo methods.
2. The 8 Mandatory Verification Scenarios:
   - Scenario 1: Candidate-Specific Matching Divergence on Dashboard
   - Scenario 2: Hard Eligibility Exclusion
   - Scenario 3: Dashboard Candidate Isolation (No Cross-Candidate Leakage)
   - Scenario 4: Independent Application Tracking
   - Scenario 5: Ownership Enforcement / IDOR Defense
   - Scenario 6: Profile Version Freshness & Re-assessment
   - Scenario 7: Empty / Missing States
   - Scenario 8: CSRF Validation & Invalid Status Rejection
"""

from __future__ import annotations

from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient

from jobradar.db.models import (
    Candidate,
    CandidateProfile,
)
from jobradar.db.repo import Repo
from jobradar.pipeline.schemas import JobPosting
from jobradar.services.matching_service import CandidateMatchingService
from jobradar.web.app import create_app
from jobradar.web.session import (
    CANDIDATE_SESSION_COOKIE,
    candidate_csrf_token,
    sign_candidate_session,
)
from tests.conftest import FakeClock
from tests.test_digest import settings

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


def make_posting(
    job_id: str,
    title: str,
    company: str,
    tracks: list[tuple[str, str]],
    required_skills: list[dict[str, Any]],
    batch_years: list[int] | None = None,
    min_exp: float = 0.0,
    max_exp: float = 1.0,
) -> tuple[str, JobPosting]:
    reqs: dict[str, Any] = {
        "required_skills": required_skills,
        "preferred_skills": [],
        "experience": [
            {
                "need": "required",
                "experience_type": "professional",
                "min_years": min_exp,
                "max_years": max_exp,
                "evidence": evidence(f"Requires {min_exp} to {max_exp} years of experience."),
            }
        ],
        "degree_levels": ["bachelor"],
        "degree_fields": ["Computer Science", "Information Technology"],
    }
    track_roles = [
        {
            "track": trk,
            "strength": st,
            "evidence": [evidence(f"Responsibilities align with {trk}.", "Role")],
        }
        for trk, st in tracks
    ]
    posting = JobPosting(
        company=company,
        role=title,
        apply_url=f"https://careers.example.com/jobs/{job_id}",
        location="Bengaluru, India",
        batch_years=[2026, 2027] if batch_years is None else batch_years,
        requirements=reqs,
        role_tracks=track_roles,
    )
    return job_id, posting


def create_test_candidate(
    repo: Repo,
    name: str,
    email: str,
    branch: str = "Computer Science",
    grad_year: int = 2026,
    skills: list[str] | None = None,
    projects: list[dict[str, Any]] | None = None,
    confirm: bool = True,
) -> tuple[Candidate, CandidateProfile]:
    cand = repo.create_candidate(name=name, email=email)
    skill_list = skills or ["Python", "FastAPI"]
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
                "institution": "Indian Institute of Technology",
                "graduation_year": grad_year,
                "cgpa": "8.8",
            }
        ],
        "skills": skill_list,
        "experience": [],
        "projects": projects
        or [
            {
                "name": f"{name} Portfolio Project",
                "technologies": skill_list[:3],
                "description": "Production scalable project.",
            }
        ],
    }
    prof = repo.save_profile_draft(cand.id, profile_data=draft_data)
    if confirm:
        prof = repo.confirm_profile(prof.id, draft_data)
    return cand, prof


# ==============================================================================
# Model and Repo Unit Tests
# ==============================================================================


def test_candidate_application_repo_lifecycle(repo: Repo) -> None:
    """Test saving, updating, status validation, and auditing of candidate applications."""
    cand, _ = create_test_candidate(repo, "Test Cand", "cand@test.com")
    job_id = "job_repo_test_01"
    repo.get_or_create_job(job_id, f"https://example.com/jobs/{job_id}")

    # 1. Save application as 'saved'
    app = repo.save_candidate_application(
        candidate_id=cand.id,
        job_id=job_id,
        status="saved",
        notes="Looks interesting",
    )
    assert app.status == "saved"
    assert app.notes == "Looks interesting"
    assert app.applied_at is None

    # 2. Update to 'applied' -> applied_at auto-populated
    app_applied = repo.save_candidate_application(
        candidate_id=cand.id,
        job_id=job_id,
        status="applied",
        notes="Applied via website",
    )
    assert app_applied.id == app.id  # Same record updated
    assert app_applied.status == "applied"
    assert app_applied.applied_at is not None
    assert app_applied.notes == "Applied via website"

    # 3. Retrieve
    retrieved = repo.get_candidate_application(cand.id, job_id)
    assert retrieved is not None
    assert retrieved.status == "applied"

    # 4. History log
    hist = repo.list_candidate_application_history(cand.id, job_id)
    assert len(hist) == 2
    assert [h.status for h in hist] == ["applied", "saved"]

    # 5. Invalid status rejection
    with pytest.raises(ValueError, match="Invalid candidate application status"):
        repo.save_candidate_application(cand.id, job_id, status="hired_now")

    # 6. Counts
    counts = repo.get_candidate_application_counts(cand.id)
    assert counts["applied"] == 1
    assert counts["saved"] == 0
    assert counts["total"] == 1


# ==============================================================================
# Scenario 1: Candidate-Specific Matching Divergence on Dashboard
# ==============================================================================


def test_scenario_1_candidate_matching_divergence(repo: Repo) -> None:
    """Verify Candidate A (AI/Backend) and Candidate B (SDET) see divergent personalized rankings."""
    cand_a, prof_a = create_test_candidate(
        repo,
        name="Aarav Sharma",
        email="aarav.ai@example.com",
        skills=["Python", "FastAPI", "PostgreSQL", "PyTorch", "Docker"],
        projects=[
            {
                "name": "LLM Retrieval Engine",
                "technologies": ["Python", "FastAPI", "PyTorch"],
                "description": "High performance vector retrieval service.",
            }
        ],
    )

    cand_b, prof_b = create_test_candidate(
        repo,
        name="Bhavna Patel",
        email="bhavna.sdet@example.com",
        skills=["Java", "Selenium", "Playwright", "TestNG", "Postman", "Cypress"],
        projects=[
            {
                "name": "E2E Test Automation Framework",
                "technologies": ["Java", "Selenium", "TestNG"],
                "description": "Automated regression suite for web applications.",
            }
        ],
    )

    j1_id, j1_posting = make_posting(
        "job_ai_backend_101",
        "AI Platform Backend Engineer",
        "CognitiveScale AI",
        tracks=[("ai_backend", "strong"), ("sde", "possible")],
        required_skills=[skill_req("Python", "FastAPI"), skill_req("Docker", "PostgreSQL")],
    )
    j2_id, j2_posting = make_posting(
        "job_sdet_qa_102",
        "SDET Automation Engineer",
        "QualityOps Labs",
        tracks=[("sdet", "strong"), ("full_stack", "possible")],
        required_skills=[skill_req("Selenium", "Playwright"), skill_req("Java", "TestNG")],
    )

    repo.get_or_create_job(j1_id, j1_posting.apply_url)
    repo.set_job_data(j1_id, j1_posting.model_dump_json())
    repo.get_or_create_job(j2_id, j2_posting.apply_url)
    repo.set_job_data(j2_id, j2_posting.model_dump_json())

    matcher = CandidateMatchingService(repo)
    matcher.assess_candidate_jobs(cand_a.id, [(j1_id, j1_posting), (j2_id, j2_posting)])
    matcher.assess_candidate_jobs(cand_b.id, [(j1_id, j1_posting), (j2_id, j2_posting)])

    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"

    # Candidate A
    token_a = sign_candidate_session(cand_a.id, token)
    client_a = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: token_a})
    res_a = client_a.get("/candidate/dashboard")
    assert res_a.status_code == 200
    html_a = res_a.text

    # Candidate B
    token_b = sign_candidate_session(cand_b.id, token)
    client_b = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: token_b})
    res_b = client_b.get("/candidate/dashboard")
    assert res_b.status_code == 200
    html_b = res_b.text

    recs_a = repo.get_candidate_recommendations(cand_a.id, prof_a.version)
    assert len(recs_a) == 2
    assert recs_a[0]["job_id"] == j1_id
    assert (recs_a[0]["technical_score"] or 0) > (recs_a[1]["technical_score"] or 0)
    assert "CognitiveScale AI" in html_a

    recs_b = repo.get_candidate_recommendations(cand_b.id, prof_b.version)
    assert len(recs_b) == 2
    assert recs_b[0]["job_id"] == j2_id
    assert (recs_b[0]["technical_score"] or 0) > (recs_b[1]["technical_score"] or 0)
    assert "QualityOps Labs" in html_b


# ==============================================================================
# Scenario 2: Hard Eligibility Exclusion
# ==============================================================================


def test_scenario_2_hard_eligibility_exclusion(repo: Repo) -> None:
    """Verify an ineligible job (batch mismatch) is marked NOT_ELIGIBLE and ranked below eligible jobs."""
    cand, prof = create_test_candidate(
        repo,
        name="Senior Grad",
        email="grad2024@example.com",
        grad_year=2024,
        skills=["Python", "FastAPI", "Docker"],
    )

    j_strict_id, j_strict_posting = make_posting(
        "job_strict_batch_2026",
        "Junior Python Developer (2026 Batch Only)",
        "CampusFirst Tech",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python", "FastAPI")],
        batch_years=[2026],
    )
    j_open_id, j_open_posting = make_posting(
        "job_open_batch_all",
        "Python Software Engineer",
        "OpenHire Systems",
        tracks=[("sde", "strong")],
        required_skills=[skill_req("Python")],
        batch_years=[2024, 2025, 2026],
    )

    repo.get_or_create_job(j_strict_id, j_strict_posting.apply_url)
    repo.set_job_data(j_strict_id, j_strict_posting.model_dump_json())
    repo.get_or_create_job(j_open_id, j_open_posting.apply_url)
    repo.set_job_data(j_open_id, j_open_posting.model_dump_json())

    matcher = CandidateMatchingService(repo)
    matcher.assess_candidate_jobs(
        cand.id, [(j_strict_id, j_strict_posting), (j_open_id, j_open_posting)]
    )

    # In default feed, NOT_ELIGIBLE is kept separate (1 open job returned)
    default_recs = repo.get_candidate_recommendations(cand.id, prof.version)
    assert len(default_recs) == 1
    assert default_recs[0]["job_id"] == j_open_id

    # In all evaluated roles, both jobs are returned with strict job ranked second
    recs = repo.get_candidate_recommendations(cand.id, prof.version, eligibility="all")
    assert len(recs) == 2

    # Open job is ELIGIBLE / LIKELY_ELIGIBLE / NEEDS_VERIFICATION and ranked FIRST
    assert recs[0]["job_id"] == j_open_id
    assert recs[0]["eligibility_status"] in ("ELIGIBLE", "LIKELY_ELIGIBLE", "NEEDS_VERIFICATION")

    # Strict job is marked NOT_ELIGIBLE and ranked SECOND
    assert recs[1]["job_id"] == j_strict_id
    assert recs[1]["eligibility_status"] == "NOT_ELIGIBLE"
    assert any("2026" in r or "graduates" in r for r in recs[1]["eligibility_reasons"])

    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"
    c_token = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: c_token})

    # Default dashboard does not show ineligible jobs
    res_default = client.get("/candidate/dashboard")
    assert res_default.status_code == 200
    assert "job_strict_batch_2026" not in res_default.text

    # Transparency log shows ineligible job
    res = client.get("/candidate/dashboard?eligibility=not_eligible")
    assert res.status_code == 200
    assert "NOT ELIGIBLE" in res.text


# ==============================================================================
# Scenario 3: Dashboard Candidate Isolation (No Cross-Candidate Leakage)
# ==============================================================================


def test_scenario_3_dashboard_candidate_isolation(repo: Repo) -> None:
    """Verify Candidate A and Candidate B see only their own data with zero leakage."""
    cand_a, _ = create_test_candidate(repo, "Candidate Alpha", "alpha.private@test.com")
    cand_b, _ = create_test_candidate(repo, "Candidate Beta", "beta.private@test.com")

    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"

    tok_a = sign_candidate_session(cand_a.id, token)
    client_a = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: tok_a})
    res_a = client_a.get("/candidate/dashboard")
    assert res_a.status_code == 200
    assert "Candidate Alpha" in res_a.text
    assert "alpha.private@test.com" in res_a.text
    assert "Candidate Beta" not in res_a.text
    assert "beta.private@test.com" not in res_a.text

    tok_b = sign_candidate_session(cand_b.id, token)
    client_b = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: tok_b})
    res_b = client_b.get("/candidate/dashboard")
    assert res_b.status_code == 200
    assert "Candidate Beta" in res_b.text
    assert "beta.private@test.com" in res_b.text
    assert "Candidate Alpha" not in res_b.text
    assert "alpha.private@test.com" not in res_b.text


# ==============================================================================
# Scenario 4: Independent Application Tracking
# ==============================================================================


def test_scenario_4_independent_application_tracking(repo: Repo) -> None:
    """Verify Candidate A applying to Job X does not affect Candidate B saving Job X."""
    cand_a, _ = create_test_candidate(repo, "Cand A", "a@track.com")
    cand_b, _ = create_test_candidate(repo, "Cand B", "b@track.com")

    shared_job_id = "job_shared_application_test"
    repo.get_or_create_job(shared_job_id, f"https://example.com/{shared_job_id}")

    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"

    # Candidate A applies
    tok_a = sign_candidate_session(cand_a.id, token)
    csrf_a = candidate_csrf_token(cand_a.id, token)
    client_a = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: tok_a})
    res_a = client_a.post(
        f"/candidate/jobs/{shared_job_id}/status",
        data={"status": "applied", "notes": "Applied on portal with resume v1", "token": csrf_a},
        follow_redirects=False,
    )
    assert res_a.status_code in (200, 303)

    # Candidate B saves
    tok_b = sign_candidate_session(cand_b.id, token)
    csrf_b = candidate_csrf_token(cand_b.id, token)
    client_b = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: tok_b})
    res_b = client_b.post(
        f"/candidate/jobs/{shared_job_id}/status",
        data={"status": "saved", "notes": "Want to research company first", "token": csrf_b},
        follow_redirects=False,
    )
    assert res_b.status_code in (200, 303)

    app_a = repo.get_candidate_application(cand_a.id, shared_job_id)
    assert app_a is not None
    assert app_a.status == "applied"
    assert app_a.notes == "Applied on portal with resume v1"

    app_b = repo.get_candidate_application(cand_b.id, shared_job_id)
    assert app_b is not None
    assert app_b.status == "saved"
    assert app_b.notes == "Want to research company first"

    board_a = client_a.get("/candidate/applications")
    assert board_a.status_code == 200
    assert "Applied on portal with resume v1" in board_a.text
    assert "Want to research company first" not in board_a.text

    board_b = client_b.get("/candidate/applications")
    assert board_b.status_code == 200
    assert "Want to research company first" in board_b.text
    assert "Applied on portal with resume v1" not in board_b.text


# ==============================================================================
# Scenario 5: Ownership Enforcement / IDOR Defense
# ==============================================================================


def test_scenario_5_ownership_enforcement_idor(repo: Repo) -> None:
    """Verify Candidate A cannot access or confirm Candidate B's profile."""
    cand_a, _ = create_test_candidate(repo, "Cand A", "a@sec.com")
    _, prof_b = create_test_candidate(repo, "Cand B", "b@sec.com")

    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"

    tok_a = sign_candidate_session(cand_a.id, token)
    client_a = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: tok_a})

    # Candidate A attempts to access Candidate B's review page
    res_view = client_a.get(f"/onboarding/review/{prof_b.id}")
    assert res_view.status_code == 403

    # Candidate A attempts to post confirmation for Candidate B's profile
    csrf_a = candidate_csrf_token(cand_a.id, token)
    res_confirm = client_a.post(
        f"/onboarding/confirm/{prof_b.id}",
        data={"name": "Hacked Name", "token": csrf_a},
        follow_redirects=False,
    )
    assert res_confirm.status_code == 403

    # Unauthenticated access to candidate dashboard redirects to onboarding
    unauth_client = TestClient(app, base_url=BASE_URL)
    res_unauth = unauth_client.get("/candidate/dashboard", follow_redirects=False)
    assert res_unauth.status_code in (302, 303)
    assert "/onboarding" in res_unauth.headers.get("location", "")


# ==============================================================================
# Scenario 6: Profile Version Freshness & Re-assessment
# ==============================================================================


def test_scenario_6_profile_version_freshness(repo: Repo) -> None:
    """Verify updating a profile to v2 triggers re-assessment against v2."""
    cand, prof_v1 = create_test_candidate(
        repo,
        "Version Cand",
        "ver@test.com",
        skills=["Python"],
    )
    assert prof_v1.version == 1

    job_id, posting = make_posting(
        "job_freshness_test",
        "Senior Backend Architect",
        "CloudCore Inc",
        tracks=[("ai_backend", "strong")],
        required_skills=[skill_req("Python"), skill_req("FastAPI")],
    )
    repo.get_or_create_job(job_id, posting.apply_url)
    repo.set_job_data(job_id, posting.model_dump_json())

    # Assess v1: missing FastAPI
    matcher = CandidateMatchingService(repo)
    v1_assessments = matcher.assess_candidate_jobs(cand.id, [(job_id, posting)], profile_version=1)
    assert len(v1_assessments) == 4
    v1_ai = next(a for a in v1_assessments if a.track == "ai_backend")
    v1_score = v1_ai.technical_score or 0

    # Candidate updates profile and confirms v2 with FastAPI added
    v2_data = dict(prof_v1.profile_data)
    v2_data["skills"] = ["Python", "FastAPI", "Docker", "PostgreSQL"]
    v2_draft = repo.save_profile_draft(cand.id, profile_data=v2_data)
    prof_v2 = repo.confirm_profile(v2_draft.id, v2_data)
    assert prof_v2.version == 2
    assert prof_v2.is_active is True

    # Web client dashboard triggers assessment for active version 2
    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"
    c_tok = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: c_tok})

    res = client.get("/candidate/dashboard")
    assert res.status_code == 200
    assert "Version 2" in res.text

    # Verify v2 assessment was created and has a higher score
    v2_assessments = repo.list_candidate_assessments(cand.id, job_id=job_id, profile_version=2)
    assert len(v2_assessments) == 4
    v2_ai = next(a for a in v2_assessments if a.track == "ai_backend")
    v2_score = v2_ai.technical_score or 0
    assert v2_score > v1_score


# ==============================================================================
# Scenario 7: Empty / Missing States
# ==============================================================================


def test_scenario_7_empty_and_draft_states(repo: Repo) -> None:
    """Verify empty applications board and unconfirmed draft dashboard render cleanly."""
    cand = repo.create_candidate(name="Draft Only Cand", email="draft@test.com")
    repo.save_profile_draft(cand.id, profile_data={"basics": {"name": "Draft Only Cand"}})

    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"
    c_tok = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: c_tok})

    # 1. Dashboard with unconfirmed draft
    res_dash = client.get("/candidate/dashboard")
    assert res_dash.status_code == 200
    assert "No Confirmed Profile Active" in res_dash.text
    assert "Review Existing Draft" in res_dash.text

    # 2. Applications board with 0 applications
    res_apps = client.get("/candidate/applications")
    assert res_apps.status_code == 200
    assert "No jobs saved" in res_apps.text
    assert "No jobs applied" in res_apps.text


# ==============================================================================
# Scenario 8: CSRF Validation & Invalid Status Rejection
# ==============================================================================


def test_scenario_8_csrf_and_invalid_status_rejection(repo: Repo) -> None:
    """Verify CSRF tampering is blocked and invalid application status is rejected."""
    cand, _ = create_test_candidate(repo, "Security Cand", "sec@test.com")
    job_id = "job_csrf_test_01"
    repo.get_or_create_job(job_id, f"https://example.com/{job_id}")

    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"
    c_tok = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: c_tok})

    # 1. Tampered / wrong CSRF token on status update -> 403 Forbidden
    res_bad_csrf = client.post(
        f"/candidate/jobs/{job_id}/status",
        data={"status": "applied", "token": "invalid_forged_csrf_token"},
        follow_redirects=False,
    )
    assert res_bad_csrf.status_code == 403

    # 2. Tampered CSRF on refresh -> 403 Forbidden
    res_bad_refresh = client.post(
        "/candidate/refresh",
        data={"token": "forged_refresh_token"},
        follow_redirects=False,
    )
    assert res_bad_refresh.status_code == 403

    # 3. Valid CSRF but invalid status value -> 400 Bad Request
    valid_csrf = candidate_csrf_token(cand.id, token)
    res_bad_status = client.post(
        f"/candidate/jobs/{job_id}/status",
        data={"status": "hired_full_time", "token": valid_csrf},
        follow_redirects=False,
    )
    assert res_bad_status.status_code == 400


# ==============================================================================
# Scenario 9: All Six Application Statuses & Candidate Isolation
# ==============================================================================


def test_scenario_9_all_six_application_statuses(repo: Repo) -> None:
    """Verify all six CandidateApplicationStatus states (saved, applied, interviewing,

    offered, rejected, skipped) are supported, counted, and rendered.
    """
    cand, _ = create_test_candidate(repo, "Status Tester", "statuses@test.com")
    st = settings()
    app = create_app(st, repo, clock=FakeClock())
    token = repo.get_setting("web_token") or "test_token"
    c_tok = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: c_tok})
    csrf = candidate_csrf_token(cand.id, token)

    all_statuses = ["saved", "applied", "interviewing", "offered", "rejected", "skipped"]
    for i, status in enumerate(all_statuses, start=1):
        job_id = f"job_status_{status}_{i}"
        repo.get_or_create_job(job_id, f"https://example.com/{job_id}")
        res = client.post(
            f"/candidate/jobs/{job_id}/status",
            data={"status": status, "notes": f"Testing {status} state", "token": csrf},
            follow_redirects=False,
        )
        assert res.status_code == 303, f"Failed updating to {status}"

    # Verify counts in repo
    counts = repo.get_candidate_application_counts(cand.id)
    for status in all_statuses:
        assert counts[status] == 1, f"Count for {status} should be 1, got {counts[status]}"
    assert counts["total"] == 6

    # Verify rendering on /candidate/applications
    res_apps = client.get("/candidate/applications")
    assert res_apps.status_code == 200
    for status in all_statuses:
        assert f"{status.capitalize()}: <b>1</b>" in res_apps.text, f"Missing badge for {status}"
        assert f'class="kanban-col-title-saas">{status.capitalize()}</span>' in res_apps.text

    # Verify /candidate/jobs status dropdown contains all 6 statuses
    res_job = client.get("/candidate/jobs/job_status_skipped_6")
    assert res_job.status_code == 200
    assert 'value="skipped"' in res_job.text
    assert 'value="saved"' in res_job.text
    assert 'value="applied"' in res_job.text
    assert 'value="interviewing"' in res_job.text
    assert 'value="offered"' in res_job.text
    assert 'value="rejected"' in res_job.text
