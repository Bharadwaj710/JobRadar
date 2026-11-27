"""Tests for Stage 3C.2 Phase 3.2: Closed-Job Recommendation Filtering.

Verifies:
1. Open jobs (e.g. 'new', 'extracted', 'discovered') can appear in candidate recommendations.
2. Hidden jobs ('hidden') cannot appear in candidate recommendations.
3. Discarded jobs ('discarded') cannot appear in candidate recommendations.
4. Existing applications for closed jobs remain visible in Kanban board and application lists.
5. Filtering candidate recommendations never mutates shared Job records.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from jobradar.clock import to_iso
from jobradar.config import Settings
from jobradar.db.models import (
    Candidate,
    CandidateApplicationStatus,
    CandidateJobAssessment,
    JobStatus,
)
from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.web import views
from tests.conftest import FakeClock
from tests.test_digest import settings as make_settings

TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo(tmp_path: Path, clock: FakeClock) -> Repo:
    db_path = tmp_path / "test_stage3c_phase3_2.db"
    eng = make_engine(db_path)
    init_db(eng)
    return Repo(eng, clock=clock)


@pytest.fixture
def test_settings() -> Settings:
    return make_settings()


@pytest.fixture
def candidate_alice(repo: Repo) -> Candidate:
    return repo.create_candidate(display_name="Candidate Alice", email="alice@test.local")


@pytest.fixture
def candidate_bob(repo: Repo) -> Candidate:
    return repo.create_candidate(display_name="Candidate Bob", email="bob@test.local")


def _add_assessment(
    repo: Repo,
    candidate_id: str,
    job_id: str,
    profile_id: str,
    profile_version: int,
    track: str = "ai_backend",
    score: int = 90,
    eligibility_status: str = "ELIGIBLE",
) -> CandidateJobAssessment:
    assessment = CandidateJobAssessment(
        id=uuid.uuid4().hex,
        candidate_id=candidate_id,
        job_id=job_id,
        profile_id=profile_id,
        profile_version=profile_version,
        track=track,
        technical_score=score,
        evidence_coverage=0.9,
        eligibility_status=eligibility_status,
        assessment_json="{}",
        eligibility_json="{}",
        explanation=f"Strong fit in {track}",
        created_at=to_iso(datetime.now(UTC)),
        updated_at=to_iso(datetime.now(UTC)),
    )
    with Session(repo.engine) as s:
        s.add(assessment)
        s.commit()
    return assessment


# ---------------------------------------------------------------------------------------------
# Test 1: Open jobs can appear in recommendations
# ---------------------------------------------------------------------------------------------
def test_1_open_jobs_can_appear_in_recommendations(repo: Repo, candidate_alice: Candidate) -> None:
    prof = repo.save_profile_draft(
        candidate_id=candidate_alice.id,
        resume_id=None,
        profile_data={"basics": {"name": "Alice"}, "skills": ["Python", "FastAPI"]},
    )

    # Create open jobs in active states
    job_new, _ = repo.get_or_create_job("job_open_new", "https://example.com/jobs/open-new")
    repo.set_job_data(job_new.id, json.dumps({"role": "Backend Engineer", "company": "Stripe"}))
    repo.set_job_status(job_new.id, JobStatus.NEW)

    job_extracted, _ = repo.get_or_create_job(
        "job_open_extracted", "https://example.com/jobs/open-extracted"
    )
    repo.set_job_data(
        job_extracted.id, json.dumps({"role": "Platform Engineer", "company": "GitHub"})
    )
    repo.set_job_status(job_extracted.id, JobStatus.EXTRACTED)

    _add_assessment(repo, candidate_alice.id, job_new.id, prof.id, prof.version, score=92)
    _add_assessment(repo, candidate_alice.id, job_extracted.id, prof.id, prof.version, score=88)

    recs = repo.get_candidate_recommendations(candidate_alice.id, prof.version)
    rec_job_ids = [r["job_id"] for r in recs]

    assert job_new.id in rec_job_ids
    assert job_extracted.id in rec_job_ids
    assert len(recs) == 2


# ---------------------------------------------------------------------------------------------
# Test 2: Hidden jobs cannot appear in recommendations
# ---------------------------------------------------------------------------------------------
def test_2_hidden_jobs_cannot_appear_in_recommendations(
    repo: Repo, candidate_alice: Candidate
) -> None:
    prof = repo.save_profile_draft(
        candidate_id=candidate_alice.id,
        resume_id=None,
        profile_data={"basics": {"name": "Alice"}, "skills": ["Python"]},
    )

    # Open job
    job_open, _ = repo.get_or_create_job("job_active", "https://example.com/jobs/active")
    repo.set_job_data(job_open.id, json.dumps({"role": "Backend Engineer", "company": "Stripe"}))
    repo.set_job_status(job_open.id, JobStatus.NEW)
    _add_assessment(repo, candidate_alice.id, job_open.id, prof.id, prof.version, score=90)

    # Hidden job (e.g. requisition closed by employer ATS)
    job_hidden, _ = repo.get_or_create_job("job_closed", "https://example.com/jobs/closed")
    repo.set_job_data(job_hidden.id, json.dumps({"role": "Python Dev", "company": "OldCorp"}))
    repo.set_job_status(job_hidden.id, JobStatus.HIDDEN, "posting removed from careers page")
    _add_assessment(repo, candidate_alice.id, job_hidden.id, prof.id, prof.version, score=95)

    recs = repo.get_candidate_recommendations(candidate_alice.id, prof.version)
    rec_job_ids = [r["job_id"] for r in recs]

    # Only open job is recommended; hidden job MUST be excluded despite higher score
    assert job_open.id in rec_job_ids
    assert job_hidden.id not in rec_job_ids
    assert len(recs) == 1


# ---------------------------------------------------------------------------------------------
# Test 3: Discarded jobs cannot appear in recommendations
# ---------------------------------------------------------------------------------------------
def test_3_discarded_jobs_cannot_appear_in_recommendations(
    repo: Repo, candidate_alice: Candidate
) -> None:
    prof = repo.save_profile_draft(
        candidate_id=candidate_alice.id,
        resume_id=None,
        profile_data={"basics": {"name": "Alice"}, "skills": ["Python"]},
    )

    job_discarded, _ = repo.get_or_create_job("job_spam", "https://example.com/jobs/spam")
    repo.set_job_data(job_discarded.id, json.dumps({"role": "Ad Spam Role", "company": "ScamCo"}))
    repo.set_job_status(job_discarded.id, JobStatus.DISCARDED, "scam/spam filter triggered")
    _add_assessment(repo, candidate_alice.id, job_discarded.id, prof.id, prof.version, score=99)

    recs = repo.get_candidate_recommendations(candidate_alice.id, prof.version)
    rec_job_ids = [r["job_id"] for r in recs]

    assert job_discarded.id not in rec_job_ids
    assert len(recs) == 0


# ---------------------------------------------------------------------------------------------
# Test 4: Existing applications for closed jobs remain visible in Kanban board
# ---------------------------------------------------------------------------------------------
def test_4_existing_applications_for_closed_jobs_remain_visible(
    repo: Repo, candidate_alice: Candidate, test_settings: Settings
) -> None:
    prof = repo.save_profile_draft(
        candidate_id=candidate_alice.id,
        resume_id=None,
        profile_data={"basics": {"name": "Alice"}, "skills": ["Python"]},
    )

    job_applied, _ = repo.get_or_create_job("job_applied_1", "https://example.com/jobs/applied-1")
    repo.set_job_data(job_applied.id, json.dumps({"role": "Staff Engineer", "company": "Figma"}))
    repo.set_job_status(job_applied.id, JobStatus.NEW)

    _add_assessment(repo, candidate_alice.id, job_applied.id, prof.id, prof.version, score=91)

    # Alice applies to the job
    app = repo.apply_candidate_action(
        candidate_alice.id, job_applied.id, "applied", notes="Applied on careers page"
    )
    assert app.status == CandidateApplicationStatus.APPLIED.value

    # Employer subsequently closes the role
    repo.set_job_status(job_applied.id, JobStatus.HIDDEN, "closed by employer")

    # 1. Closed job must NOT appear in recommendations
    recs = repo.get_candidate_recommendations(candidate_alice.id, prof.version)
    assert job_applied.id not in [r["job_id"] for r in recs]

    # 2. Existing application MUST remain intact in repository
    app_reloaded = repo.get_candidate_application(candidate_alice.id, job_applied.id)
    assert app_reloaded is not None
    assert app_reloaded.status == CandidateApplicationStatus.APPLIED.value
    assert app_reloaded.notes == "Applied on careers page"

    # 3. Application with job must be returned in list_candidate_applications_with_jobs
    apps_with_jobs = repo.list_candidate_applications_with_jobs(candidate_alice.id)
    assert len(apps_with_jobs) == 1
    found_app, found_job = apps_with_jobs[0]
    assert found_app.id == app.id
    assert found_job.id == job_applied.id
    assert found_job.status == JobStatus.HIDDEN.value

    # 4. Kanban board must show the card with closed_by_employer = True
    columns = views.board(repo, test_settings, datetime.now(UTC), candidate_id=candidate_alice.id)
    applied_col = next(col for col in columns if col.key == "applied")
    assert len(applied_col.cards) == 1
    assert applied_col.cards[0].card.closed_by_employer is True
    assert applied_col.cards[0].card.id == job_applied.id


# ---------------------------------------------------------------------------------------------
# Test 5: Filtering recommendations does not mutate shared Job state
# ---------------------------------------------------------------------------------------------
def test_5_filtering_recommendations_does_not_mutate_shared_job_state(
    repo: Repo, candidate_alice: Candidate, candidate_bob: Candidate
) -> None:
    prof_a = repo.save_profile_draft(
        candidate_id=candidate_alice.id,
        resume_id=None,
        profile_data={"basics": {"name": "Alice"}, "skills": ["Python"]},
    )
    prof_b = repo.save_profile_draft(
        candidate_id=candidate_bob.id,
        resume_id=None,
        profile_data={"basics": {"name": "Bob"}, "skills": ["Go"]},
    )

    job_hidden, _ = repo.get_or_create_job("job_hidden_shared", "https://example.com/jobs/shared")
    repo.set_job_data(job_hidden.id, json.dumps({"role": "SRE", "company": "Cloudflare"}))
    repo.set_job_status(job_hidden.id, JobStatus.HIDDEN, "requisition closed")

    _add_assessment(repo, candidate_alice.id, job_hidden.id, prof_a.id, prof_a.version, score=85)
    _add_assessment(repo, candidate_bob.id, job_hidden.id, prof_b.id, prof_b.version, score=89)

    initial_job = repo.get_job(job_hidden.id)
    assert initial_job is not None
    assert initial_job.status == JobStatus.HIDDEN.value
    assert initial_job.user_status_at is None

    # Query recommendations for Alice and Bob
    recs_a = repo.get_candidate_recommendations(candidate_alice.id, prof_a.version)
    recs_b = repo.get_candidate_recommendations(candidate_bob.id, prof_b.version)

    assert len(recs_a) == 0
    assert len(recs_b) == 0

    # Verify shared job record is completely unmutated
    job_after = repo.get_job(job_hidden.id)
    assert job_after is not None
    assert job_after.status == JobStatus.HIDDEN.value
    assert job_after.user_status_at is None
    assert job_after.created_at == initial_job.created_at
    assert job_after.updated_at == initial_job.updated_at
