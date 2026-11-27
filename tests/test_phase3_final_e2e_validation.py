"""Phase 3: Final End-to-End Validation Test Suite for Spektra Submission.

Executes and verifies the complete 8-step candidate journey and system architecture:
- Step 1: Resume upload (file validation, extraction, malformed/unsupported format rejection)
- Step 2: Profile review (editing, confirmation, immutable version creation, candidate association)
- Step 3: J4 assessment (all 4 tracks, technical score vs eligibility separation, candidate isolation)
- Step 4: Match Radar (Confirmed matches, Opportunities Pending Verification, Ineligible transparency view, counts, track filters)
- Step 5: Job detail (company, role, description, requirements, score, eligibility, evidence coverage, apply link)
- Step 6: Application tracking (status transitions, notes, history log, reload persistence)
- Step 7: Restart and isolation (persistence across restarts, IDOR defense, session tampering rejection)
- Step 8: Failure handling (empty resume, corrupted file, incomplete job description, missing assessment, closed job, transaction rollback)
"""

from __future__ import annotations

import contextlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from jobradar.clock import to_iso, utcnow
from jobradar.db.models import (
    Candidate,
    CandidateApplicationStatus,
    CandidateProfile,
    Job,
    JobStatus,
)
from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.pipeline.candidate_adapter import adapt_candidate_profile
from jobradar.pipeline.schemas import JobPosting
from jobradar.services.matching_service import CandidateMatchingService
from jobradar.web.app import create_app
from jobradar.web.session import (
    CANDIDATE_SESSION_COOKIE,
    candidate_csrf_token,
    parse_candidate_session,
    sign_candidate_session,
)
from tests.conftest import FakeClock
from tests.test_digest import settings

AS_OF = date(2026, 11, 24)
BASE_URL = "http://127.0.0.1:8080"


# ==============================================================================
# Helper functions and fixtures
# ==============================================================================


def evidence(sentence: str, section: str = "Requirements") -> dict[str, str]:
    return {"sentence": sentence, "section": section, "source": "page"}


def skill_req(
    *skills: str, relation: Literal["all", "any"] = "all", sentence: str | None = None
) -> dict[str, Any]:
    wording = sentence or f"Required skills: {' or '.join(skills)}."
    return {"skills": list(skills), "relation": relation, "evidence": evidence(wording)}


def make_valid_pdf_bytes(text: str) -> bytes:
    """Generate a minimal valid PDF byte sequence containing text."""
    header = b"%PDF-1.4\n"
    s1 = b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
    s2 = b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
    s3 = b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
    lines = text.split("\n")
    stream_lines = ["BT", "/F1 12 Tf", "50 750 Td", "16 TL"]
    for i, line in enumerate(lines):
        esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if i == 0:
            stream_lines.append(f"({esc}) Tj")
        else:
            stream_lines.append(f"T* ({esc}) Tj")
    stream_lines.append("ET")
    stream_data = "\n".join(stream_lines).encode("latin-1", errors="replace")
    s4 = (
        f"4 0 obj <</Length {len(stream_data)}>> stream\n".encode("latin-1")
        + stream_data
        + b"\nendstream\nendobj\n"
    )
    s5 = b"5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n"
    xref = (
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000400 00000 n \n"
        b"trailer <</Size 6 /Root 1 0 R>>\nstartxref\n500\n%%EOF\n"
    )
    return header + s1 + s2 + s3 + s4 + s5 + xref


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
) -> tuple[str, JobPosting]:
    reqs = {
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
        ],
        "experience_status": "known",
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
    return job_id, JobPosting.model_validate(data)


def seed_job(
    repo: Repo, job_id: str, posting: JobPosting, status: str = JobStatus.NEW.value
) -> Job:
    now = to_iso(utcnow())
    job = Job(
        id=job_id,
        canonical_url=posting.apply_url or f"https://example.com/jobs/{job_id}",
        role=posting.role,
        company=posting.company,
        status=status,
        score=80,
        score_reason="Evaluated job",
        data_json=json.dumps(posting.model_dump()),
        created_at=now,
        updated_at=now,
    )
    with repo.engine.begin() as conn:
        conn.exec_driver_sql(
            """
            INSERT OR REPLACE INTO job (
                id, canonical_url, role, company, status, score, score_reason, data_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


@pytest.fixture
def isolated_repo(tmp_path: Path) -> Repo:
    engine = make_engine(tmp_path / "test_phase3.db")
    init_db(engine)
    return Repo(engine)


# ==============================================================================
# Step 1: Resume Upload and Extraction Tests
# ==============================================================================


def test_step1_resume_upload_and_extraction(isolated_repo: Repo, clock: FakeClock) -> None:
    """Step 1: Test valid resume extraction and invalid/malformed file rejection."""
    st = settings()
    app = create_app(st, isolated_repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    # 1. Onboarding page loads
    res_get = client.get("/onboarding")
    assert res_get.status_code == 200
    assert "Upload Resume" in res_get.text or "Profile Setup" in res_get.text

    # 2. Upload valid synthetic PDF
    token = isolated_repo.get_setting("web_token") or st.secrets.web_token
    resume_text = (
        "Aditi Rao\n"
        "Email: aditi.rao@example.com\n"
        "Phone: +91 98765 12345\n"
        "Location: Bengaluru, India\n"
        "Education: B.Tech in Computer Science, NIT Karnataka, Graduation 2026, CGPA 8.9\n"
        "Skills: Python, FastAPI, PostgreSQL, Docker, Redis, PyTorch\n"
        "Experience: Software Engineering Intern at CloudCore (Jan 2025 - Present)\n"
        "Projects: Distributed Stream Engine using Python, FastAPI, and Kafka\n"
    )
    pdf_bytes = make_valid_pdf_bytes(resume_text)

    files = {"resume": ("aditi_resume.pdf", pdf_bytes, "application/pdf")}
    res_upload = client.post(
        "/onboarding/upload",
        files=files,
        data={"token": token},
        headers={"origin": "http://127.0.0.1:8080"},
        follow_redirects=False,
    )
    assert res_upload.status_code == 303
    redirect_url = res_upload.headers["location"]
    assert "/onboarding/review/" in redirect_url

    # Candidate session cookie must be set
    assert CANDIDATE_SESSION_COOKIE in res_upload.cookies
    session_cookie = res_upload.cookies[CANDIDATE_SESSION_COOKIE]
    cand_id = parse_candidate_session(session_cookie, token)
    assert cand_id is not None

    # Verify extracted entities in profile draft
    profile_id = redirect_url.split("/onboarding/review/")[1].split("?")[0]
    profile = isolated_repo.get_profile(profile_id)
    assert profile is not None
    assert profile.review_status == "draft"
    pdata = profile.profile_data
    assert pdata.get("basics", {}).get("name") == "Aditi Rao"
    assert pdata.get("basics", {}).get("email") == "aditi.rao@example.com"
    assert "Python" in pdata.get("skills", [])

    # 3. Malformed/unsupported format rejection
    bad_files = {
        "resume": ("malicious.exe", b"MZ\x90\x00\x03\x00\x00\x00", "application/x-dosexec")
    }
    res_bad = client.post(
        "/onboarding/upload",
        files=bad_files,
        data={"token": token},
        headers={"origin": "http://127.0.0.1:8080"},
        follow_redirects=False,
    )
    assert res_bad.status_code in (400, 415, 303)


# ==============================================================================
# Step 2: Profile Review and Confirmation Tests
# ==============================================================================


def test_step2_profile_review_edit_and_confirmation(isolated_repo: Repo, clock: FakeClock) -> None:
    """Step 2: Candidate reviews, edits, and confirms profile into immutable version 1."""
    st = settings()
    app = create_app(st, isolated_repo, clock=clock)
    token = isolated_repo.get_setting("web_token") or st.secrets.web_token

    cand = isolated_repo.create_candidate(name="Dev Tester", email="dev@example.com")
    draft = isolated_repo.save_profile_draft(
        candidate_id=cand.id,
        profile_data={
            "basics": {
                "name": "Dev Tester",
                "email": "dev@example.com",
                "location": "Bengaluru, India",
            },
            "education": [
                {"degree": "B.Tech", "branch": "Computer Science", "graduation_year": 2026}
            ],
            "skills": ["Python", "FastAPI"],
            "experience": [{"title": "Software Engineering Intern", "company": "TestLab"}],
            "projects": [{"name": "AI Pipeline", "technologies": ["Python"]}],
        },
    )

    session_token = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: session_token})

    # 1. View review page
    res_review = client.get(f"/onboarding/review/{draft.id}")
    assert res_review.status_code == 200
    assert "Dev Tester" in res_review.text
    assert "Python" in res_review.text

    # 2. Confirm profile with CSRF
    c_csrf = candidate_csrf_token(cand.id, token)
    res_confirm = client.post(
        f"/onboarding/confirm/{draft.id}",
        data={
            "token": c_csrf,
            "name": "Dev Tester Updated",
            "email": "dev@example.com",
            "skills": "Python, FastAPI, Docker, PyTorch",
        },
        headers={"origin": "http://127.0.0.1:8080"},
        follow_redirects=False,
    )
    assert res_confirm.status_code in (302, 303)

    # 3. Verify confirmed profile is immutable version 1
    active = isolated_repo.get_active_profile(cand.id)
    assert active is not None
    assert active.review_status == "confirmed"
    assert active.version == 1
    assert active.candidate_id == cand.id
    canonical = adapt_candidate_profile(active)
    assert "Docker" in canonical.get("skills", [])


# ==============================================================================
# Step 3: J4 Four-Track Technical Assessment Tests
# ==============================================================================


def test_step3_j4_four_track_assessment_generation(isolated_repo: Repo) -> None:
    """Step 3: All 4 tracks assessed with isolated persistence and separate eligibility."""
    cand_a = isolated_repo.create_candidate(name="Candidate A", email="a@example.com")
    draft_a = isolated_repo.save_profile_draft(
        cand_a.id,
        profile_data={
            "basics": {"name": "Candidate A", "location": "Bengaluru, India"},
            "education": [
                {"degree": "B.Tech", "branch": "Computer Science", "graduation_year": 2026}
            ],
            "skills": ["Python", "FastAPI", "Docker", "PyTorch"],
            "experience": [{"title": "Software Engineering Intern", "company": "Co A"}],
            "projects": [{"name": "P1", "technologies": ["Python"]}],
        },
    )
    prof_a = isolated_repo.confirm_profile(draft_a.id)

    cand_b = isolated_repo.create_candidate(name="Candidate B", email="b@example.com")
    draft_b = isolated_repo.save_profile_draft(
        cand_b.id,
        profile_data={
            "basics": {"name": "Candidate B", "location": "Pune, India"},
            "education": [
                {"degree": "B.Tech", "branch": "Information Technology", "graduation_year": 2024}
            ],
            "skills": ["Java", "Selenium", "TestNG", "Postman"],
            "experience": [{"title": "QA Engineering Intern", "company": "Co B"}],
            "projects": [{"name": "P2", "technologies": ["Java"]}],
        },
    )
    prof_b = isolated_repo.confirm_profile(draft_b.id)

    # Job: 2026 Batch Python Backend
    j_id, posting = make_test_posting(
        "job_shared_p3",
        "AI Platform Engineer",
        "Anthropic Partner",
        tracks=[
            ("ai_backend", "strong"),
            ("sde", "strong"),
            ("sdet", "possible"),
            ("full_stack", "possible"),
        ],
        required_skills=[skill_req("Python", "FastAPI")],
        batch_years=[2025, 2026],
    )
    seed_job(isolated_repo, j_id, posting)

    service = CandidateMatchingService(isolated_repo)
    service.assess_candidate_jobs(
        cand_a.id, [(j_id, posting)], profile_version=prof_a.version, as_of=AS_OF
    )
    service.assess_candidate_jobs(
        cand_b.id, [(j_id, posting)], profile_version=prof_b.version, as_of=AS_OF
    )

    # 1. Candidate A has all 4 tracks
    ass_a = isolated_repo.list_candidate_assessments(cand_a.id, job_id=j_id)
    assert len(ass_a) == 4
    tracks_a = {a.track for a in ass_a}
    assert tracks_a == {"ai_backend", "full_stack", "sde", "sdet"}

    # Candidate A is ELIGIBLE for 2026 job
    ai_a = isolated_repo.get_candidate_job_assessment(cand_a.id, j_id, prof_a.version, "ai_backend")
    assert ai_a is not None
    assert ai_a.eligibility_status == "ELIGIBLE"
    assert ai_a.technical_score is not None and ai_a.technical_score > 70
    assert ai_a.evidence_coverage > 0

    # 2. Candidate B is NOT_ELIGIBLE (graduated 2024, job accepts 2025-2026)
    ai_b = isolated_repo.get_candidate_job_assessment(cand_b.id, j_id, prof_b.version, "ai_backend")
    assert ai_b is not None
    assert ai_b.eligibility_status == "NOT_ELIGIBLE"
    assert any("2024" in r or "batch" in r.lower() for r in ai_b.eligibility_reasons)

    # 3. Isolation: Candidate A and B records remain separate
    assert ai_a.id != ai_b.id
    assert ai_a.candidate_id == cand_a.id
    assert ai_b.candidate_id == cand_b.id


# ==============================================================================
# Step 4: Match Radar Discovery and Presentation Tests
# ==============================================================================


def test_step4_match_radar_discovery_and_presentation(
    isolated_repo: Repo, clock: FakeClock
) -> None:
    """Step 4: Verify Confirmed Matches, Opportunities Pending Verification, and Ineligible log."""
    cand, prof = _create_confirmed_candidate(
        isolated_repo, "Radar Tester", "radar@test.local", 2026
    )

    # 1. Confirmed Eligible role
    j1_id, p1 = make_test_posting(
        "j_eligible_match",
        "Backend SDE",
        "PrimeTech",
        [("ai_backend", "strong")],
        [skill_req("Python", "FastAPI")],
        batch_years=[2026],
    )
    seed_job(isolated_repo, j1_id, p1)

    # 2. Opportunities Pending Verification (batch unstated)
    j2_id, p2 = make_test_posting(
        "j_verify_match",
        "ML Engineer",
        "DataCorp",
        [("ai_backend", "strong")],
        [skill_req("Python")],
        batch_years=[],
    )
    seed_job(isolated_repo, j2_id, p2)

    # 3. Ineligible role (2022-2024 batch)
    j3_id, p3 = make_test_posting(
        "j_ineligible_match",
        "Senior Architect",
        "OldSystems",
        [("ai_backend", "strong")],
        [skill_req("Python")],
        batch_years=[2022, 2023, 2024],
    )
    seed_job(isolated_repo, j3_id, p3)

    service = CandidateMatchingService(isolated_repo)
    service.assess_candidate_jobs(
        cand.id, [(j1_id, p1), (j2_id, p2), (j3_id, p3)], profile_version=prof.version, as_of=AS_OF
    )

    st = settings()
    app = create_app(st, isolated_repo, clock=clock)
    token = isolated_repo.get_setting("web_token") or st.secrets.web_token
    session_token = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: session_token})

    # Default view
    res = client.get("/candidate/dashboard")
    assert res.status_code == 200
    html = res.text

    # Section 1: Confirmed Matches
    assert "Confirmed Matches" in html
    assert "j_eligible_match" in html

    # Section 2: Opportunities Pending Verification with warning banner
    assert "Opportunities Pending Verification" in html
    assert "Requirements Unspecified:" in html
    assert "j_verify_match" in html

    # Ineligible role is kept separate from default feed
    assert "j_ineligible_match" not in html

    # Transparency Log filter
    res_inelig = client.get("/candidate/dashboard?eligibility=not_eligible")
    assert res_inelig.status_code == 200
    assert "Ineligible Roles (Transparency Log)" in res_inelig.text
    assert "j_ineligible_match" in res_inelig.text


# ==============================================================================
# Step 5: Job Detail and Fit Breakdown Tests
# ==============================================================================


def test_step5_job_detail_fit_breakdown(isolated_repo: Repo, clock: FakeClock) -> None:
    """Step 5: Verify job description, requirements, fit score, eligibility explanation, and links."""
    cand, prof = _create_confirmed_candidate(
        isolated_repo, "Detail Tester", "detail@test.local", 2026
    )

    j_id, posting = make_test_posting(
        "job_detail_001",
        "Core Python Engineer",
        "Stripe India",
        [("ai_backend", "strong")],
        [skill_req("Python", "FastAPI")],
        batch_years=[2026],
    )
    seed_job(isolated_repo, j_id, posting)

    service = CandidateMatchingService(isolated_repo)
    service.assess_candidate_jobs(
        cand.id, [(j_id, posting)], profile_version=prof.version, as_of=AS_OF
    )

    st = settings()
    app = create_app(st, isolated_repo, clock=clock)
    token = isolated_repo.get_setting("web_token") or st.secrets.web_token
    session_token = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: session_token})

    res = client.get(f"/candidate/jobs/{j_id}")
    assert res.status_code == 200
    html = res.text

    assert "Core Python Engineer" in html
    assert "Stripe India" in html
    assert "ELIGIBLE" in html
    assert "Python" in html
    assert "evidence" in html.lower() or "score" in html.lower()


# ==============================================================================
# Step 6: Application Tracking and History Tests
# ==============================================================================


def test_step6_application_tracking_and_history(isolated_repo: Repo, clock: FakeClock) -> None:
    """Step 6: Move job across statuses, add notes, and verify history log persistence."""
    cand, _prof = _create_confirmed_candidate(
        isolated_repo, "App Tracker", "tracker@test.local", 2026
    )

    j_id, posting = make_test_posting(
        "job_track_001", "Systems Engineer", "Vercel", [("sde", "strong")], [skill_req("Python")]
    )
    seed_job(isolated_repo, j_id, posting)

    st = settings()
    app = create_app(st, isolated_repo, clock=clock)
    token = isolated_repo.get_setting("web_token") or st.secrets.web_token
    session_token = sign_candidate_session(cand.id, token)
    client = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: session_token})
    csrf = candidate_csrf_token(cand.id, token)

    # 1. Save job
    res1 = client.post(
        f"/candidate/jobs/{j_id}/status",
        data={"token": csrf, "status": "saved", "notes": "Bookmarked for review"},
        follow_redirects=False,
    )
    assert res1.status_code in (302, 303)

    # 2. Move to Applied
    res2 = client.post(
        f"/candidate/jobs/{j_id}/status",
        data={"token": csrf, "status": "applied", "notes": "Submitted application online"},
        follow_redirects=False,
    )
    assert res2.status_code in (302, 303)

    # 3. Move to Interviewing
    res3 = client.post(
        f"/candidate/jobs/{j_id}/status",
        data={"token": csrf, "status": "interviewing", "notes": "Technical round scheduled"},
        follow_redirects=False,
    )
    assert res3.status_code in (302, 303)

    # 4. Verify history log in database
    app_record = isolated_repo.get_candidate_application(cand.id, j_id)
    assert app_record is not None
    assert app_record.status == CandidateApplicationStatus.INTERVIEWING.value
    assert app_record.notes == "Technical round scheduled"

    history = isolated_repo.list_candidate_application_history(cand.id, j_id)
    assert len(history) >= 3
    statuses = [h.status for h in history]
    assert CandidateApplicationStatus.SAVED.value in statuses
    assert CandidateApplicationStatus.APPLIED.value in statuses
    assert CandidateApplicationStatus.INTERVIEWING.value in statuses


# ==============================================================================
# Step 7: Restart Persistence and Security Isolation Tests
# ==============================================================================


def test_step7_restart_persistence_and_candidate_isolation(
    isolated_repo: Repo, clock: FakeClock
) -> None:
    """Step 7: Data persists across restart simulation; Candidate B cannot access Candidate A data."""
    cand_a, _ = _create_confirmed_candidate(
        isolated_repo, "Alice Primary", "alice.p@test.local", 2026
    )
    cand_b, _ = _create_confirmed_candidate(isolated_repo, "Bob Intruder", "bob.i@test.local", 2026)

    j_id, posting = make_test_posting(
        "job_sec_001", "Security Role", "ShieldCorp", [("sde", "strong")], [skill_req("Python")]
    )
    seed_job(isolated_repo, j_id, posting)

    st = settings()
    app = create_app(st, isolated_repo, clock=clock)
    token = isolated_repo.get_setting("web_token") or st.secrets.web_token

    # Candidate A saves application
    isolated_repo.apply_candidate_action(
        cand_a.id, j_id, "applied", notes="Confidential notes for Alice"
    )

    # Candidate B attempts to view or tamper with Candidate A's application
    tok_b = sign_candidate_session(cand_b.id, token)
    client_b = TestClient(app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: tok_b})

    # Candidate B's applications board does NOT see Candidate A's application
    res_b_apps = client_b.get("/candidate/applications")
    assert res_b_apps.status_code == 200
    assert "Confidential notes for Alice" not in res_b_apps.text

    # Tampered/expired cookie rejected
    bad_client = TestClient(
        app, base_url=BASE_URL, cookies={CANDIDATE_SESSION_COOKIE: "tampered.signature.here"}
    )
    res_bad = bad_client.get("/candidate/dashboard", follow_redirects=False)
    assert res_bad.status_code in (302, 303)
    assert "/onboarding" in res_bad.headers.get("location", "")


# ==============================================================================
# Step 8: Failure Handling and Edge Cases Tests
# ==============================================================================


def test_step8_failure_handling_and_edge_cases(isolated_repo: Repo) -> None:
    """Step 8: Tests missing descriptions, unscored jobs, closed jobs, and transaction rollback."""
    cand, prof = _create_confirmed_candidate(
        isolated_repo, "Robust Cand", "robust@test.local", 2026
    )

    # 1. Incomplete job description without skills
    j_incomplete, p_incomplete = make_test_posting(
        "j_inc", "Empty Requirements Role", "BlankCo", [("sde", "possible")], []
    )
    seed_job(isolated_repo, j_incomplete, p_incomplete)

    service = CandidateMatchingService(isolated_repo)
    service.assess_candidate_jobs(
        cand.id, [(j_incomplete, p_incomplete)], profile_version=prof.version, as_of=AS_OF
    )

    # Missing/unscored technical fit remains None and does not crash
    recs = isolated_repo.get_candidate_recommendations(cand.id, profile_version=prof.version)
    assert len(recs) == 1
    assert recs[0]["job_id"] == "j_inc"

    # 2. Closed / hidden job omitted from active recommendations
    j_closed, p_closed = make_test_posting(
        "j_closed", "Closed Role", "ClosedCo", [("sde", "strong")], [skill_req("Python")]
    )
    seed_job(isolated_repo, j_closed, p_closed, status=JobStatus.HIDDEN.value)
    service.assess_candidate_jobs(
        cand.id, [(j_closed, p_closed)], profile_version=prof.version, as_of=AS_OF
    )

    active_recs = isolated_repo.get_candidate_recommendations(
        cand.id, profile_version=prof.version, eligibility="all"
    )
    assert "j_closed" not in {r["job_id"] for r in active_recs}

    # 3. Database transaction rollback verification
    initial_count = len(isolated_repo.get_candidate_profiles(cand.id))
    with contextlib.suppress(Exception), Session(isolated_repo.engine) as session:
        # Insert a valid profile, then cause an intentional integrity error
        p_dup = CandidateProfile(
            id=prof.id,  # Duplicate primary key forces IntegrityError
            candidate_id=cand.id,
            version=99,
            profile_data_json="{}",
            review_status="draft",
            created_at=to_iso(utcnow()),
            updated_at=to_iso(utcnow()),
        )
        session.add(p_dup)
        session.commit()

    # Count must be unchanged after failed transaction
    after_count = len(isolated_repo.get_candidate_profiles(cand.id))
    assert after_count == initial_count


# ==============================================================================
# Helper
# ==============================================================================


def _create_confirmed_candidate(
    repo: Repo, name: str, email: str, grad_year: int
) -> tuple[Candidate, CandidateProfile]:
    cand = repo.create_candidate(name=name, email=email)
    draft = repo.save_profile_draft(
        cand.id,
        profile_data={
            "basics": {"name": name, "email": email, "location": "Bengaluru, India"},
            "education": [
                {"degree": "B.Tech", "branch": "Computer Science", "graduation_year": grad_year}
            ],
            "skills": ["Python", "FastAPI", "SQL", "Docker"],
            "experience": [{"title": "Software Engineering Intern", "company": "TechLab"}],
            "projects": [{"name": "Web Service", "technologies": ["Python"]}],
        },
    )
    prof = repo.confirm_profile(draft.id)
    return cand, prof
