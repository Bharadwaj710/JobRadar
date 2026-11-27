"""Tests for Stage 3C Phase 3: Web Dashboard & View Layer Migration.

Verifies:
1. Candidate A cannot see Candidate B's application state (candidate isolation).
2. Candidate actions never change shared Job.status or Job.user_status_at.
3. Application status is loaded from CandidateApplication.
4. Current J4 scores are loaded from CandidateJobAssessment.
5. Legacy scores remain separately labeled and never become J4 scores.
6. Applied dates use CandidateApplication.applied_at (not job.updated_at or job.user_status_at).
7. Saved and skipped applications do not receive fabricated applied dates.
8. Closed jobs remain visible in candidate's application board with closed indicator.
9. CSRF and origin protections remain strictly effective on job action routes.
10. Existing dashboard filters, routes, and actions remain functional.
11. Missing assessments do not break dashboard rendering.
12. Candidate application history is preserved across transitions.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from jobradar.clock import to_iso
from jobradar.config import Settings
from jobradar.db.models import (
    Candidate,
    CandidateApplicationStatus,
    CandidateJobAssessment,
    Job,
    JobStatus,
)
from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.queue.tasks import TaskQueue
from jobradar.web import views
from jobradar.web.app import create_app, page_token
from jobradar.web.session import CANDIDATE_SESSION_COOKIE, sign_candidate_session
from tests.conftest import FakeClock
from tests.test_digest import settings as make_settings

TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo(tmp_path: Path, clock: FakeClock) -> Repo:
    db_path = tmp_path / "test_stage3c_phase3.db"
    eng = make_engine(db_path)
    init_db(eng)
    return Repo(eng, clock=clock)


@pytest.fixture
def test_settings() -> Settings:
    return make_settings()


@pytest.fixture
def queue(repo: Repo, clock: FakeClock) -> TaskQueue:
    return TaskQueue(repo, clock=clock)


@pytest.fixture
def app_instance(test_settings: Settings, repo: Repo, clock: FakeClock, queue: TaskQueue) -> Any:
    return create_app(test_settings, repo, clock=clock, queue=queue)


@pytest.fixture
def candidate_a(repo: Repo) -> Candidate:
    return repo.create_candidate(display_name="Candidate Alice", email="alice@test.local")


@pytest.fixture
def candidate_b(repo: Repo) -> Candidate:
    return repo.create_candidate(display_name="Candidate Bob", email="bob@test.local")


@pytest.fixture
def sample_job(repo: Repo) -> Job:
    job, _ = repo.get_or_create_job("job_sample_1", "https://example.com/jobs/sample-1")
    repo.save_extraction(
        job_id=job.id,
        role="Backend Engineer",
        company="Stripe",
        data_json=json.dumps({"role": "Backend Engineer", "company": "Stripe"}),
        fingerprint="stripe|backend|1",
        deadline=None,
        prompt_version="v1",
        status=JobStatus.NEW,
    )
    return job


def make_client_for_candidate(
    app_instance: Any, repo: Repo, candidate_id: str | None = None
) -> tuple[TestClient, str]:
    token = page_token(repo)
    client = TestClient(app_instance, base_url="http://127.0.0.1")
    if candidate_id:
        signed_cookie = sign_candidate_session(candidate_id, token)
        client.cookies.set(CANDIDATE_SESSION_COOKIE, signed_cookie)
    return client, token


# ---------------------------------------------------------------------------------------------
# Test 1: Candidate A cannot see Candidate B's application state (Candidate Isolation)
# ---------------------------------------------------------------------------------------------
def test_1_candidate_a_cannot_see_candidate_b_application_state(
    app_instance: Any, repo: Repo, candidate_a: Candidate, candidate_b: Candidate, sample_job: Job
) -> None:
    # Candidate A applies to sample_job
    repo.apply_candidate_action(candidate_a.id, sample_job.id, "applied", notes="Alice applied")

    client_b, _ = make_client_for_candidate(app_instance, repo, candidate_b.id)
    resp_b_board = client_b.get("/applications")
    assert resp_b_board.status_code == 200
    # Candidate B's board must not contain sample_job in Applied
    assert sample_job.id not in resp_b_board.text or "No applications yet" in resp_b_board.text

    # Detail view for Candidate B
    resp_b_job = client_b.get(f"/jobs/{sample_job.id}")
    assert resp_b_job.status_code == 200
    assert "Alice applied" not in resp_b_job.text
    assert "You haven't set a status yet" in resp_b_job.text

    # Candidate A's board must contain sample_job
    client_a, _ = make_client_for_candidate(app_instance, repo, candidate_a.id)
    resp_a_board = client_a.get("/applications")
    assert resp_a_board.status_code == 200
    assert sample_job.id in resp_a_board.text
    assert "Applied" in resp_a_board.text


# ---------------------------------------------------------------------------------------------
# Test 2: Candidate actions never change shared Job.status
# ---------------------------------------------------------------------------------------------
def test_2_candidate_actions_never_change_shared_job_status(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    initial_job = repo.get_job(sample_job.id)
    assert initial_job is not None
    assert initial_job.status == JobStatus.NEW.value
    assert initial_job.user_status_at is None

    client_a, token = make_client_for_candidate(app_instance, repo, candidate_a.id)

    # Candidate A takes action "applied"
    resp_act = client_a.post(
        f"/jobs/{sample_job.id}/act",
        data={"action": "applied", "token": token, "notes": "Applied on career site"},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert resp_act.status_code in (200, 303)

    # Shared Job.status MUST NOT change
    job_after_act = repo.get_job(sample_job.id)
    assert job_after_act is not None
    assert job_after_act.status == JobStatus.NEW.value
    assert job_after_act.user_status_at is None

    # Candidate A sets status "interview"
    resp_status = client_a.post(
        f"/jobs/{sample_job.id}/status",
        data={"status": "interviewing", "token": token},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert resp_status.status_code in (200, 303)

    # Shared Job.status MUST STILL NOT change
    job_after_status = repo.get_job(sample_job.id)
    assert job_after_status is not None
    assert job_after_status.status == JobStatus.NEW.value
    assert job_after_status.user_status_at is None


# ---------------------------------------------------------------------------------------------
# Test 3: Application status is loaded from CandidateApplication
# ---------------------------------------------------------------------------------------------
def test_3_application_status_loaded_from_candidate_application(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    repo.apply_candidate_action(candidate_a.id, sample_job.id, "interviewing")

    client_a, _ = make_client_for_candidate(app_instance, repo, candidate_a.id)

    # Verify Applications board shows it in Interview column
    resp_board = client_a.get("/applications")
    assert resp_board.status_code == 200
    assert sample_job.id in resp_board.text

    # Verify Job detail view shows Interview status
    resp_job = client_a.get(f"/jobs/{sample_job.id}")
    assert resp_job.status_code == 200
    assert "Interview" in resp_job.text


# ---------------------------------------------------------------------------------------------
# Test 4: Current J4 scores are loaded from CandidateJobAssessment
# ---------------------------------------------------------------------------------------------
def test_4_current_j4_scores_loaded_from_candidate_job_assessment(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    # Save candidate profile first to satisfy foreign key
    prof = repo.save_profile_draft(
        candidate_id=candidate_a.id,
        resume_id=None,
        profile_data={"basics": {"name": "Alice"}, "skills": ["Python", "FastAPI"]},
    )
    # Save CandidateJobAssessment with score 94 for track ai_backend
    assessment = CandidateJobAssessment(
        id=uuid.uuid4().hex,
        candidate_id=candidate_a.id,
        job_id=sample_job.id,
        profile_id=prof.id,
        profile_version=prof.version,
        track="ai_backend",
        technical_score=94,
        evidence_coverage=0.9,
        eligibility_status="ELIGIBLE",
        assessment_json="{}",
        eligibility_json="{}",
        explanation="Strong fit in AI and Backend",
        created_at=to_iso(datetime.now(UTC)),
        updated_at=to_iso(datetime.now(UTC)),
    )
    with Session(repo.engine) as s:
        s.add(assessment)
        s.commit()

    client_a, _ = make_client_for_candidate(app_instance, repo, candidate_a.id)
    resp = client_a.get(f"/jobs/{sample_job.id}")
    assert resp.status_code == 200
    assert "94" in resp.text


# ---------------------------------------------------------------------------------------------
# Test 5: Legacy scores remain separately labeled and never become J4 scores
# ---------------------------------------------------------------------------------------------
def test_5_legacy_scores_remain_separately_labeled_never_become_j4_scores(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    # Save a historical assessment (legacy_matcher)
    repo.save_historical_assessment(
        candidate_id=candidate_a.id,
        job_id=sample_job.id,
        score=73,
        score_reason="Match from legacy keywords",
    )

    # Verify with view layer directly
    detail = views.job_detail(
        repo,
        sample_job.id,
        TZ,
        datetime.now(UTC),
        candidate_id=candidate_a.id,
    )
    assert detail is not None
    # Score must NOT be treated as J4 score (detail.card.score must be None)
    assert detail.card.score is None
    assert detail.card.historical_score == 73
    assert detail.card.is_legacy_score is True

    # Verify template rendering displays legacy_matcher badge
    client_a, _ = make_client_for_candidate(app_instance, repo, candidate_a.id)
    resp = client_a.get(f"/jobs/{sample_job.id}")
    assert resp.status_code == 200
    assert "legacy_matcher" in resp.text
    assert "73" in resp.text


# ---------------------------------------------------------------------------------------------
# Test 6: Applied dates use CandidateApplication.applied_at
# ---------------------------------------------------------------------------------------------
def test_6_applied_dates_use_candidate_application_applied_at(
    repo: Repo, candidate_a: Candidate, sample_job: Job, clock: FakeClock
) -> None:
    applied_time = clock()
    repo.apply_candidate_action(
        candidate_a.id, sample_job.id, "applied", applied_at=to_iso(applied_time)
    )

    # Shared job.user_status_at remains None
    job = repo.get_job(sample_job.id)
    assert job is not None
    assert job.user_status_at is None

    # Advance clock by 4 days
    clock.advance(4 * 86400)

    app = repo.get_candidate_application(candidate_a.id, sample_job.id)
    c = views.card(job, TZ, now=clock(), application=app)

    # 4 days between Oct 8 and Oct 12
    assert c.days == 4
    assert "8 Oct" in c.moved


# ---------------------------------------------------------------------------------------------
# Test 7: Saved and skipped applications do not receive fabricated applied dates
# ---------------------------------------------------------------------------------------------
def test_7_saved_and_skipped_applications_do_not_receive_fabricated_applied_dates(
    repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    # Save / shortlist action
    app_saved = repo.apply_candidate_action(candidate_a.id, sample_job.id, "shortlist")
    assert app_saved.status == CandidateApplicationStatus.SAVED.value
    assert app_saved.applied_at is None

    job = repo.get_job(sample_job.id)
    assert job is not None
    card_saved = views.card(job, TZ, now=datetime.now(UTC), application=app_saved)
    assert card_saved.status == "saved"
    # Applied date must not be fabricated
    assert app_saved.applied_at is None

    # Skip action on a second job
    job2, _ = repo.get_or_create_job("job_sample_2", "https://example.com/jobs/2")
    app_skipped = repo.apply_candidate_action(candidate_a.id, job2.id, "skip")
    assert app_skipped.status == CandidateApplicationStatus.SKIPPED.value
    assert app_skipped.applied_at is None


# ---------------------------------------------------------------------------------------------
# Test 8: Closed jobs remain visible in candidate's application board where appropriate
# ---------------------------------------------------------------------------------------------
def test_8_closed_jobs_remain_visible_on_candidate_board_with_closed_indicator(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    # Candidate A applies
    repo.apply_candidate_action(candidate_a.id, sample_job.id, "applied")

    # Employer closes the posting -> shared Job.status becomes hidden
    repo.set_job_status(sample_job.id, JobStatus.HIDDEN)

    client_a, _ = make_client_for_candidate(app_instance, repo, candidate_a.id)
    resp = client_a.get("/applications")
    assert resp.status_code == 200
    assert sample_job.id in resp.text
    # Closed badge must be rendered
    assert "Closed" in resp.text

    # Board view method verification
    columns = views.board(repo, make_settings(), datetime.now(UTC), candidate_id=candidate_a.id)
    applied_col = next(col for col in columns if col.key == "applied")
    assert len(applied_col.cards) == 1
    assert applied_col.cards[0].card.closed_by_employer is True


# ---------------------------------------------------------------------------------------------
# Test 9: CSRF and ownership protections remain effective
# ---------------------------------------------------------------------------------------------
def test_9_csrf_and_ownership_protections_remain_effective(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    client_a, token = make_client_for_candidate(app_instance, repo, candidate_a.id)

    # 1. Missing CSRF token
    resp_missing_csrf = client_a.post(
        f"/jobs/{sample_job.id}/act",
        data={"action": "applied"},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert resp_missing_csrf.status_code == 403

    # 2. Tampered CSRF token
    resp_bad_csrf = client_a.post(
        f"/jobs/{sample_job.id}/act",
        data={"action": "applied", "token": "invalid-token-12345"},
        headers={"Origin": "http://127.0.0.1"},
    )
    assert resp_bad_csrf.status_code == 403

    # 3. Disallowed origin header
    resp_bad_origin = client_a.post(
        f"/jobs/{sample_job.id}/act",
        data={"action": "applied", "token": token},
        headers={"Origin": "http://evil-attacker.com"},
    )
    assert resp_bad_origin.status_code == 403


# ---------------------------------------------------------------------------------------------
# Test 10: Existing dashboard filters, routes, and actions remain functional
# ---------------------------------------------------------------------------------------------
def test_10_existing_dashboard_filters_routes_and_actions_functional(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    client_a, token = make_client_for_candidate(app_instance, repo, candidate_a.id)

    # GET /applications
    resp_apps = client_a.get("/applications")
    assert resp_apps.status_code == 200

    # GET /jobs
    resp_jobs = client_a.get("/jobs?q=Backend")
    assert resp_jobs.status_code == 200
    assert sample_job.id in resp_jobs.text

    # GET /jobs/{job_id}
    resp_detail = client_a.get(f"/jobs/{sample_job.id}")
    assert resp_detail.status_code == 200
    assert "Backend Engineer" in resp_detail.text

    # POST /jobs/{job_id}/act with AJAX header
    resp_act_ajax = client_a.post(
        f"/jobs/{sample_job.id}/act",
        data={"action": "shortlist", "token": token},
        headers={"Origin": "http://127.0.0.1", "x-jobradar-token": token},
    )
    assert resp_act_ajax.status_code == 200
    json_data = resp_act_ajax.json()
    assert json_data["ok"] is True
    assert json_data["status"] == "saved"


# ---------------------------------------------------------------------------------------------
# Test 11: Missing assessments do not break dashboard rendering
# ---------------------------------------------------------------------------------------------
def test_11_missing_assessments_do_not_break_dashboard_rendering(
    app_instance: Any, repo: Repo, candidate_a: Candidate
) -> None:
    # Create job without any assessments or extra data
    job_empty, _ = repo.get_or_create_job("job_empty_assess", "https://example.com/empty")

    client_a, _ = make_client_for_candidate(app_instance, repo, candidate_a.id)

    # Job detail view renders without crash
    resp_detail = client_a.get(f"/jobs/{job_empty.id}")
    assert resp_detail.status_code == 200

    # Board renders without crash
    repo.apply_candidate_action(candidate_a.id, job_empty.id, "shortlist")
    resp_board = client_a.get("/applications")
    assert resp_board.status_code == 200
    assert job_empty.id in resp_board.text


# ---------------------------------------------------------------------------------------------
# Test 12: Candidate application history is preserved
# ---------------------------------------------------------------------------------------------
def test_12_candidate_application_history_preserved(
    app_instance: Any, repo: Repo, candidate_a: Candidate, sample_job: Job
) -> None:
    client_a, token = make_client_for_candidate(app_instance, repo, candidate_a.id)

    # Sequential actions
    client_a.post(
        f"/jobs/{sample_job.id}/act",
        data={"action": "shortlist", "token": token, "notes": "Interested"},
        headers={"Origin": "http://127.0.0.1"},
    )
    client_a.post(
        f"/jobs/{sample_job.id}/act",
        data={"action": "applied", "token": token, "notes": "Applied on workday"},
        headers={"Origin": "http://127.0.0.1"},
    )
    client_a.post(
        f"/jobs/{sample_job.id}/status",
        data={"status": "interviewing", "token": token, "notes": "Technical screen scheduled"},
        headers={"Origin": "http://127.0.0.1"},
    )

    # Verify history recorded in database (ordered newest first)
    hist = repo.list_candidate_application_history(candidate_a.id, sample_job.id)
    assert len(hist) == 3
    statuses = [h.status for h in hist]
    assert statuses == ["interviewing", "applied", "saved"]

    # Verify detail view shows history entries
    resp_detail = client_a.get(f"/jobs/{sample_job.id}")
    assert resp_detail.status_code == 200
    assert "Your status history" in resp_detail.text
    assert "Shortlisted" in resp_detail.text
    assert "Applied" in resp_detail.text
    assert "Interview" in resp_detail.text
