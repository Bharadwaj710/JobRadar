"""SQLModel tables: source, raw_message, job, job_source, artifact, task, llm_cache.

Mirrors the LLD schema. Timestamps are UTC text from `jobradar.clock.to_iso`.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


class Platform(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    MANUAL = "manual"  # `jobradar add <url>`
    BOARD = "board"  # a company's job board (Greenhouse, Lever, Ashby, SmartRecruiters)
    PAGE = "page"  # a watched web page, e.g. a hiring-drive page (sources.pages)


class JobStatus(StrEnum):
    DISCOVERED = "discovered"
    EXTRACTED = "extracted"
    NEEDS_REVIEW = "needs_review"
    DISCARDED = "discarded"
    NEW = "new"
    HIDDEN = "hidden"
    SHORTLISTED = "shortlisted"
    RESUME_READY = "resume_ready"
    APPLIED = "applied"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _in(column: str, values: type[StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Source(SQLModel, table=True):
    __tablename__ = "source"
    __table_args__ = (
        UniqueConstraint("platform", "chat_id"),
        CheckConstraint(_in("platform", Platform), name="source_platform"),
    )

    id: int | None = Field(default=None, primary_key=True)
    platform: str
    chat_id: str
    title: str | None = None
    enabled: bool = True
    last_msg_id: int | None = None  # catch-up point


class RawMessage(SQLModel, table=True):
    __tablename__ = "raw_message"
    __table_args__ = (UniqueConstraint("source_id", "message_id"),)

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="source.id")
    message_id: str
    text: str | None = None
    urls_json: str = "[]"
    media_path: str | None = None  # saved image for OCR
    posted_at: str
    received_at: str
    processed: bool = False


class Job(SQLModel, table=True):
    __tablename__ = "job"
    __table_args__ = (
        CheckConstraint("scam_risk IN ('low', 'medium', 'high')", name="job_scam_risk"),
        CheckConstraint(_in("status", JobStatus), name="job_status_value"),
        Index("job_fingerprint", "fingerprint"),
        Index("job_status", "status"),
    )

    id: str = Field(primary_key=True)  # sha1(canonical_url) or sha1(fingerprint)
    canonical_url: str | None = Field(default=None, unique=True)
    fingerprint: str | None = None  # norm(company)|norm(role)|norm(location)
    company: str | None = None
    role: str | None = None
    deadline: str | None = None
    data_json: str | None = None  # full JobPosting
    score: int | None = None
    score_reason: str | None = None
    scam_risk: str | None = None
    status: str = JobStatus.DISCOVERED.value
    notion_page_id: str | None = None
    notion_synced_at: str | None = None
    prompt_version: str | None = None
    user_status_at: str | None = None  # you set the Status in Notion: JobRadar never overrides it
    created_at: str
    updated_at: str


class JobSource(SQLModel, table=True):
    __tablename__ = "job_source"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    raw_message_id: int = Field(foreign_key="raw_message.id", primary_key=True)


class Artifact(SQLModel, table=True):
    __tablename__ = "artifact"
    __table_args__ = (CheckConstraint("kind IN ('resume', 'kit')", name="artifact_kind"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    kind: str
    version: int = 1
    local_path: str | None = None
    drive_url: str | None = None  # Drive webViewLink, shown in Notion
    content_json: str | None = None  # tailored content / answers
    created_at: str


class Task(SQLModel, table=True):
    __tablename__ = "task"
    __table_args__ = (
        UniqueConstraint("type", "key"),
        CheckConstraint(_in("status", TaskStatus), name="task_status_value"),
        Index("task_ready", "status", "run_after"),
    )

    id: int | None = Field(default=None, primary_key=True)
    type: str
    key: str  # idempotency key, e.g. job id
    payload: str = "{}"
    status: str = TaskStatus.PENDING.value
    attempts: int = 0
    run_after: str
    started_at: str | None = None  # set on claim; finds tasks stuck after a crash
    rerun: bool = False  # re-enqueued while running: run again after this attempt
    priority: int | None = None  # higher runs first (FRESH: work for a job that just arrived)
    last_error: str | None = None


class FetchedPage(SQLModel, table=True):
    """Main text of a job's page (and its JobPosting data, if any), kept between the fetch
    and extract tasks."""

    __tablename__ = "fetched_page"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    final_url: str | None = None
    title: str | None = None
    text: str | None = None  # None: login wall, blocked, or not HTML; use the post text
    note: str | None = None  # why there is no text
    structured_json: str | None = None  # schema.org JobPosting embedded in the page
    fetched_at: str


class MatchResult(SQLModel, table=True):
    """A provider's answers for one job against one version of the profile. Scores are
    computed from these by code, so new weights need no new provider calls."""

    __tablename__ = "match_result"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    profile_version: str = Field(primary_key=True)
    provider: str
    answers_json: str  # MatchAnswers
    created_at: str


class JobTrackMatch(SQLModel, table=True):
    """One evidence-backed career-track assessment for a job and profile version.

    The legacy `match_result` and `job.score` remain the global matching path.  This additive
    table keeps all four J4 assessments without duplicating the job or overwriting that score.
    """

    __tablename__ = "job_track_match"
    __table_args__ = (
        CheckConstraint(
            "track IN ('ai_backend', 'full_stack', 'sde', 'sdet')",
            name="job_track_match_track_value",
        ),
        CheckConstraint(
            "technical_score IS NULL OR technical_score BETWEEN 0 AND 100",
            name="job_track_match_score_range",
        ),
        CheckConstraint(
            "evidence_coverage BETWEEN 0 AND 1",
            name="job_track_match_coverage_range",
        ),
        CheckConstraint(
            "eligibility_status IN "
            "('ELIGIBLE', 'LIKELY_ELIGIBLE', 'NEEDS_VERIFICATION', 'NOT_ELIGIBLE')",
            name="job_track_match_eligibility_value",
        ),
        Index("job_track_match_track_score", "track", "technical_score"),
        Index("job_track_match_profile_version", "profile_version"),
    )

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    profile_version: str = Field(primary_key=True)
    track: str = Field(primary_key=True)
    technical_score: int | None = None
    evidence_coverage: float = Field(ge=0, le=1)
    eligibility_status: str
    recommended_resume: str | None = None
    explanation: str
    assessment_json: str
    eligibility_json: str
    created_at: str
    updated_at: str


class JobLead(SQLModel, table=True):
    """Current verified application-readiness record for one job.

    Stable job and technical-match facts remain in their existing tables. This row stores the
    time-sensitive vacancy check, reviewed primary track, public contact evidence, and priority.
    """

    __tablename__ = "job_lead"
    __table_args__ = (
        CheckConstraint(
            "primary_track IN ('ai_backend', 'full_stack', 'sde', 'sdet')",
            name="job_lead_primary_track_value",
        ),
        CheckConstraint(
            "vacancy_status IN ('active', 'expired', 'unknown')",
            name="job_lead_vacancy_status_value",
        ),
        CheckConstraint(
            "eligibility_status IN "
            "('ELIGIBLE', 'LIKELY_ELIGIBLE', 'NEEDS_VERIFICATION', 'NOT_ELIGIBLE')",
            name="job_lead_eligibility_value",
        ),
        CheckConstraint(
            "application_priority IN ('apply_now', 'review', 'low', 'exclude')",
            name="job_lead_priority_value",
        ),
        Index("job_lead_priority", "application_priority", "last_verified_at"),
        Index("job_lead_profile_version", "profile_version"),
    )

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    profile_version: str
    primary_track: str
    vacancy_status: str
    eligibility_status: str
    application_priority: str
    official_application_url: str
    last_verified_at: str
    enrichment_json: str
    created_at: str
    updated_at: str


class StatusChange(SQLModel, table=True):
    """Every Status you set in Notion, with when JobRadar noticed: your application funnel."""

    __tablename__ = "status_change"

    id: int | None = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    status: str
    changed_at: str


class FeedBoard(SQLModel, table=True):
    """A company job board JobRadar watches, and what it saw there last time."""

    __tablename__ = "feed_board"

    key: str = Field(primary_key=True)  # "greenhouse:stripe"
    seen_json: str | None = None  # posting ids at the last good check; None: never checked
    jobs_on_board: int = 0  # all postings at the last good check, before filters
    new_total: int = 0  # postings JobRadar passed on since it started watching
    last_checked: str | None = None
    last_ok: str | None = None
    last_error: str | None = None
    misses: int = 0  # real 404s in a row; other errors never count
    skip_until: str | None = None  # a board that 404s 3 times is retried after 30 days
    latest_json: str | None = None  # newest posting seen: title, location, verdict, when


class PageState(StrEnum):
    NEW = "new"  # never read
    WATCHING = "watching"
    BACKING_OFF = "backing_off"  # the site refused (403/429); next read after wait_until
    PAUSED = "paused"  # refused 3 times in a row, or needs a login: `jobradar pages resume`
    BLOCKED = "blocked"  # robots.txt disallows it: not read until `jobradar pages resume`


class WatchedPage(SQLModel, table=True):
    """A web page JobRadar watches for new announcements, and its text at the last read."""

    __tablename__ = "watch_page"

    url: str = Field(primary_key=True)
    name: str
    state: str = PageState.NEW.value
    text: str | None = None  # cleaned main text at the last good read; None: no baseline yet
    last_checked: str | None = None
    last_ok: str | None = None
    last_changed: str | None = None  # last read that posted something
    last_error: str | None = None
    refusals: int = 0  # 403/429 in a row; other errors never count
    wait_until: str | None = None
    posts_total: int = 0


class Setting(SQLModel, table=True):
    """Small bits of state that outlive a run, e.g. the Notion poll cursor."""

    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: str


class Alert(SQLModel, table=True):
    """At most one email alert per job, ever."""

    __tablename__ = "alert"

    job_id: str = Field(foreign_key="job.id", primary_key=True)
    queued_at: str
    sent_at: str | None = None
    outcome: str | None = None  # sent | dry_run | capped (daily limit reached)


class LLMCache(SQLModel, table=True):
    __tablename__ = "llm_cache"

    key: str = Field(primary_key=True)  # sha1(model|prompt_version|input)
    output: str
    cost_inr: float | None = None
    created_at: str


class Candidate(SQLModel, table=True):
    """A candidate using JobRadar to discover and track opportunities."""

    __tablename__ = "candidate"

    id: str = Field(primary_key=True)  # uuid4
    display_name: str | None = None
    email: str | None = Field(default=None, unique=True, index=True)
    created_at: str
    updated_at: str

    @property
    def name(self) -> str | None:
        return self.display_name


class Resume(SQLModel, table=True):
    """An uploaded candidate resume document (PDF or DOCX).

    Privacy and Security Note:
    Raw uploaded resume binaries are processed in-memory during onboarding extraction and
    are intentionally NOT persisted to the local filesystem or public directories. This
    ensures sensitive personal candidate data is not stored insecurely. The `stored_path`
    attribute records the logical storage URI / identifier for tracking provenance.
    """

    __tablename__ = "resume"

    id: str = Field(primary_key=True)  # uuid4
    candidate_id: str = Field(foreign_key="candidate.id", index=True)
    original_filename: str
    stored_path: str = Field(
        description="Logical storage URI or identifier for tracking document provenance. "
        "Raw binaries are processed in-memory and not stored on disk for privacy."
    )
    file_type: str  # "pdf" | "docx"
    file_size: int
    extraction_status: str  # "extracted" | "empty_or_scanned" | "unreadable" | "failed"
    extracted_text: str | None = None
    created_at: str

    @property
    def filename(self) -> str:
        return self.original_filename

    @property
    def raw_text(self) -> str:
        return self.extracted_text or ""


class CandidateProfile(SQLModel, table=True):
    """A versioned structured profile draft or confirmed profile for a candidate."""

    __tablename__ = "candidate_profile"
    __table_args__ = (
        UniqueConstraint("candidate_id", "version", name="candidate_profile_version"),
    )

    id: str = Field(primary_key=True)  # uuid4
    candidate_id: str = Field(foreign_key="candidate.id", index=True)
    resume_id: str | None = Field(default=None, foreign_key="resume.id")
    version: int = 1
    profile_data_json: str  # structured JSON adhering to ResumeProfileDraft
    review_status: str = "draft"  # "draft" | "confirmed"
    is_active: bool = False
    confirmed_at: str | None = None
    created_at: str
    updated_at: str

    @property
    def profile_data(self) -> dict[str, Any]:

        return json.loads(self.profile_data_json) if self.profile_data_json else {}

    @property
    def status(self) -> str:
        return self.review_status


class CandidateJobAssessment(SQLModel, table=True):
    """Candidate-specific four-track technical match and eligibility assessment.

    Evaluates a shared normalized Job record against a specific CandidateProfile version.
    Isolated by candidate_id to prevent any cross-candidate overwrites.
    """

    __tablename__ = "candidate_job_assessment"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "job_id",
            "profile_version",
            "track",
            name="candidate_job_assessment_candidate_job_version_track",
        ),
        CheckConstraint(
            "track IN ('ai_backend', 'full_stack', 'sde', 'sdet')",
            name="candidate_job_assessment_track_value",
        ),
        CheckConstraint(
            "technical_score IS NULL OR technical_score BETWEEN 0 AND 100",
            name="candidate_job_assessment_score_range",
        ),
        CheckConstraint(
            "evidence_coverage BETWEEN 0 AND 1",
            name="candidate_job_assessment_coverage_range",
        ),
        CheckConstraint(
            "eligibility_status IN "
            "('ELIGIBLE', 'LIKELY_ELIGIBLE', 'NEEDS_VERIFICATION', 'NOT_ELIGIBLE')",
            name="candidate_job_assessment_eligibility_value",
        ),
        Index("candidate_job_assessment_lookup", "candidate_id", "job_id"),
        Index(
            "candidate_job_assessment_ranking",
            "candidate_id",
            "track",
            "technical_score",
        ),
    )

    id: str = Field(primary_key=True)  # uuid4
    candidate_id: str = Field(foreign_key="candidate.id", index=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    profile_id: str = Field(foreign_key="candidate_profile.id")
    profile_version: int
    track: str
    technical_score: int | None = None
    evidence_coverage: float = Field(ge=0, le=1)
    eligibility_status: str
    eligibility_reasons_json: str = "[]"  # list[str] of blockers / unresolved reasons
    matched_skills_json: str = "[]"  # list[str]
    missing_required_skills_json: str = "[]"  # list[str]
    missing_preferred_skills_json: str = "[]"  # list[str]
    recommended_resume: str | None = None
    explanation: str
    assessment_json: str  # full serialized TrackAssessment
    eligibility_json: str  # full serialized EligibilityResult
    created_at: str
    updated_at: str

    @property
    def matched_skills(self) -> list[str]:
        return json.loads(self.matched_skills_json) if self.matched_skills_json else []

    @property
    def missing_required_skills(self) -> list[str]:
        return (
            json.loads(self.missing_required_skills_json)
            if self.missing_required_skills_json
            else []
        )

    @property
    def missing_preferred_skills(self) -> list[str]:
        return (
            json.loads(self.missing_preferred_skills_json)
            if self.missing_preferred_skills_json
            else []
        )

    @property
    def eligibility_reasons(self) -> list[str]:
        return json.loads(self.eligibility_reasons_json) if self.eligibility_reasons_json else []


class CandidateApplicationStatus(StrEnum):
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFERED = "offered"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class CandidateApplication(SQLModel, table=True):
    """Independent candidate-level application tracking for a job."""

    __tablename__ = "candidate_application"
    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="candidate_application_candidate_job"),
        CheckConstraint(
            "status IN ('saved', 'applied', 'interviewing', 'offered', 'rejected', 'skipped')",
            name="candidate_application_status_value",
        ),
        Index("candidate_application_candidate_status", "candidate_id", "status"),
    )

    id: str = Field(primary_key=True)  # uuid4
    candidate_id: str = Field(foreign_key="candidate.id", index=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    status: str = "saved"
    applied_at: str | None = None
    notes: str = ""
    created_at: str
    updated_at: str


class CandidateApplicationHistory(SQLModel, table=True):
    """Audit log of candidate-specific application status transitions."""

    __tablename__ = "candidate_application_history"
    __table_args__ = (Index("candidate_app_history_candidate_job", "candidate_id", "job_id"),)

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: str = Field(foreign_key="candidate.id", index=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    status: str
    notes: str = ""
    changed_at: str


class CandidateHistoricalAssessment(SQLModel, table=True):
    """Preserved historical legacy Matcher score and reasoning for a candidate.

    Stores original numeric scores (0-100) and reasoning from the legacy Matcher
    without contaminating J4 technical scores or fabricating J4 track assessments.
    """

    __tablename__ = "candidate_historical_assessment"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "job_id",
            name="candidate_historical_assessment_candidate_job",
        ),
        Index("ix_candidate_historical_assessment_lookup", "candidate_id", "job_id"),
        CheckConstraint(
            "score BETWEEN 0 AND 100",
            name="candidate_historical_assessment_score_range",
        ),
    )

    id: str = Field(primary_key=True)  # uuid4
    candidate_id: str = Field(foreign_key="candidate.id", index=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    score: int
    score_reason: str = Field(default="")
    scoring_system: str = Field(default="legacy_matcher")
    scored_at: str | None = Field(default=None)
    created_at: str
    updated_at: str
