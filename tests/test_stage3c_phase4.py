"""Stage 3C Phase 4 verification test suite.

Validates candidate isolation, token security, and lifecycle decoupling in notification sinks:
1. Candidate-specific alerts and digests.
2. Candidate-specific application reminders.
3. Correct applied timestamp calculations (applied_at).
4. Missing or unconfirmed candidate profiles handled safely.
5. Cross-candidate notification isolation (no data leakage).
6. Legacy score versus J4 score separation.
7. Valid, expired, malformed, and tampered action tokens.
8. Token replay and candidate-ID substitution attacks.
9. Candidate actions never mutating shared Job.status or Job.user_status_at.
10. Company replies matching candidate applied/interviewing jobs.
11. Disabled Notion integration safety (notion.enabled: false).
12. Existing notification regression compatibility.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from sqlmodel import Session

from jobradar.clock import to_iso
from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import (
    Candidate,
    CandidateJobAssessment,
    CandidateProfile,
    Job,
    JobStatus,
)
from jobradar.db.repo import Repo
from jobradar.pipeline.task_types import SEND_ALERTS
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.company_replies import CompanyReplies
from jobradar.sinks.digest import Digest, gather
from jobradar.sinks.email_actions import Actions, Incoming, ReplyReader
from jobradar.sinks.email_notify import AlertSender, build_email
from jobradar.sinks.email_template import job_card
from jobradar.sinks.followups import FollowUpReminder
from jobradar.sinks.reminders import ClosingReminder
from tests.conftest import FakeClock


def make_settings(**notify: Any) -> Settings:
    notify.setdefault("dry_run", False)
    return Settings(
        config=AppConfig.model_validate(
            {
                "sources": {"telegram": {"enabled": False}},
                "notify": notify,
                "notion": {"enabled": False},
                "web": {"enabled": True, "url": "http://127.0.0.1:8080"},
            }
        ),
        secrets=Secrets.model_validate(
            {
                "email_address": "owner@example.com",
                "email_app_password": "owner-secret-pass",
                "notify_to": "owner-phone@example.com",
            }
        ),
    )


def create_test_candidate(
    repo: Repo,
    candidate_id: str,
    name: str,
    email: str,
    confirmed: bool = True,
) -> tuple[Candidate, CandidateProfile]:
    cand = repo.create_candidate(id=candidate_id, name=name, email=email)
    p_data = {
        "basics": {"name": name, "email": email, "phone": "+91-9876543210"},
        "skills": ["Python", "FastAPI", "SQL", "PostgreSQL"],
    }
    draft = repo.save_profile_draft(candidate_id, profile_data=p_data)
    profile = repo.confirm_profile(draft.id) if confirmed else draft
    return cand, profile


def add_shared_job(
    repo: Repo,
    job_id: str,
    role: str = "Backend Engineer",
    company: str = "Acme Corp",
    deadline: str | None = None,
    job_score: int | None = 70,
    job_status: JobStatus = JobStatus.NEW,
) -> Job:
    repo.get_or_create_job(job_id, f"https://jobs.example.com/{job_id}")
    repo.save_extraction(
        job_id,
        json.dumps({"role": role, "company": company, "locations": ["Bengaluru"]}),
        company,
        role,
        deadline,
        "rules-v1",
        JobStatus.EXTRACTED,
    )
    repo.set_job_status(job_id, job_status)
    if job_score is not None:
        repo.set_match_score(job_id, job_score, f"Good fit · {job_score} · eligible ✓")
    return repo.get_job(job_id)  # type: ignore[return-value]


def save_j4_assessment(
    repo: Repo,
    candidate_id: str,
    job_id: str,
    profile_id: str,
    profile_version: int,
    track: str = "ai_backend",
    technical_score: int = 90,
    explanation: str = "Strong match for AI Backend",
) -> CandidateJobAssessment:
    now = to_iso(datetime.now(UTC))
    with Session(repo.engine) as s:
        ass = CandidateJobAssessment(
            id=uuid.uuid4().hex,
            candidate_id=candidate_id,
            job_id=job_id,
            profile_id=profile_id,
            profile_version=profile_version,
            track=track,
            technical_score=technical_score,
            evidence_coverage=0.9,
            eligibility_status="ELIGIBLE",
            eligibility_reasons_json="[]",
            matched_skills_json=json.dumps(["Python", "SQL"]),
            missing_required_skills_json="[]",
            missing_preferred_skills_json="[]",
            explanation=explanation,
            assessment_json=json.dumps({"track": track, "score": technical_score}),
            eligibility_json=json.dumps({"status": "ELIGIBLE"}),
            created_at=now,
            updated_at=now,
        )
        s.add(ass)
        s.commit()
        s.refresh(ass)
        return ass


class Outbox:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def __call__(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


def mail_incoming(
    subject: str,
    sender: str = "owner@example.com",
    auth: str = "",
    sent_dt: datetime | None = None,
    n: int = 1,
) -> Incoming:
    dt = sent_dt or datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    return Incoming(str(n), f"<m{n}@test>", sender, subject, auth, dt)


def subject_from_link(link: str) -> str:
    return unquote(parse_qs(urlsplit(link).query)["subject"][0])


# ==============================================================================
# TESTS
# ==============================================================================


@pytest.mark.asyncio
async def test_candidate_specific_alerts_and_j4_scores(repo: Repo, queue: TaskQueue) -> None:
    """1. Candidate-specific alerts, 5. Isolation, 6. J4 scores."""
    cand_a, prof_a = create_test_candidate(repo, "cand-alpha", "Alice", "alice@example.com")
    cand_b, prof_b = create_test_candidate(repo, "cand-beta", "Bob", "bob@example.com")
    job_a = add_shared_job(
        repo, "a001" + "a" * 28, role="AI Engineer", company="OmniAI", job_score=50
    )
    job_b = add_shared_job(
        repo, "b001" + "b" * 28, role="Data Engineer", company="OmniData", job_score=50
    )

    save_j4_assessment(
        repo,
        cand_a.id,
        job_a.id,
        prof_a.id,
        prof_a.version,
        technical_score=95,
        explanation="Alice 95% fit",
    )
    save_j4_assessment(
        repo,
        cand_b.id,
        job_b.id,
        prof_b.id,
        prof_b.version,
        technical_score=62,
        explanation="Bob 62% fit",
    )

    cfg = make_settings()
    outbox_a = Outbox()
    outbox_b = Outbox()

    clock = FakeClock()
    sender_a = AlertSender(cfg, repo, queue, candidate_id=cand_a.id, clock=clock, send=outbox_a)
    sender_b = AlertSender(cfg, repo, queue, candidate_id=cand_b.id, clock=clock, send=outbox_b)

    repo.queue_alert(job_a.id)
    await sender_a.send(queue.enqueue(SEND_ALERTS, "batch_a"))
    assert len(outbox_a.sent) == 1
    msg_a = outbox_a.sent[0]
    assert "[JobRadar 95]" in str(msg_a["Subject"])
    body_a = str(msg_a.get_body(("plain",)).get_content())
    assert "95" in body_a
    assert "Alice 95% fit" in body_a
    assert cand_a.id in body_a

    repo.queue_alert(job_b.id)
    await sender_b.send(queue.enqueue(SEND_ALERTS, "batch_b"))
    assert len(outbox_b.sent) == 1
    msg_b = outbox_b.sent[0]
    assert "[JobRadar 62]" in str(msg_b["Subject"])
    body_b = str(msg_b.get_body(("plain",)).get_content())
    assert "62" in body_b
    assert "Bob 62% fit" in body_b
    assert cand_b.id in body_b


def test_legacy_score_versus_j4_score_separation(repo: Repo) -> None:
    """6. Legacy score versus J4 score separation."""
    cand_a, prof_a = create_test_candidate(repo, "cand-j4", "J4 User", "j4@example.com")
    cand_b, _ = create_test_candidate(repo, "cand-leg", "Legacy User", "leg@example.com")
    job = add_shared_job(
        repo, "a002" + "b" * 28, role="Platform SDE", company="InfraTech", job_score=70
    )

    save_j4_assessment(repo, cand_a.id, job.id, prof_a.id, prof_a.version, technical_score=88)
    repo.save_historical_assessment(cand_b.id, job.id, score=74, score_reason="Historical 74 fit")

    card_j4_html, card_j4_text = job_card(job, score=88, score_override=True, is_legacy=False)
    assert "88" in card_j4_html
    assert "legacy_matcher" not in card_j4_html
    assert "88  Platform SDE" in card_j4_text
    assert "legacy_matcher" not in card_j4_text

    card_leg_html, card_leg_text = job_card(job, score=74, score_override=True, is_legacy=True)
    assert "74" in card_leg_html
    assert "legacy_matcher" in card_leg_html
    assert "74 (legacy_matcher)" in card_leg_text


@pytest.mark.asyncio
async def test_missing_or_unconfirmed_candidate_profile_safety(
    repo: Repo, queue: TaskQueue
) -> None:
    """4. Missing or unconfirmed candidate profiles handled safely."""
    cand, _unconfirmed_prof = create_test_candidate(
        repo, "cand-unconf", "Unconfirmed", "unconf@example.com", confirmed=False
    )
    job = add_shared_job(repo, "a003" + "c" * 28)

    cfg = make_settings()
    outbox = Outbox()
    clock = FakeClock()

    sender = AlertSender(cfg, repo, queue, candidate_id=cand.id, clock=clock, send=outbox)
    repo.queue_alert(job.id)
    await sender.send(queue.enqueue(SEND_ALERTS, "batch_unconf"))
    assert len(outbox.sent) == 0

    digest = Digest(cfg, repo, queue, candidate_id=cand.id, clock=clock)
    digest_outcome = await digest.send_now()
    assert "candidate profile not confirmed" in digest_outcome

    closing = ClosingReminder(cfg, repo, queue, candidate_id=cand.id, clock=clock)
    closing_outcome = await closing.check_now()
    assert "candidate profile not confirmed" in closing_outcome

    followup = FollowUpReminder(cfg, repo, queue, candidate_id=cand.id, clock=clock)
    assert followup.due() == []

    non_existent_sender = AlertSender(
        cfg, repo, queue, candidate_id="cand-nonexistent", clock=clock, send=outbox
    )
    repo.queue_alert(job.id)
    await non_existent_sender.send(queue.enqueue(SEND_ALERTS, "batch_nonexistent"))
    assert len(outbox.sent) == 0


def test_candidate_specific_digest_pipeline_and_isolation(repo: Repo) -> None:
    """1. Candidate-specific digests, 5. Isolation."""
    cand_a, prof_a = create_test_candidate(repo, "cand-pipe-a", "Pipe A", "pipea@example.com")
    cand_b, prof_b = create_test_candidate(repo, "cand-pipe-b", "Pipe B", "pipeb@example.com")

    job1 = add_shared_job(repo, "a004" + "d" * 28, role="SDE 1")
    job2 = add_shared_job(repo, "a005" + "e" * 28, role="SDE 2")

    repo.save_candidate_application(cand_a.id, job1.id, status="applied")
    repo.save_candidate_application(cand_a.id, job2.id, status="interviewing")

    repo.save_candidate_application(cand_b.id, job1.id, status="saved")
    repo.save_candidate_application(cand_b.id, job2.id, status="rejected")

    cfg = make_settings()
    now = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
    since = now - timedelta(hours=12)

    data_a = gather(repo, cfg, since, now, candidate_id=cand_a.id, profile_version=prof_a.version)
    assert data_a.pipeline.get("applied") == 1
    assert data_a.pipeline.get("interviewing") == 1
    assert data_a.pipeline.get("saved") == 0

    data_b = gather(repo, cfg, since, now, candidate_id=cand_b.id, profile_version=prof_b.version)
    assert data_b.pipeline.get("applied") == 0
    assert data_b.pipeline.get("interviewing") == 0
    assert data_b.pipeline.get("saved") == 1
    assert data_b.pipeline.get("rejected") == 1


def test_candidate_specific_followup_reminders_and_applied_at(repo: Repo, queue: TaskQueue) -> None:
    """2. Application reminders, 3. Correct applied_at calculation."""
    cand_a, _ = create_test_candidate(repo, "cand-fup-a", "Fup Alice", "fupa@example.com")
    cand_b, _ = create_test_candidate(repo, "cand-fup-b", "Fup Bob", "fupb@example.com")

    job = add_shared_job(repo, "a006" + "f" * 28, role="Backend Lead", company="TechCo")
    assert job.status == JobStatus.NEW.value

    clock = FakeClock()
    clock.now = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    applied_12_days_ago = to_iso(clock() - timedelta(days=12))
    applied_2_days_ago = to_iso(clock() - timedelta(days=2))

    repo.save_candidate_application(
        cand_a.id, job.id, status="applied", applied_at=applied_12_days_ago
    )
    repo.save_candidate_application(
        cand_b.id, job.id, status="applied", applied_at=applied_2_days_ago
    )

    cfg = make_settings(followup_after_days=10)
    reminders_a = FollowUpReminder(cfg, repo, queue, candidate_id=cand_a.id, clock=clock)
    reminders_b = FollowUpReminder(cfg, repo, queue, candidate_id=cand_b.id, clock=clock)

    due_a = reminders_a.due()
    assert len(due_a) == 1
    assert due_a[0].job.id == job.id
    assert due_a[0].quiet_days == 12
    assert due_a[0].nth == 1

    due_b = reminders_b.due()
    assert len(due_b) == 0

    draft_a = reminders_a.draft_for(job)
    assert draft_a is not None
    assert "Backend Lead" in draft_a.subject


@pytest.mark.asyncio
async def test_candidate_closing_reminders(repo: Repo, queue: TaskQueue) -> None:
    """2. Candidate application reminders for closing jobs."""
    cand_a, _ = create_test_candidate(repo, "cand-close-a", "Close Alice", "closea@example.com")
    cand_b, _ = create_test_candidate(repo, "cand-close-b", "Close Bob", "closeb@example.com")

    clock = FakeClock()
    clock.now = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    tomorrow = (clock().date() + timedelta(days=1)).isoformat()
    job = add_shared_job(
        repo, "a007" + "1" * 28, role="DevOps", company="CloudWorks", deadline=tomorrow
    )

    repo.save_candidate_application(cand_a.id, job.id, status="saved")

    cfg = make_settings(dry_run=True)
    reminder_a = ClosingReminder(cfg, repo, queue, candidate_id=cand_a.id, clock=clock)
    reminder_b = ClosingReminder(cfg, repo, queue, candidate_id=cand_b.id, clock=clock)

    out_b = await reminder_b.check_now()
    assert out_b == "nothing closing soon"

    out_a = await reminder_a.check_now()
    assert "dry run:" in out_a or "sent:" in out_a
    assert repo.get_setting(f"closing_reminder:{cand_a.id}:{job.id}") is not None


def test_action_token_lifecycle_valid_expired_malformed_tampered(repo: Repo) -> None:
    """7. Valid, expired, malformed, and tampered action tokens."""
    cand, _ = create_test_candidate(repo, "cand-tok", "Tok User", "tok@example.com")
    job = add_shared_job(repo, "a008" + "2" * 28)

    clock = FakeClock()
    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    actions = Actions(
        "my-action-secret-key-1234567890", "owner@example.com", candidate_id=cand.id, clock=clock
    )

    subj = actions.subject("applied", job)
    assert f".{cand.id}." in subj

    parsed = actions.parse(subj)
    assert parsed is not None
    assert parsed.action == "applied"
    assert parsed.candidate_id == cand.id
    assert parsed.prefix == job.id[:12]
    assert parsed.is_legacy is False

    clock.advance(8 * 86400)
    assert actions.parse(subj) is None

    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    tampered_subj = subj[:-4] + "dead"
    assert actions.parse(tampered_subj) is None

    tampered_action = subj.replace("applied", "skip", 1)
    assert actions.parse(tampered_action) is None

    assert actions.parse("Random email subject from recruiter") is None
    assert actions.parse("") is None


def test_token_replay_and_candidate_substitution_attack(repo: Repo, queue: TaskQueue) -> None:
    """8. Token replay and candidate-ID substitution attacks."""
    cand_a, _ = create_test_candidate(repo, "cand-sub-a", "Alice Sub", "alice.sub@example.com")
    cand_b, _ = create_test_candidate(repo, "cand-sub-b", "Bob Sub", "bob.sub@example.com")
    job = add_shared_job(repo, "a009" + "3" * 28)

    clock = FakeClock()
    clock.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    actions_a = Actions(
        "sec-key-32-chars-long-for-testing",
        "owner@example.com",
        candidate_id=cand_a.id,
        clock=clock,
    )
    cfg = make_settings()
    reader = ReplyReader(cfg, repo, queue, actions_a, clock=clock)

    subj_a = actions_a.subject("applied", job)
    msg1 = mail_incoming(subj_a)
    outcome1 = reader.apply(msg1)
    assert outcome1 is not None
    assert "applied:" in outcome1

    app_a = repo.get_candidate_application(cand_a.id, job.id)
    assert app_a is not None
    assert app_a.status == "applied"

    outcome2 = reader.apply(msg1)
    assert outcome2 == "applied: already used"

    substituted_subj = subj_a.replace(cand_a.id, cand_b.id)
    msg_sub = mail_incoming(substituted_subj)
    outcome_sub = reader.apply(msg_sub)
    assert outcome_sub is None
    assert repo.get_candidate_application(cand_b.id, job.id) is None


def test_candidate_actions_never_mutate_shared_job_inventory(repo: Repo, queue: TaskQueue) -> None:
    """9. Candidate actions never mutating shared Job.status or Job.user_status_at."""
    cand, _ = create_test_candidate(repo, "cand-immut", "Immut User", "immut@example.com")
    job = add_shared_job(repo, "a010" + "4" * 28, job_status=JobStatus.NEW)
    assert job.status == JobStatus.NEW.value
    assert job.user_status_at is None

    clock = FakeClock()
    actions = Actions(
        "sec-key-32-chars-long-for-testing", "owner@example.com", candidate_id=cand.id, clock=clock
    )
    cfg = make_settings()
    reader = ReplyReader(cfg, repo, queue, actions, clock=clock)

    subj_applied = actions.subject("applied", job)
    reader.apply(mail_incoming(subj_applied, n=1))

    job_after_applied = repo.get_job(job.id)
    assert job_after_applied is not None
    assert job_after_applied.status == JobStatus.NEW.value
    assert job_after_applied.user_status_at is None

    cand_app = repo.get_candidate_application(cand.id, job.id)
    assert cand_app is not None
    assert cand_app.status == "applied"

    subj_interview = actions.subject("interview", job)
    reader.apply(mail_incoming(subj_interview, n=2))

    job_after_interview = repo.get_job(job.id)
    assert job_after_interview is not None
    assert job_after_interview.status == JobStatus.NEW.value
    assert job_after_interview.user_status_at is None

    cand_app_2 = repo.get_candidate_application(cand.id, job.id)
    assert cand_app_2 is not None
    assert cand_app_2.status == "interviewing"


def test_company_replies_candidate_matching_and_isolation(repo: Repo) -> None:
    """10. Company replies matching candidate applied/interviewing jobs, 5. Isolation."""
    cand_a, _ = create_test_candidate(repo, "cand-rep-a", "Rep Alice", "repa@example.com")
    cand_b, _ = create_test_candidate(repo, "cand-rep-b", "Rep Bob", "repb@example.com")

    job_acme = add_shared_job(
        repo, "a011" + "5" * 28, company="Acme Technologies", role="Backend Dev"
    )
    job_beta = add_shared_job(repo, "a012" + "6" * 28, company="Beta Corp", role="FullStack Dev")

    repo.save_candidate_application(cand_a.id, job_acme.id, status="applied")
    repo.save_candidate_application(cand_b.id, job_beta.id, status="applied")

    actions_a = Actions(
        "sec-key-32-chars-long-for-testing", "owner@example.com", candidate_id=cand_a.id
    )
    cfg = make_settings()

    scanner_a = CompanyReplies(cfg, repo, actions_a, candidate_id=cand_a.id)
    scanner_b = CompanyReplies(cfg, repo, actions_a, candidate_id=cand_b.id)
    assert scanner_a.candidate_id == cand_a.id
    assert scanner_b.candidate_id == cand_b.id

    pairs_a = repo.list_candidate_applications_with_jobs(
        cand_a.id, statuses=["applied", "interviewing"]
    )
    jobs_a = [j for _, j in pairs_a]
    assert len(jobs_a) == 1
    assert jobs_a[0].id == job_acme.id

    pairs_b = repo.list_candidate_applications_with_jobs(
        cand_b.id, statuses=["applied", "interviewing"]
    )
    jobs_b = [j for _, j in pairs_b]
    assert len(jobs_b) == 1
    assert jobs_b[0].id == job_beta.id


def test_disabled_notion_integration_safety(repo: Repo) -> None:
    """11. Disabled Notion integration safety (notion.enabled: false)."""
    cfg = make_settings()
    assert cfg.config.notion.enabled is False

    job = add_shared_job(repo, "a013" + "7" * 28)
    repo.set_notion_page(job.id, "notion-page-1234")

    msg = build_email(
        [job], "owner@example.com", "owner@example.com", notion=cfg.config.notion.enabled
    )
    html_content = str(msg.get_body(("html",)).get_content())
    assert "https://www.notion.so/" not in html_content
    assert "Open in Notion" not in html_content


def test_legacy_notification_regression_compatibility(repo: Repo, queue: TaskQueue) -> None:
    """12. Existing notification regression compatibility."""
    job = add_shared_job(repo, "a014" + "8" * 28, job_status=JobStatus.NEW)
    clock = FakeClock()
    clock.now = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)

    legacy_actions = Actions(
        "sec-key-32-chars-long-for-testing", "owner@example.com", candidate_id=None, clock=clock
    )
    legacy_subj = legacy_actions.subject("applied", job)
    assert f"| {job.id[:12]}." in legacy_subj

    parsed = legacy_actions.parse(legacy_subj)
    assert parsed is not None
    assert parsed.is_legacy is True
    assert parsed.candidate_id is None
    assert parsed == ("applied", job.id[:12])

    cfg = make_settings()
    reader = ReplyReader(cfg, repo, queue, legacy_actions, clock=clock)
    outcome = reader.apply(mail_incoming(legacy_subj))
    assert outcome is not None
    assert "applied:" in outcome
    assert repo.get_job(job.id).status == JobStatus.APPLIED.value
