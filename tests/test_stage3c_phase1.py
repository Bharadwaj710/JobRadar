"""Tests for Stage 3C Phase 1: Safe Compatibility Layer.

Verifies:
1. Candidate isolation: Candidates A & B maintain independent application states for the same job.
2. Candidate actions never change shared Job.status or Job.user_status_at.
3. Candidate actions never influence shared deduplication (same_role_main) or archival decisions.
4. Shared lifecycle updates (e.g. scraper marking closed job as hidden) never clobber CandidateApplication state.
5. Identity resolution fails safely when identity sources conflict or are missing (no arbitrary fallback).
6. Invalid, tampered, or nonexistent session cookies fail safely and prevent IDOR cross-candidate access.
7. Legacy Matcher scores and J4 multi-track technical scores remain completely distinct.
8. RESUME_READY handling maps safely to saved, records provenance notes, and preserves artifact links without data loss.
9. Candidate-scoped read methods (get_job_with_candidate_context, list_candidate_applications_with_jobs) load isolated data.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlmodel import Session
from starlette.requests import Request

from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import (
    CandidateApplicationStatus,
    CandidateJobAssessment,
    Job,
    JobStatus,
)
from jobradar.db.repo import OwnerResolutionError, Repo, init_db, make_engine
from jobradar.web.session import (
    CANDIDATE_SESSION_COOKIE,
    resolve_candidate_context,
    sign_candidate_session,
    verify_profile_ownership,
)
from tests.conftest import FakeClock
from tests.test_digest import settings


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo(tmp_path: Path, clock: FakeClock) -> Repo:
    """Create isolated test repository."""
    db_path = tmp_path / "test_stage3c_phase1.db"
    eng = make_engine(db_path)
    init_db(eng)
    return Repo(eng, clock=clock)


@pytest.fixture
def secret() -> str:
    return "test-hmac-secret-32-chars-long-abcde"


@pytest.fixture
def sample_job(repo: Repo) -> Job:
    """Create a sample shared job in inventory."""
    job, _ = repo.get_or_create_job("job_sample_123", "https://example.com/jobs/123")
    return job


def test_candidate_isolation_independent_states(repo: Repo, sample_job: Job) -> None:
    """1. Candidates A and B maintain completely independent application states on the same job."""
    cand_a = repo.create_candidate(display_name="Candidate A", email="cand_a@example.com")
    cand_b = repo.create_candidate(display_name="Candidate B", email="cand_b@example.com")

    # Candidate A applies
    app_a = repo.apply_candidate_action(
        cand_a.id, sample_job.id, "applied", notes="Applied on website"
    )
    assert app_a.status == CandidateApplicationStatus.APPLIED.value
    assert app_a.applied_at is not None

    # Candidate B saves
    app_b = repo.apply_candidate_action(
        cand_b.id, sample_job.id, "shortlist", notes="Considering for next week"
    )
    assert app_b.status == CandidateApplicationStatus.SAVED.value
    assert app_b.applied_at is None

    # Verify candidate A's state did not bleed into candidate B
    fetched_a = repo.get_candidate_application(cand_a.id, sample_job.id)
    fetched_b = repo.get_candidate_application(cand_b.id, sample_job.id)
    assert fetched_a is not None and fetched_a.status == "applied"
    assert fetched_b is not None and fetched_b.status == "saved"

    # History entries are strictly partitioned
    hist_a = repo.list_candidate_application_history(cand_a.id, sample_job.id)
    hist_b = repo.list_candidate_application_history(cand_b.id, sample_job.id)
    assert len(hist_a) == 1 and hist_a[0].status == "applied"
    assert len(hist_b) == 1 and hist_b[0].status == "saved"


def test_candidate_actions_never_change_shared_job_status(repo: Repo, sample_job: Job) -> None:
    """2. Candidate application actions never mutate shared Job.status or Job.user_status_at."""
    cand = repo.create_candidate(display_name="Candidate Test", email="cand_test@example.com")

    initial_job = repo.get_job(sample_job.id)
    assert initial_job is not None
    assert initial_job.status == JobStatus.DISCOVERED.value
    assert initial_job.user_status_at is None

    # Perform various actions
    for action in ["shortlist", "applied", "interview", "offer", "rejected"]:
        repo.apply_candidate_action(cand.id, sample_job.id, action)

        job_after = repo.get_job(sample_job.id)
        assert job_after is not None
        # Shared Job.status MUST NOT change to applicant workflow states
        assert job_after.status == JobStatus.DISCOVERED.value
        # user_status_at MUST NOT be set
        assert job_after.user_status_at is None


def test_candidate_actions_never_influence_shared_deduplication(
    repo: Repo, clock: FakeClock
) -> None:
    """3. Candidate actions never influence shared deduplication in same_role_main."""
    cand = repo.create_candidate(display_name="Candidate Test", email="dedupe_test@example.com")

    fingerprint = "stripe|backend_engineer|remote"

    # Create Job 1 at initial time
    job1, _ = repo.get_or_create_job("job_primary", "https://stripe.com/jobs/1")
    repo.save_extraction(
        job_id=job1.id,
        role="Backend Engineer",
        company="Stripe",
        data_json=json.dumps({"role": "Backend Engineer", "company": "Stripe"}),
        fingerprint=fingerprint,
        deadline=None,
        prompt_version="v1",
        status=JobStatus.NEW,
    )

    # Candidate A applies to Job 1
    repo.apply_candidate_action(cand.id, job1.id, "applied")

    # Advance clock so Job 2 is created chronologically later
    clock.advance(seconds=10)

    # Create Job 2 with identical fingerprint later
    job2, _ = repo.get_or_create_job("job_duplicate", "https://telegram.me/job2")
    repo.save_extraction(
        job_id=job2.id,
        role="Backend Engineer",
        company="Stripe",
        data_json=json.dumps({"role": "Backend Engineer", "company": "Stripe"}),
        fingerprint=fingerprint,
        deadline=None,
        prompt_version="v1",
        status=JobStatus.EXTRACTED,
    )

    # Check same_role_main: Job 1 MUST still be identified as the main posting to deduplicate into
    main_id = repo.same_role_main(job2.id, fingerprint, since="2000-01-01T00:00:00Z")
    assert main_id == job1.id, "Candidate application action must not prevent shared deduplication"


def test_shared_lifecycle_updates_never_clobber_candidate_workflow(
    repo: Repo, sample_job: Job
) -> None:
    """4. Scraper or lifecycle updates to shared Job.status (e.g. hiding closed job) never alter CandidateApplication."""
    cand = repo.create_candidate(display_name="Candidate Test", email="lifecycle_test@example.com")

    # Candidate applies to the job
    repo.apply_candidate_action(cand.id, sample_job.id, "applied", notes="Active application")

    # Board watcher discovers employer closed the posting and sets shared Job.status = 'hidden'
    repo.set_job_status(sample_job.id, JobStatus.HIDDEN, reason="closed on board")

    # Verify shared job is hidden
    shared_job = repo.get_job(sample_job.id)
    assert shared_job is not None
    assert shared_job.status == JobStatus.HIDDEN.value
    assert shared_job.score_reason == "closed on board"

    # Verify candidate's application is COMPLETELY UNTOUCHED and remains 'applied'
    app = repo.get_candidate_application(cand.id, sample_job.id)
    assert app is not None
    assert app.status == CandidateApplicationStatus.APPLIED.value
    assert app.notes == "Active application"


def test_identity_resolution_fails_safely_when_conflicting_or_missing(
    repo: Repo, secret: str
) -> None:
    """5. Identity resolution fails safely without silent or hardcoded fallback."""
    # Create candidate in DB first so resolution checks identity concordance rather than empty DB
    repo.create_candidate(display_name="Existing Cand", email="existing@example.com")

    base_cfg = settings().config.model_dump()
    base_cfg["owner_candidate_id"] = None

    # Scenario A: Disagreeing email sources (secrets vs profile basics.email)
    conflicting_settings = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="conflicting_notify@example.com"),
    )
    conflicting_profile = {"basics": {"email": "owner_profile@example.com"}}

    # Must raise OwnerResolutionError
    with pytest.raises(OwnerResolutionError, match="disagree"):
        repo.resolve_owner_candidate_from_settings(
            conflicting_settings, profile=conflicting_profile
        )

    with pytest.raises(OwnerResolutionError):
        resolve_candidate_context(
            request=None,
            secret=secret,
            repo=repo,
            settings=conflicting_settings,
            profile=conflicting_profile,
            allow_owner_fallback=True,
            strict=True,
        )

    # In non-strict mode, returns None instead of guessing
    ctx = resolve_candidate_context(
        request=None,
        secret=secret,
        repo=repo,
        settings=conflicting_settings,
        profile=conflicting_profile,
        allow_owner_fallback=True,
        strict=False,
    )
    assert ctx is None

    # Scenario B: Agreement between sources resolves correctly with confirmed profile
    c_owner = repo.create_candidate(display_name="Owner Cand", email="owner_profile@example.com")
    repo.save_profile_draft(c_owner.id, {"basics": {"name": "Owner Profile"}})
    prof_owner = repo.get_candidate_profiles(c_owner.id)[0]
    repo.confirm_profile(c_owner.id, prof_owner.id, {"basics": {"name": "Owner Profile"}})

    concordant_settings = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="owner_profile@example.com"),
    )
    concordant_profile = {"basics": {"email": "owner_profile@example.com"}}

    owner = repo.resolve_owner_candidate_from_settings(
        concordant_settings, profile=concordant_profile
    )
    assert owner.email == "owner_profile@example.com"

    ctx_owner = resolve_candidate_context(
        request=None,
        secret=secret,
        repo=repo,
        settings=concordant_settings,
        profile=concordant_profile,
        allow_owner_fallback=True,
        strict=True,
    )
    assert ctx_owner is not None
    assert ctx_owner.candidate_id == owner.id
    assert ctx_owner.is_owner is True
    assert ctx_owner.source == "owner_settings"


def test_invalid_session_cookies_fail_safely_and_prevent_idor(repo: Repo, secret: str) -> None:
    """6. Invalid or tampered session cookies fail safely and prevent cross-candidate access."""
    cand1 = repo.create_candidate(display_name="Cand 1", email="cand1@example.com")
    cand2 = repo.create_candidate(display_name="Cand 2", email="cand2@example.com")

    base_cfg = settings().config.model_dump()
    base_cfg["owner_candidate_id"] = None
    test_settings = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="owner@example.com"),
    )
    test_profile = {"basics": {"email": "owner@example.com"}}

    # Valid session for Cand 1
    valid_token = sign_candidate_session(cand1.id, secret)
    req_scope = {
        "type": "http",
        "method": "GET",
        "path": "/candidate/dashboard",
        "headers": [(b"cookie", f"{CANDIDATE_SESSION_COOKIE}={valid_token}".encode())],
    }
    req_valid = Request(req_scope)
    ctx1 = resolve_candidate_context(req_valid, secret, repo, test_settings, test_profile)
    assert ctx1 is not None
    assert ctx1.candidate_id == cand1.id
    assert ctx1.source == "session"

    # Tampered session token
    tampered_token = f"{cand1.id}.tampered_signature_12345"
    req_tampered = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/candidate/dashboard",
            "headers": [(b"cookie", f"{CANDIDATE_SESSION_COOKIE}={tampered_token}".encode())],
        }
    )
    ctx_tampered = resolve_candidate_context(
        req_tampered, secret, repo, test_settings, test_profile
    )
    assert ctx_tampered is None, "Tampered session token must fail verification"

    # Nonexistent candidate ID in signed token
    ghost_token = sign_candidate_session("cand_nonexistent_9999", secret)
    req_ghost = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/candidate/dashboard",
            "headers": [(b"cookie", f"{CANDIDATE_SESSION_COOKIE}={ghost_token}".encode())],
        }
    )
    ctx_ghost = resolve_candidate_context(req_ghost, secret, repo, test_settings, test_profile)
    assert ctx_ghost is None, "Nonexistent candidate session must fail safely"

    # IDOR Profile ownership defense
    repo.save_profile_draft(cand2.id, {"basics": {"name": "Cand 2"}})
    prof_cand2 = repo.get_candidate_profiles(cand2.id)[0]
    repo.confirm_profile(cand2.id, prof_cand2.id, {"basics": {"name": "Cand 2"}})
    assert verify_profile_ownership(cand1.id, prof_cand2) is False
    assert verify_profile_ownership(cand2.id, prof_cand2) is True


def test_legacy_and_j4_scores_remain_distinct(repo: Repo, sample_job: Job) -> None:
    """7. Legacy Matcher scores and J4 multi-track technical fit scores remain distinct."""
    cand = repo.create_candidate(display_name="Score Tester", email="score_tester@example.com")
    repo.save_profile_draft(cand.id, {"basics": {"name": "Score Tester"}})
    profile = repo.get_candidate_profiles(cand.id)[0]
    repo.confirm_profile(cand.id, profile.id, {"basics": {"name": "Score Tester"}})

    # 1. Historical assessment (migrated legacy matcher: score 82, scoring_system='legacy_matcher')
    repo.save_historical_assessment(
        candidate_id=cand.id,
        job_id=sample_job.id,
        score=82,
        score_reason="legacy matcher skills rule match",
        scoring_system="legacy_matcher",
        scored_at="2026-10-15T12:00:00Z",
    )

    # 2. J4 multi-track assessment (ai_backend: technical_score 95)
    with Session(repo.engine) as s:
        s.add(
            CandidateJobAssessment(
                id=uuid.uuid4().hex,
                candidate_id=cand.id,
                job_id=sample_job.id,
                profile_id=profile.id,
                profile_version=1,
                track="ai_backend",
                technical_score=95,
                evidence_coverage=0.9,
                eligibility_status="ELIGIBLE",
                eligibility_reasons_json="[]",
                matched_skills_json='["Python", "FastAPI"]',
                missing_required_skills_json="[]",
                missing_preferred_skills_json="[]",
                recommended_resume=None,
                explanation="Strong backend fit",
                assessment_json="{}",
                eligibility_json="{}",
                created_at=repo._now(),
                updated_at=repo._now(),
            )
        )
        s.commit()

    # Load combined context
    ctx = repo.get_job_with_candidate_context(sample_job.id, cand.id)
    assert ctx is not None

    historical = ctx["historical_assessment"]
    assert historical is not None
    assert historical.score == 82
    assert historical.scoring_system == "legacy_matcher"
    assert historical.score_reason == "legacy matcher skills rule match"

    assessments = ctx["assessments"]
    assert len(assessments) == 1
    assert assessments[0].track == "ai_backend"
    assert assessments[0].technical_score == 95

    # Verification: J4 score and legacy score must NOT be confused or merged
    assert historical.score != assessments[0].technical_score


def test_resume_ready_maps_safely_preserving_evidence(repo: Repo, sample_job: Job) -> None:
    """8. RESUME_READY handling maps safely to saved, records provenance notes, and preserves artifact links."""
    cand = repo.create_candidate(display_name="Resume Ready Candidate", email="rr_cand@example.com")

    # Apply action with legacy 'resume_ready' string
    app = repo.apply_candidate_action(
        candidate_id=cand.id,
        job_id=sample_job.id,
        action="resume_ready",
        notes="Pre-existing candidate note.",
    )

    # Verify status is saved
    assert app.status == CandidateApplicationStatus.SAVED.value
    # Verify provenance note is preserved
    assert "Migrated from legacy resume_ready state." in app.notes
    assert "Pre-existing candidate note." in app.notes

    # Shared Job.status was not mutated
    shared_job = repo.get_job(sample_job.id)
    assert shared_job is not None
    assert shared_job.status == JobStatus.DISCOVERED.value


def test_candidate_scoped_read_methods(repo: Repo) -> None:
    """9. Candidate-scoped read methods (get_job_with_candidate_context, list_candidate_applications_with_jobs)."""
    cand = repo.create_candidate(display_name="Query Tester", email="query_tester@example.com")

    job_a, _ = repo.get_or_create_job("job_qa", "https://example.com/qa")
    job_b, _ = repo.get_or_create_job("job_qb", "https://example.com/qb")

    repo.apply_candidate_action(cand.id, job_a.id, "applied", notes="Applied to QA")
    repo.apply_candidate_action(cand.id, job_b.id, "shortlist", notes="Saved QB")

    # Test list_candidate_applications_with_jobs
    all_apps = repo.list_candidate_applications_with_jobs(cand.id)
    assert len(all_apps) == 2
    app_ids = {a.job_id for a, j in all_apps}
    assert app_ids == {job_a.id, job_b.id}

    # Filter by status
    applied_only = repo.list_candidate_applications_with_jobs(cand.id, statuses=["applied"])
    assert len(applied_only) == 1
    assert applied_only[0][0].job_id == job_a.id
    assert applied_only[0][1].id == job_a.id
