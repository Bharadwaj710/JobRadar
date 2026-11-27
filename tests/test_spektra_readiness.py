"""Spektra Systems submission readiness verification test suite.

Verifies end-to-end demo journey readiness and candidate-scoped CLI/pipeline behavior:
1. Candidate-aware applications by source (candidate_application_history).
2. Candidate isolation in sources reporting.
3. Candidate-aware follow-ups query and status calculation.
4. Background matcher candidate assessment persistence for confirmed profiles.
5. Protection of draft/unconfirmed profiles during background matching.
6. Pipeline sink factory candidate binding in app.py.
7. Complete end-to-end 8-step demo journey validation.
8. Shared inventory immutability (Job.status remains untouched).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import text

from jobradar import app as application
from jobradar.by_source import by_source
from jobradar.clock import to_iso
from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import (
    Job,
    JobStatus,
    Platform,
)
from jobradar.db.repo import Repo
from jobradar.pipeline.schemas import JobPosting
from jobradar.queue.tasks import TaskQueue
from jobradar.services.matching_service import CandidateMatchingService
from jobradar.sinks.followups import FollowUpReminder
from tests.conftest import FakeClock


def make_test_settings(owner_candidate_id: str | None = None, **notify: Any) -> Settings:
    notify.setdefault("dry_run", True)
    config_dict: dict[str, Any] = {
        "sources": {"telegram": {"enabled": False}},
        "notify": notify,
        "notion": {"enabled": False},
        "web": {"enabled": True, "url": "http://127.0.0.1:8080"},
        "matching": {"enabled": False},
    }
    if owner_candidate_id:
        config_dict["owner_candidate_id"] = owner_candidate_id

    return Settings(
        config=AppConfig.model_validate(config_dict),
        secrets=Secrets.model_validate(
            {"EMAIL_ADDRESS": "test@example.com", "NOTIFY_TO": "notify@example.com"}
        ),
    )


def sample_profile_data(
    name: str = "Alice Candidate", email: str = "alice@example.com"
) -> dict[str, Any]:
    return {
        "basics": {
            "name": name,
            "email": email,
            "phone": "+91 9876543210",
            "location": "Bengaluru, India",
            "github": f"https://github.com/{name.split()[0].lower()}",
            "linkedin": f"https://linkedin.com/in/{name.split()[0].lower()}",
        },
        "education": [
            {
                "degree": "B.Tech",
                "branch": "Electrical Engineering",
                "institution": "IIT Bombay",
                "graduation_year": 2025,
                "cgpa": "8.7",
            }
        ],
        "skills": {
            "languages": ["Python", "Go", "SQL"],
            "frameworks": ["FastAPI", "Django"],
            "developer_tools": ["Docker", "Git", "PostgreSQL"],
        },
        "evidence": {
            "skills": {
                "python": {"confidence": 0.95, "sources": ["projects", "experience"]},
                "fastapi": {"confidence": 0.90, "sources": ["projects"]},
                "sql": {"confidence": 0.85, "sources": ["education"]},
            },
            "metadata": {"confidence": 0.92, "warnings": []},
        },
    }


def add_job_with_source(
    repo: Repo,
    job_id: str,
    platform: str,
    source_name: str,
    role: str = "Backend Engineer",
    company: str = "Acme Corp",
    apply_url: str | None = None,
) -> Job:
    url = apply_url or f"https://example.com/jobs/{job_id}"
    reqs: dict[str, Any] = {
        "required_skills": [
            {
                "skills": ["python", "fastapi"],
                "relation": "all",
                "evidence": {
                    "sentence": "Python and FastAPI required",
                    "section": "Requirements",
                    "source": "page",
                },
            }
        ],
        "preferred_skills": [],
        "experience": [],
        "experience_status": "known",
    }
    posting_data: dict[str, Any] = {
        "company": company,
        "role": role,
        "title": role,
        "employment_type": "full_time",
        "batch_years": [2025, 2026],
        "degrees": ["B.Tech", "BE"],
        "locations": ["Bengaluru, India"],
        "work_mode": "remote",
        "role_tracks": [
            {
                "track": "sde",
                "strength": "strong",
                "evidence": [{"sentence": "SDE role", "section": "Role", "source": "page"}],
            },
            {
                "track": "ai_backend",
                "strength": "strong",
                "evidence": [{"sentence": "Backend role", "section": "Role", "source": "page"}],
            },
        ],
        "requirements": reqs,
        "skills_required": ["python", "fastapi"],
        "skills_preferred": [],
        "apply_url": url,
    }
    posting = JobPosting.model_validate(posting_data)
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO job (id, canonical_url, role, company, status, data_json, created_at, updated_at) "
                "VALUES (:id, :url, :role, :company, :status, :data, :now, :now)"
            ),
            {
                "id": job_id,
                "url": url,
                "role": role,
                "company": company,
                "status": JobStatus.NEW.value,
                "data": posting.model_dump_json(),
                "now": datetime.now(UTC).isoformat(),
            },
        )
    source = repo.upsert_source(Platform(platform), f"chat_{job_id}", source_name)
    assert source.id is not None
    raw, _ = repo.save_raw_message(
        source.id, f"msg_{job_id}", "2026-10-08T08:00:00.000000Z", f"Hiring {role} at {company}"
    )
    repo.add_job_source(job_id, raw)
    job = repo.get_job(job_id)
    assert job is not None
    return job


def test_candidate_aware_applications_by_source(repo: Repo) -> None:
    """Verify applications_by_source accurately reads candidate-specific history and enforces isolation."""
    # Setup Candidate Alice and Candidate Bob
    alice = repo.create_candidate("Alice", "alice@example.com")
    bob = repo.create_candidate("Bob", "bob@example.com")

    # Ingest 2 jobs from different sources
    job1_id = "a001" + "1" * 28
    job2_id = "a002" + "2" * 28
    add_job_with_source(
        repo, job1_id, "board", "Greenhouse · Stripe", role="Backend Eng", company="Stripe"
    )
    add_job_with_source(
        repo, job2_id, "board", "Lever · Databricks", role="Platform Eng", company="Databricks"
    )

    # Alice applied to Job 1, moved to Interviewing
    repo.save_candidate_application(alice.id, job1_id, "applied", notes="Applied on website")
    repo.save_candidate_application(
        alice.id, job1_id, "interviewing", notes="Recruiter screen booked"
    )

    # Bob applied to Job 2, moved to Rejected
    repo.save_candidate_application(bob.id, job2_id, "applied", notes="Referral apply")
    repo.save_candidate_application(bob.id, job2_id, "rejected", notes="Position closed")

    # 1. Query Alice's sources
    alice_rows = repo.applications_by_source(undo_minutes=10, candidate_id=alice.id)
    assert len(alice_rows) == 1
    _platform, name, jid, status, next_round, is_first = alice_rows[0]
    assert name == "Greenhouse · Stripe"
    assert jid == job1_id
    assert status == "interviewing"
    assert next_round is True
    assert is_first is True

    alice_report = by_source(repo, candidate_id=alice.id)
    assert len(alice_report.rows) == 1
    assert alice_report.applications == 1
    assert alice_report.next_round == 1
    assert "1 reached the next round" in alice_report.summary

    # 2. Query Bob's sources
    bob_rows = repo.applications_by_source(undo_minutes=10, candidate_id=bob.id)
    assert len(bob_rows) == 1
    _platform_b, name_b, jid_b, status_b, next_round_b, _ = bob_rows[0]
    assert name_b == "Lever · Databricks"
    assert jid_b == job2_id
    assert status_b == "rejected"
    assert next_round_b is False

    bob_report = by_source(repo, candidate_id=bob.id)
    assert len(bob_report.rows) == 1
    assert bob_report.applications == 1
    assert bob_report.next_round == 0
    assert "None has reached an interview yet" in bob_report.summary

    # 3. Isolation: Bob's data does not show for Alice, and vice versa
    assert not any(r[2] == job2_id for r in alice_rows)
    assert not any(r[2] == job1_id for r in bob_rows)

    # 4. Legacy query with candidate_id=None falls back to status_change table (empty here)
    legacy_rows = repo.applications_by_source(undo_minutes=10, candidate_id=None)
    assert legacy_rows == []


def test_candidate_followups_cli_logic(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    """Verify candidate-aware followups correctly resolves candidate applications without mutating Job.status."""
    cand = repo.create_candidate("Alice", "alice@example.com")
    draft_p = repo.save_profile_draft(cand.id, sample_profile_data("Alice", "alice@example.com"))
    repo.confirm_profile(draft_p.id, draft_p.profile_data)
    job_id = "a003" + "3" * 28
    job = add_job_with_source(
        repo, job_id, "board", "Greenhouse · Stripe", role="SWE", company="Stripe"
    )

    # Candidate applies
    applied_time = clock() - timedelta(days=12)
    repo.save_candidate_application(
        cand.id, job_id, "applied", applied_at=to_iso(applied_time), notes="Direct apply"
    )

    settings = make_test_settings(owner_candidate_id=cand.id, followup_after_days=10)
    reminder = FollowUpReminder(settings, repo, queue, candidate_id=cand.id, clock=clock)

    # Verify query for candidate applied jobs
    pairs = repo.list_candidate_applications_with_jobs(cand.id, statuses=["applied"])
    assert len(pairs) == 1
    app_rec, job_rec = pairs[0]
    assert app_rec.status == "applied"
    assert job_rec.id == job_id

    # Verify status calculation
    _applied_dt, since_dt, sent_count = reminder.status(job, candidate_id=cand.id)
    assert sent_count == 0
    assert (clock() - since_dt).days >= 12

    # Verify due detection
    due_list = reminder.due()
    assert len(due_list) == 1
    assert due_list[0].job.id == job_id
    assert due_list[0].quiet_days >= 12

    # Verify shared job record is still in NEW status
    fresh_job = repo.get_job(job_id)
    assert fresh_job is not None
    assert fresh_job.status == JobStatus.NEW.value
    assert fresh_job.user_status_at is None


def test_matcher_background_candidate_assessment(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    """Verify Matcher automatically computes candidate assessments for active confirmed profiles."""
    from jobradar.pipeline.match import Matcher

    # Candidate 1: Confirmed profile
    cand_alice = repo.create_candidate("Alice Confirmed", "alice.conf@example.com")
    draft_p = repo.save_profile_draft(
        cand_alice.id, sample_profile_data("Alice", "alice.conf@example.com")
    )
    conf_p = repo.confirm_profile(draft_p.id, draft_p.profile_data)
    assert conf_p.review_status == "confirmed"

    # Candidate 2: Draft profile (unconfirmed)
    cand_bob = repo.create_candidate("Bob Draft", "bob.draft@example.com")
    draft_bob = repo.save_profile_draft(
        cand_bob.id, sample_profile_data("Bob", "bob.draft@example.com")
    )
    assert draft_bob.review_status == "draft"

    # Ingest a new job
    job_id = "a004" + "4" * 28
    job = add_job_with_source(
        repo,
        job_id,
        "board",
        "Greenhouse · Acme",
        role="Python Backend Developer",
        company="Acme Corp",
    )

    settings = make_test_settings()
    matcher = Matcher(
        repo=repo,
        queue=queue,
        providers=[],
        profile=conf_p.profile_data,
        version="v1",
        matching=settings.config.matching,
        filters=settings.config.filters,
        clock=clock,
        candidate_independent=True,
    )

    posting = JobPosting.model_validate_json(job.data_json or "{}")
    # Trigger background candidate matching
    matcher._persist_candidate_matches(job.id, posting)

    # 1. Alice (confirmed) must have assessments generated
    alice_assessments = repo.list_candidate_assessments(cand_alice.id, job_id=job.id)
    assert len(alice_assessments) == 4
    sde_ass = next((a for a in alice_assessments if a.track in ("sde", "ai_backend")), None)
    assert sde_ass is not None
    assert sde_ass.technical_score is not None
    assert sde_ass.technical_score > 0
    assert sde_ass.explanation != ""

    # 2. Bob (draft) must NOT have assessments
    bob_assessments = repo.list_candidate_assessments(cand_bob.id, job_id=job.id)
    assert len(bob_assessments) == 0


def test_app_pipeline_candidate_sink_wiring(repo: Repo, queue: TaskQueue) -> None:
    """Verify app.py Pipeline correctly wires resolved owner candidate into all sinks."""
    cand = repo.create_candidate("Owner Alice", "owner.alice@example.com")
    draft_p = repo.save_profile_draft(
        cand.id, sample_profile_data("Owner Alice", "owner.alice@example.com")
    )
    repo.confirm_profile(draft_p.id, draft_p.profile_data)

    settings = make_test_settings(owner_candidate_id=cand.id)
    pipeline = application.Pipeline(settings, repo, queue, sink=None)

    assert pipeline.owner_candidate is not None
    assert pipeline.owner_candidate.id == cand.id

    # Verify sinks carry candidate context
    if pipeline.digest is not None:
        assert pipeline.digest.candidate_id == cand.id
    if pipeline.reminders is not None:
        assert pipeline.reminders.candidate_id == cand.id
    if pipeline.followups is not None:
        assert pipeline.followups.candidate_id == cand.id


def test_full_demo_journey(repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
    """Validate all 8 steps of the end-to-end demo journey in sequence."""
    # Step 1: Start JobRadar / Environment
    _ = make_test_settings()

    # Step 2: Discover and ingest jobs from configured ATS sources
    job1_id = "a005" + "5" * 28
    job2_id = "a006" + "6" * 28
    job1 = add_job_with_source(
        repo, job1_id, "board", "Greenhouse · Stripe", role="Backend Engineer", company="Stripe"
    )
    job2 = add_job_with_source(
        repo,
        job2_id,
        "board",
        "Lever · Razorpay",
        role="Software Engineer - Core Backend",
        company="Razorpay",
    )
    assert repo.get_job(job1_id) is not None
    assert repo.get_job(job2_id) is not None
    assert job1.status == "new"
    assert job2.status == "new"

    # Step 3: Onboard a candidate and upload a resume
    cand = repo.create_candidate("Candidate Demo", "demo.cand@example.com")
    draft_profile = repo.save_profile_draft(
        cand.id, sample_profile_data("Candidate Demo", "demo.cand@example.com")
    )
    assert draft_profile.review_status == "draft"

    # Step 4: Confirm the extracted candidate profile
    confirmed_profile = repo.confirm_profile(draft_profile.id, draft_profile.profile_data)
    assert confirmed_profile.review_status == "confirmed"
    assert confirmed_profile.version == 1

    # Step 5: Generate candidate-specific assessments
    matcher_service = CandidateMatchingService(repo)
    assessments = matcher_service.assess_candidate_jobs(
        candidate_id=cand.id,
        jobs=[job1, job2],
        profile_version=confirmed_profile.version,
    )
    assert len(assessments) == 8  # 2 jobs * 4 tracks = 8 assessments

    # Step 6: Display personalized recommendations and explanations
    recs = repo.get_candidate_recommendations(
        cand.id, profile_version=confirmed_profile.version, track="sde"
    )
    assert len(recs) == 2
    top_rec = recs[0]
    assert top_rec["job_id"] in (job1_id, job2_id)
    assert top_rec["technical_score"] is not None
    assert top_rec["technical_score"] > 0
    assert top_rec["explanation"] != ""

    # Step 7: Save a job and move it through application statuses
    # 7a. Save (Shortlist)
    app1 = repo.save_candidate_application(cand.id, job1_id, "saved", notes="Target for this week")
    assert app1.status == "saved"
    # 7b. Move to Applied
    app1_applied = repo.save_candidate_application(
        cand.id, job1_id, "applied", notes="Applied on careers portal"
    )
    assert app1_applied.status == "applied"
    # 7c. Move to Interviewing
    app1_interview = repo.save_candidate_application(
        cand.id, job1_id, "interviewing", notes="Technical round scheduled"
    )
    assert app1_interview.status == "interviewing"

    # Step 8: Show application history and candidate-scoped notifications / sources
    # 8a. History log audit trail
    history = repo.list_candidate_application_history(cand.id, job_id=job1_id)
    assert len(history) == 3
    history_statuses = [h.status for h in history]
    assert history_statuses == ["interviewing", "applied", "saved"]

    # 8b. Applications by source reporting
    sources_report = by_source(repo, candidate_id=cand.id)
    assert len(sources_report.rows) == 1
    assert sources_report.rows[0].name == "Greenhouse · Stripe"
    assert sources_report.applications == 1
    assert sources_report.next_round == 1

    # 8c. Decoupling verification: shared Job records remain pristine
    j1_final = repo.get_job(job1_id)
    j2_final = repo.get_job(job2_id)
    assert j1_final is not None and j2_final is not None
    assert j1_final.status == JobStatus.NEW.value
    assert j2_final.status == JobStatus.NEW.value
    assert j1_final.user_status_at is None
    assert j2_final.user_status_at is None
