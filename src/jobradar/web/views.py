"""What each dashboard page shows, read from JobRadar's own database.

No web code here: each function turns database rows into plain data a template prints, so a
page's content is tested without a server. Times are shown in `notify.timezone`.
"""

from __future__ import annotations

import json
import re
import zlib
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

from jobradar.by_source import UNDO_MINUTES, Application, SourceRow, by_source
from jobradar.clock import to_iso
from jobradar.config import Settings
from jobradar.db.models import (
    Artifact,
    CandidateApplication,
    CandidateApplicationStatus,
    CandidateHistoricalAssessment,
    CandidateJobAssessment,
    Job,
    JobLead,
    JobStatus,
    JobTrackMatch,
)
from jobradar.db.repo import Repo
from jobradar.pipeline import status_rules as rules
from jobradar.pipeline.eligibility import EligibilityResult
from jobradar.pipeline.leads import LeadContact, LeadEnrichment
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.skill_detail import Detail as Needs
from jobradar.pipeline.skill_detail import detail, spelling
from jobradar.pipeline.skill_gaps import Demand, Gap, demand, skill_gaps
from jobradar.pipeline.task_types import BUILD_RESUME
from jobradar.pipeline.technical_match import TrackAssessment
from jobradar.profile import TRACK_NAMES, TrackName, skill_names, skill_warnings
from jobradar.providers.base import MatchAnswers
from jobradar.providers.skills_rule import compare_skills, skill_key
from jobradar.queue.tasks import TaskQueue
from jobradar.sinks.digest import gather, part_of_day, pipeline_line, previous_slot
from jobradar.sinks.email_actions import REPLIED
from jobradar.sinks.email_template import Reason, day_month, posting_of, split_reason
from jobradar.sinks.followups import MAX_REMINDERS, Draft, FollowUpReminder
from jobradar.sinks.notion import experience_text, message_link, source_label
from jobradar.sinks.notion_sync import FOLLOWED_UP
from jobradar.web import search as fts

LABELS = {
    JobStatus.NEW: "New",
    JobStatus.SHORTLISTED: "Shortlisted",
    JobStatus.RESUME_READY: "Resume ready",
    JobStatus.APPLIED: "Applied",
    JobStatus.INTERVIEW: "Interview",
    JobStatus.OFFER: "Offer",
    JobStatus.REJECTED: "Rejected",
    JobStatus.SKIPPED: "Skipped",
    JobStatus.HIDDEN: "Hidden",
    JobStatus.DISCARDED: "Not a job",
    JobStatus.NEEDS_REVIEW: "Needs a look",
    JobStatus.DISCOVERED: "Just found",
    JobStatus.EXTRACTED: "Being scored",
}
TODAY_DAYS = 3  # Today lists good matches first seen this recently
FRESH_HOURS = 6  # a job first seen this recently is marked "new"
PER_PAGE = 50

TRACK_LABELS: dict[TrackName, str] = {
    "ai_backend": "AI / backend",
    "full_stack": "Full-stack",
    "sde": "SDE",
    "sdet": "SDET",
}
TRACK_CHOICES = [("", "All tracks"), *[(track, TRACK_LABELS[track]) for track in TRACK_NAMES]]
ELIGIBILITY_LABELS = {
    "ELIGIBLE": "Eligible",
    "LIKELY_ELIGIBLE": "Likely eligible",
    "NEEDS_VERIFICATION": "Needs verification",
    "NOT_ELIGIBLE": "Not eligible",
}
ELIGIBILITY_CHOICES = [("", "Any eligibility"), *ELIGIBILITY_LABELS.items()]
TECHNICAL_SCORES = [(0, "Any technical fit"), (50, "50+"), (70, "70+"), (80, "80+"), (90, "90+")]
WORK_MODES = [("", "Any work mode"), ("remote", "Remote"), ("hybrid", "Hybrid"),
              ("onsite", "Onsite")]  # fmt: skip
PUBLISHED_WITHIN = [(0, "Any publication date"), (1, "Published in 24 h"),
                    (3, "Published in 3 days"), (7, "Published this week"),
                    (30, "Published this month")]  # fmt: skip
PRIORITY_LABELS = {
    "apply_now": "Apply now",
    "review": "Review",
    "low": "Low priority",
    "exclude": "Excluded",
}
PRIORITY_CHOICES = [("", "Any application priority"), *PRIORITY_LABELS.items()]

# The Applications board: (column, the statuses in it).
# The Applications board: (column, the status a card dropped there gets, the statuses in it).
COLUMNS: list[tuple[str, JobStatus, list[JobStatus]]] = [
    ("Shortlisted", JobStatus.SHORTLISTED, [JobStatus.SHORTLISTED, JobStatus.RESUME_READY]),
    ("Applied", JobStatus.APPLIED, [JobStatus.APPLIED]),
    ("Interview", JobStatus.INTERVIEW, [JobStatus.INTERVIEW]),
    ("Offer", JobStatus.OFFER, [JobStatus.OFFER]),
    ("Rejected", JobStatus.REJECTED, [JobStatus.REJECTED]),
]

# All jobs: the "Show" choices. "jobs" leaves out what JobRadar hid or found wasn't a job.
SHOW_JOBS = [
    JobStatus.NEW, JobStatus.NEEDS_REVIEW, JobStatus.SHORTLISTED, JobStatus.RESUME_READY,
    JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED,
    JobStatus.SKIPPED,
]  # fmt: skip
SHOW_CHOICES: list[tuple[str, str]] = [
    ("jobs", "Jobs (not hidden)"),
    *[(s.value, LABELS[s]) for s in SHOW_JOBS],
    (JobStatus.HIDDEN.value, LABELS[JobStatus.HIDDEN]),
    ("everything", "Everything"),
]


def label(status: str) -> str:
    try:
        return LABELS[JobStatus(status)]
    except ValueError:
        return status.replace("_", " ").capitalize()


def _at(iso: str) -> datetime:
    at = datetime.fromisoformat(iso)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def ago(iso: str | None, now: datetime) -> str:
    """'just now', '12 min ago', '3 h ago', '2 days ago', or ''."""
    if not iso:
        return ""
    try:
        minutes = max(int((now - _at(iso)).total_seconds() // 60), 0)
    except ValueError:
        return ""
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // (24 * 60)} days ago"


def when(iso: str | None, tz: ZoneInfo) -> str:
    """'19 Oct, 21:15' in your timezone, or ''. A bare date (boards often give only the day,
    stored as midnight UTC) shows as '19 Oct'."""
    if not iso:
        return ""
    try:
        at = _at(iso)
    except ValueError:
        return ""
    if at.astimezone(UTC).time() == time(0):
        return f"{at.day} {at:%b}"
    local = at.astimezone(tz)
    return f"{local.day} {local:%b}, {local:%H:%M}"


def _place(posting: JobPosting | None) -> str:
    if posting is None:
        return ""
    places = posting.locations[:3]
    mode = posting.work_mode.capitalize()
    if posting.work_mode not in ("unknown", "onsite") and mode.casefold() not in {
        p.casefold() for p in places
    }:
        places.append(mode)
    more = len(posting.locations) - 3
    return " · ".join(places) + (f" +{more}" if more > 0 else "")


@dataclass(frozen=True)
class TrackMatchCard:
    state: str  # current, partial, stale, or unavailable
    version: str | None
    primary: str | None
    primary_label: str | None
    secondary: list[str]
    score: int | None
    evidence: int | None
    eligibility: str | None
    eligibility_label: str | None
    resume: str | None
    message: str


def _track_label(value: str) -> str:
    return next(
        (label for track, label in TRACK_LABELS.items() if track == value),
        value.replace("_", " ").title(),
    )


@dataclass(frozen=True)
class LeadCard:
    state: str
    profile_version: str
    priority: str
    priority_label: str
    primary_track_label: str
    vacancy: str
    vacancy_label: str


def _lead_enrichment(row: JobLead) -> LeadEnrichment | None:
    try:
        return LeadEnrichment.model_validate_json(row.enrichment_json)
    except ValueError:
        return None


def lead_card(row: JobLead | None, active_version: str | None) -> LeadCard | None:
    if row is None or _lead_enrichment(row) is None:
        return None
    priority_label = PRIORITY_LABELS.get(
        row.application_priority, row.application_priority.replace("_", " ").title()
    )
    return LeadCard(
        "current"
        if active_version is not None and row.profile_version == active_version
        else "stale",
        row.profile_version,
        row.application_priority,
        priority_label,
        _track_label(row.primary_track),
        row.vacancy_status,
        row.vacancy_status.capitalize(),
    )


def _track_assessment(
    row: JobTrackMatch | CandidateJobAssessment,
) -> TrackAssessment | None:
    try:
        return TrackAssessment.model_validate_json(row.assessment_json)
    except ValueError:
        return None


def _role_alignment(assessment: TrackAssessment) -> float:
    component = assessment.components.get("role_alignment")
    return component.value if component is not None and component.value is not None else 0


def _track_rank(assessment: TrackAssessment) -> tuple[bool, int, float, float, int]:
    return (
        assessment.score is not None,
        assessment.score or -1,
        _role_alignment(assessment),
        assessment.evidence_coverage,
        -TRACK_NAMES.index(assessment.track),
    )


def track_card(
    rows: Sequence[JobTrackMatch | CandidateJobAssessment], active_version: str | None
) -> TrackMatchCard:
    """The current profile's compact assessment; older rows only make the state stale."""

    if active_version is None:
        return TrackMatchCard(
            "stale" if rows else "unavailable",
            None,
            None,
            None,
            [],
            None,
            None,
            None,
            None,
            None,
            "The active profile has no evidence-backed four-track views.",
        )
    current = [row for row in rows if str(row.profile_version) == str(active_version)]
    if not current:
        return TrackMatchCard(
            "stale" if rows else "unavailable",
            active_version,
            None,
            None,
            [],
            None,
            None,
            None,
            None,
            None,
            (
                "Assessments exist only for an older profile version."
                if rows
                else "No assessment exists for the active profile version."
            ),
        )
    parsed = [assessment for row in current if (assessment := _track_assessment(row))]
    aligned = [assessment for assessment in parsed if _role_alignment(assessment) > 0]
    primary = max(aligned, key=_track_rank) if aligned else None
    ordered = sorted(
        (item for item in aligned if item is not primary), key=_track_rank, reverse=True
    )
    eligibility = current[0].eligibility_status
    available = len({assessment.track for assessment in parsed})
    complete = available == len(TRACK_NAMES)
    return TrackMatchCard(
        "current" if complete else "partial",
        active_version,
        primary.track if primary else None,
        TRACK_LABELS[primary.track] if primary else None,
        [TRACK_LABELS[item.track] for item in ordered],
        primary.score if primary else None,
        round(100 * primary.evidence_coverage) if primary else None,
        eligibility,
        ELIGIBILITY_LABELS.get(eligibility, eligibility.replace("_", " ").title()),
        primary.recommended_resume if primary else None,
        "" if complete else f"Only {available} of {len(TRACK_NAMES)} track results are valid.",
    )


@dataclass(frozen=True)
class Card:
    """One job as a line on a list or a card on the board."""

    id: str
    role: str
    company: str
    score: int | None
    status: str
    status_label: str
    place: str
    closes: str  # "24 Oct", or ""
    seen: str  # first seen, "19 Oct, 21:15"
    moved: str  # when you last set its status, or ""
    reason: Reason
    initials: str  # the company's avatar: "RU"
    hue: int  # and its colour, 0-7, stable per company
    days: int | None  # days since you last set its status
    tags: str  # quick-filter tags on Today: "eligible remote internship closing"
    closes_in: int | None  # days until the deadline (negative: closed), None: no deadline
    url: str | None  # the job's own page
    seen_at: str  # first seen, ISO, for sorting
    age: str  # "3 h ago"
    fresh: bool  # first seen in the last few hours
    actions: list[tuple[str, str]]  # the buttons for its status: (action, label)
    work_mode: str = ""
    published: str = ""
    track_match: TrackMatchCard | None = None
    lead: LeadCard | None = None
    closed_by_employer: bool = False
    historical_score: int | None = None
    is_legacy_score: bool = False
    legacy_score_reason: str = ""
    candidate_status: str | None = None


def initials(name: str) -> str:
    words = [w for w in name.replace("&", " ").split() if w[:1].isalnum()]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return (words[0][:2] if words else "?").upper()


def hue(name: str) -> int:
    return zlib.crc32(name.casefold().encode()) % 8


def _tags(job: Job, posting: JobPosting | None, reason: Reason, closes_in: int | None) -> list[str]:
    tags: list[str] = []
    if reason.eligible:
        tags.append("eligible")
    if posting and posting.work_mode in ("remote", "hybrid"):
        tags.append("remote")
    if (posting and posting.employment_type == "internship") or "intern" in (
        job.role or ""
    ).casefold():
        tags.append("internship")
    if closes_in is not None and 0 <= closes_in <= 7:
        tags.append("closing")
    return tags


QUICK_FILTERS = [
    ("all", "All"), ("eligible", "Eligible"), ("remote", "Remote or hybrid"),
    ("internship", "Internships"), ("closing", "Closing soon"),
]  # fmt: skip


BUTTON_LABELS = {
    "applied": "I applied", "shortlist": "Shortlist", "skip": "Not interested",
    "interview": "Interview", "followed up": "I followed up", "offer": "Offer",
    "rejected": "Rejected",
}  # fmt: skip
# The buttons each status shows, most likely first. (The rules allow a little more, e.g.
# Interview straight from New; that's in Change status.)
_BUTTONS: dict[str, list[str]] = {
    JobStatus.NEW.value: ["applied", "shortlist", "skip"],
    JobStatus.HIDDEN.value: ["shortlist", "applied"],
    JobStatus.SHORTLISTED.value: ["applied", "skip"],
    JobStatus.RESUME_READY.value: ["applied", "skip"],
    JobStatus.SKIPPED.value: ["shortlist", "applied"],
    JobStatus.APPLIED.value: ["interview", "followed up", "rejected"],
    JobStatus.INTERVIEW.value: ["offer", "rejected"],
}


def buttons(job: Job) -> list[tuple[str, str]]:
    return [(a, BUTTON_LABELS[a]) for a in _BUTTONS.get(job.status, []) if rules.can(a, job)]


CANDIDATE_STATUS_LABELS: dict[str, str] = {
    "saved": "Shortlisted",
    "shortlisted": "Shortlisted",
    "resume_ready": "Resume ready",
    "applied": "Applied",
    "interviewing": "Interview",
    "interview": "Interview",
    "offered": "Offer",
    "offer": "Offer",
    "rejected": "Rejected",
    "skipped": "Skipped",
    "new": "New",
}


def candidate_label(status: str) -> str:
    return CANDIDATE_STATUS_LABELS.get(status, status.replace("_", " ").capitalize())


CANDIDATE_BUTTONS: dict[str, list[str]] = {
    "new": ["applied", "shortlist", "skip"],
    "saved": ["applied", "skip"],
    "shortlisted": ["applied", "skip"],
    "resume_ready": ["applied", "skip"],
    "applied": ["interview", "followed up", "rejected"],
    "interviewing": ["offer", "rejected"],
    "interview": ["offer", "rejected"],
    "offered": [],
    "offer": [],
    "rejected": [],
    "skipped": ["shortlist", "applied"],
}


def candidate_actions(status: str) -> list[tuple[str, str]]:
    acts = CANDIDATE_BUTTONS.get(status, ["applied", "shortlist", "skip"])
    return [(a, BUTTON_LABELS[a]) for a in acts if a in BUTTON_LABELS]


def card(
    job: Job,
    tz: ZoneInfo,
    now: datetime | None = None,
    track_match: TrackMatchCard | None = None,
    lead: LeadCard | None = None,
    application: CandidateApplication | None = None,
    candidate_assessment: CandidateJobAssessment | None = None,
    historical_assessment: CandidateHistoricalAssessment | None = None,
) -> Card:
    posting = posting_of(job)
    company = job.company or ""
    reason = split_reason(job.score_reason, posting)
    closes_in = None
    if job.deadline and now is not None:
        try:
            closes_in = (date.fromisoformat(job.deadline[:10]) - now.astimezone(tz).date()).days
        except ValueError:
            closes_in = None

    closed_by_employer = job.status in (JobStatus.HIDDEN.value, JobStatus.DISCARDED.value)

    if application is not None:
        status_val = application.status
        status_lbl = candidate_label(application.status)
        acts = candidate_actions(application.status)
        if (
            application.status == CandidateApplicationStatus.APPLIED.value
            and application.applied_at
        ):
            moved = when(application.applied_at, tz)
            days = max((now - _at(application.applied_at)).days, 0) if now is not None else None
        else:
            moved = when(application.updated_at, tz)
            days = max((now - _at(application.updated_at)).days, 0) if now is not None else None
    else:
        status_val = job.status
        status_lbl = label(job.status)
        acts = buttons(job)
        days = None
        if job.user_status_at and now is not None:
            days = max((now - _at(job.user_status_at)).days, 0)
        moved = when(job.user_status_at, tz)

    # Technical Score vs Historical Legacy Score separation
    score: int | None = None
    historical_score: int | None = None
    is_legacy_score = False
    legacy_score_reason = ""

    if candidate_assessment is not None:
        score = candidate_assessment.technical_score
        if historical_assessment is not None:
            historical_score = historical_assessment.score
            legacy_score_reason = historical_assessment.score_reason
    elif historical_assessment is not None:
        # Never present historical legacy score as current J4 technical score
        score = None
        historical_score = historical_assessment.score
        is_legacy_score = True
        legacy_score_reason = historical_assessment.score_reason
    else:
        score = job.score

    return Card(
        id=job.id,
        role=job.role or "Role not stated",
        company=job.company or "",
        score=score,
        status=status_val,
        status_label=status_lbl,
        place=_place(posting),
        closes=day_month(job.deadline),
        seen=when(job.created_at, tz),
        moved=moved,
        reason=reason,
        initials=initials(company or job.role or "?"),
        hue=hue(company or job.role or "?"),
        days=days,
        tags=" ".join(_tags(job, posting, reason, closes_in)),
        closes_in=closes_in,
        url=job.canonical_url,
        seen_at=job.created_at,
        age=ago(job.created_at, now) if now is not None else "",
        fresh=now is not None and now - _at(job.created_at) < timedelta(hours=FRESH_HOURS),
        actions=acts,
        work_mode=(
            posting.work_mode.capitalize() if posting and posting.work_mode != "unknown" else ""
        ),
        published=(
            day_month(posting.published_on.isoformat()) if posting and posting.published_on else ""
        ),
        track_match=track_match,
        lead=lead,
        closed_by_employer=closed_by_employer,
        historical_score=historical_score,
        is_legacy_score=is_legacy_score,
        legacy_score_reason=legacy_score_reason,
        candidate_status=application.status if application is not None else None,
    )


# ---- Today -----------------------------------------------------------------------------------


def today(
    repo: Repo,
    min_score: int,
    now: datetime,
    tz: ZoneInfo,
    candidate_id: str | None = None,
) -> list[Card]:
    """Good matches you haven't acted on, first seen in the last few days, best first."""
    since = to_iso(now - timedelta(days=TODAY_DAYS))
    jobs = repo.good_new_jobs(min_score, since)
    if candidate_id:
        apps = {a.job_id: a for a in repo.list_candidate_applications(candidate_id)}
        assessments_by_job: dict[str, CandidateJobAssessment] = {}
        for a in repo.list_candidate_assessments(candidate_id):
            if a.job_id not in assessments_by_job or (
                a.technical_score is not None
                and assessments_by_job[a.job_id].technical_score is None
            ):
                assessments_by_job[a.job_id] = a
        historical_by_job: dict[str, CandidateHistoricalAssessment] = {
            h.job_id: h for h in repo.list_historical_assessments(candidate_id)
        }
        active_jobs = [
            j
            for j in jobs
            if apps.get(j.id) is None or apps[j.id].status in ("saved", "shortlisted")
        ]
        return [
            card(
                j,
                tz,
                now,
                application=apps.get(j.id),
                candidate_assessment=assessments_by_job.get(j.id),
                historical_assessment=historical_by_job.get(j.id),
            )
            for j in active_jobs
        ]
    return [card(j, tz, now) for j in jobs]


# ---- Applications ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Track:
    """Where an application stands, beyond its status."""

    resume: bool  # a resume was built for it
    replied: str | None  # the company wrote back: "interview", "test", "rejected"
    followed_up: str  # the day you last followed up, "21 Oct", or ""
    next_nudge: str  # when JobRadar will draft a follow-up, "29 Oct", or ""
    nudge_in: int | None  # days until then (0: due now)
    waited: int  # % of the wait to the next follow-up, for the meter
    reminders: int  # follow-up drafts sent so far


@dataclass(frozen=True)
class BoardCard:
    card: Card
    track: Track


@dataclass(frozen=True)
class Column:
    key: str  # the status a card dropped here gets
    title: str
    cards: list[BoardCard]
    note: str  # one line under the title: "2 due for a follow-up"


REPLY_WORDS = {"interview": "Interview invite", "test": "Online test", "rejected": "Rejection"}


def track(
    repo: Repo,
    job: Job,
    tz: ZoneInfo,
    now: datetime,
    nudges: FollowUpReminder,
    application: CandidateApplication | None = None,
) -> Track:
    raw = repo.get_setting(REPLIED + job.id)
    replied = None
    if raw:
        try:
            replied = str(json.loads(raw).get("kind") or "") or None
        except ValueError:
            replied = None
    followed = repo.get_setting(FOLLOWED_UP + job.id) or ""
    next_nudge, nudge_in, waited, reminders = "", None, 0, 0

    app_applied = (
        application is not None
        and application.status == CandidateApplicationStatus.APPLIED.value
        and application.applied_at is not None
    )
    legacy_applied = (
        application is None
        and job.status == JobStatus.APPLIED.value
        and job.user_status_at is not None
    )

    if (app_applied or legacy_applied) and replied is None:
        if app_applied and application is not None and application.applied_at:
            since = _at(application.applied_at)
            reminders = 0
            if reminders < MAX_REMINDERS:
                due = since + nudges.after
                nudge_in = max((due.astimezone(tz).date() - now.astimezone(tz).date()).days, 0)
                next_nudge = f"{due.astimezone(tz).day} {due.astimezone(tz):%b}"
                total = nudges.after.total_seconds()
                waited = min(100, max(0, round(100 * (now - since).total_seconds() / total)))
        else:
            _, since, reminders = nudges.status(job)
            if reminders < MAX_REMINDERS:
                due = since + nudges.after
                nudge_in = max((due.astimezone(tz).date() - now.astimezone(tz).date()).days, 0)
                next_nudge = f"{due.astimezone(tz).day} {due.astimezone(tz):%b}"
                total = nudges.after.total_seconds()
                waited = min(100, max(0, round(100 * (now - since).total_seconds() / total)))
    return Track(
        resume=repo.latest_artifact(job.id) is not None,
        replied=replied,
        followed_up=day_month(followed) if followed else "",
        next_nudge=next_nudge,
        nudge_in=nudge_in,
        waited=waited,
        reminders=reminders,
    )


def _note(key: JobStatus, cards: list[BoardCard]) -> str:
    if not cards:
        return ""
    if key == JobStatus.SHORTLISTED:
        ready = sum(1 for c in cards if c.track.resume)
        return f"{ready} with a resume ready" if ready else "No resumes built yet"
    if key == JobStatus.APPLIED:
        due = sum(1 for c in cards if c.track.nudge_in == 0)
        replied = sum(1 for c in cards if c.track.replied)
        bits = [f"{due} due for a follow-up"] if due else []
        bits += [f"{replied} replied"] if replied else []
        days = [c.card.days for c in cards if c.card.days is not None]
        if days and (average := round(sum(days) / len(days))) >= 1:
            bits.append(f"waiting {average} day{'s' if average > 1 else ''} on average")
        return " · ".join(bits)
    if key == JobStatus.INTERVIEW:
        return "Prepare, then set Offer or Rejected"
    return ""


def board(
    repo: Repo, settings: Settings, now: datetime, candidate_id: str | None = None
) -> list[Column]:
    tz = settings.config.notify.tz
    nudges = FollowUpReminder(settings, repo, TaskQueue(repo))
    if candidate_id:
        apps_with_jobs = repo.list_candidate_applications_with_jobs(candidate_id)

        # Preload assessments for candidate
        assessments_by_job: dict[str, CandidateJobAssessment] = {}
        for a in repo.list_candidate_assessments(candidate_id):
            if a.job_id not in assessments_by_job or (
                a.technical_score is not None
                and assessments_by_job[a.job_id].technical_score is None
            ):
                assessments_by_job[a.job_id] = a

        # Preload historical assessments for candidate
        historical_by_job: dict[str, CandidateHistoricalAssessment] = {
            h.job_id: h for h in repo.list_historical_assessments(candidate_id)
        }

        col_cards: dict[str, list[BoardCard]] = {
            JobStatus.SHORTLISTED.value: [],
            JobStatus.APPLIED.value: [],
            JobStatus.INTERVIEW.value: [],
            JobStatus.OFFER.value: [],
            JobStatus.REJECTED.value: [],
        }

        status_col_map = {
            "saved": JobStatus.SHORTLISTED.value,
            "shortlisted": JobStatus.SHORTLISTED.value,
            "resume_ready": JobStatus.SHORTLISTED.value,
            "applied": JobStatus.APPLIED.value,
            "interviewing": JobStatus.INTERVIEW.value,
            "interview": JobStatus.INTERVIEW.value,
            "offered": JobStatus.OFFER.value,
            "offer": JobStatus.OFFER.value,
            "rejected": JobStatus.REJECTED.value,
        }

        for app, j in apps_with_jobs:
            target_col = status_col_map.get(app.status)
            if not target_col or target_col not in col_cards:
                continue
            c = card(
                j,
                tz,
                now,
                application=app,
                candidate_assessment=assessments_by_job.get(j.id),
                historical_assessment=historical_by_job.get(j.id),
            )
            t = track(repo, j, tz, now, nudges, application=app)
            col_cards[target_col].append(BoardCard(c, t))

        columns = []
        for title, key, _ in COLUMNS:
            cards = col_cards.get(key.value, [])
            columns.append(Column(key.value, title, cards, _note(key, cards)))
        return columns

    columns = []
    for title, key, statuses in COLUMNS:
        cards = [
            BoardCard(card(j, tz, now), track(repo, j, tz, now, nudges))
            for j in repo.jobs_in(statuses)
        ]
        columns.append(Column(key.value, title, cards, _note(key, cards)))
    return columns


# ---- All jobs --------------------------------------------------------------------------------


@dataclass(frozen=True)
class AssessmentSummary:
    version: str | None
    total: int
    assessed: int
    stale: int
    eligible: int
    likely: int
    verification: int
    ineligible: int


def assessment_summary(repo: Repo, version: str | None) -> AssessmentSummary:
    counts = repo.track_dashboard_counts(version)
    return AssessmentSummary(
        version=version,
        total=counts.total_jobs,
        assessed=counts.assessed_jobs,
        stale=counts.stale_jobs,
        eligible=counts.eligibility.get("ELIGIBLE", 0),
        likely=counts.eligibility.get("LIKELY_ELIGIBLE", 0),
        verification=counts.eligibility.get("NEEDS_VERIFICATION", 0),
        ineligible=counts.eligibility.get("NOT_ELIGIBLE", 0),
    )


@dataclass(frozen=True)
class JobList:
    cards: list[Card]
    total: int
    page: int
    pages: int
    show: str
    words: str
    sort: str = "new"
    filters: Filters = field(default_factory=lambda: Filters())
    snippets: dict[str, Markup] = field(default_factory=dict)  # job id -> where it matched
    assessments: AssessmentSummary | None = None

    def query(self, page: int) -> str:
        """This search's address on another page."""
        f = self.filters
        pairs = {"q": self.words, "show": self.show, "sort": self.sort, "min": f.min_score or "",
                 "within": f.within or "", "place": f.place, "remote": "1" if f.remote else "",
                  "source": f.source or "", "track": f.track, "eligibility": f.eligibility,
                  "technical": f.technical_min or "", "mode": f.work_mode,
                   "published": f.published_within or "", "priority": f.priority,
                   "page": page}  # fmt: skip
        return "/jobs?" + urlencode({k: v for k, v in pairs.items() if v not in ("", None)})


@dataclass
class Filters:
    min_score: int | None = None
    within: int | None = None  # days
    place: str = ""
    remote: bool = False
    source: int | None = None
    track: str = ""
    eligibility: str = ""
    technical_min: int | None = None
    work_mode: str = ""
    published_within: int | None = None
    priority: str = ""

    @property
    def active(self) -> int:
        return sum(
            bool(x)
            for x in (
                self.min_score,
                self.within,
                self.place,
                self.remote,
                self.source,
                self.track,
                self.eligibility,
                self.technical_min,
                self.work_mode,
                self.published_within,
                self.priority,
            )
        )


MIN_SCORES = [(0, "Any score"), (50, "50+"), (75, "75+"), (90, "90+")]
WITHIN = [
    (0, "Any time"),
    (1, "Last 24 h"),
    (3, "Last 3 days"),
    (7, "Last week"),
    (30, "Last month"),
]


def show_statuses(show: str) -> list[str] | None:
    """The statuses a "Show" choice lists; None for everything. Unknown → "jobs"."""
    if show == "everything":
        return None
    if show in {s.value for s in JobStatus}:
        return [show]
    return [s.value for s in SHOW_JOBS]


SORTS = [("new", "Newest first"), ("old", "Oldest first"), ("score", "Best match"),
         ("relevance", "Most relevant")]  # fmt: skip


def all_jobs(
    repo: Repo,
    tz: ZoneInfo,
    show: str,
    words: str,
    page: int,
    now: datetime | None = None,
    sort: str = "new",
    filters: Filters | None = None,
    track_version: str | None = None,
    candidate_id: str | None = None,
) -> JobList:
    filters = filters or Filters()
    now = now or datetime.now(UTC)
    if show not in dict(SHOW_CHOICES):
        show = "jobs"
    words = words.strip()[:100]
    if sort not in dict(SORTS) or (sort == "relevance" and not words):
        sort = "relevance" if words and sort not in dict(SORTS) else "new"
    query = fts.match(words, filters.place, filters.remote)
    if query:
        fts.refresh(repo, now)
    since = to_iso(now - timedelta(days=filters.within)) if filters.within else None
    published_since = (
        (now.astimezone(tz).date() - timedelta(days=filters.published_within)).isoformat()
        if filters.published_within
        else None
    )
    args = (
        show_statuses(show),
        query,
        filters.min_score or None,
        since,
        filters.source,
        track_version,
        filters.track or None,
        filters.eligibility or None,
        filters.technical_min or None,
        filters.work_mode or None,
        published_since,
        filters.priority or None,
        sort,
    )
    _, total = repo.find_jobs(*args, 0, 0, candidate_id=candidate_id)
    pages = max(1, -(-total // PER_PAGE))
    page = min(max(page, 1), pages)
    found, total = repo.find_jobs(*args, PER_PAGE, (page - 1) * PER_PAGE, candidate_id=candidate_id)
    job_ids = [job.id for job, _ in found]
    track_rows: dict[str, list[JobTrackMatch]] = {}
    lead_rows = {row.job_id: row for row in repo.leads_for_jobs(job_ids)}

    cand_assessments_map: dict[str, list[CandidateJobAssessment]] = {}
    cand_apps_map: dict[str, CandidateApplication] = {}
    if candidate_id and job_ids:
        for ca in repo.list_candidate_assessments(candidate_id, job_ids=job_ids):
            cand_assessments_map.setdefault(ca.job_id, []).append(ca)
        for c_app in repo.list_candidate_applications(candidate_id, job_ids=job_ids):
            cand_apps_map[c_app.job_id] = c_app
    else:
        for row in repo.track_matches_for_jobs(job_ids):
            track_rows.setdefault(row.job_id, []).append(row)

    cards: list[Card] = []
    for j, _ in found:
        if candidate_id and j.id in cand_assessments_map:
            c_list = cand_assessments_map[j.id]
            if filters.track:
                matched_t = [a for a in c_list if a.track == filters.track]
                primary_assessment = matched_t[0] if matched_t else c_list[0]
            else:
                primary_assessment = max(
                    c_list,
                    key=lambda a: a.technical_score if a.technical_score is not None else -1,
                )
            t_card = track_card(c_list, str(c_list[0].profile_version))
            card_app = cand_apps_map.get(j.id)
            cards.append(
                card(
                    j,
                    tz,
                    now,
                    track_match=t_card,
                    lead=lead_card(lead_rows.get(j.id), track_version),
                    application=card_app,
                    candidate_assessment=primary_assessment,
                )
            )
        else:
            cards.append(
                card(
                    j,
                    tz,
                    now,
                    track_match=track_card(track_rows.get(j.id, []), track_version),
                    lead=lead_card(lead_rows.get(j.id), track_version),
                    application=cand_apps_map.get(j.id),
                )
            )

    return JobList(
        cards,
        total,
        page,
        pages,
        show,
        words,
        sort,
        filters,
        {j.id: fts.snippet(snip) for j, snip in found if snip},
        assessment_summary(repo, track_version),
    )


# ---- One job ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Seen:
    where: str
    at: str
    link: str | None


@dataclass(frozen=True)
class File:
    id: int | None
    name: str
    kind: str
    at: str
    version: int = 1
    url: str | None = None  # opens the PDF, when it's there
    cover_url: str | None = None  # the cover letter written next to it, when there is one
    tailored: bool = False
    problems: list[str] = field(default_factory=list)  # the resume check's findings
    skills_on: list[str] = field(default_factory=list)  # the job's skills on the page
    skills_off: list[str] = field(default_factory=list)  # the job's skills you have, not on it


@dataclass
class EligibilityCheckView:
    dimension: str
    status: str
    reason: str
    evidence: list[str]


@dataclass
class EligibilityView:
    status: str
    label: str
    blockers: list[str]
    unresolved: list[str]
    checks: list[EligibilityCheckView]


@dataclass
class TrackAssessmentView:
    track: TrackName
    label: str
    available: bool
    score: int | None = None
    status: str = "unavailable"
    evidence: int | None = None
    matched_required: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    matched_preferred: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    internships: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    explanation: str = ""
    unscored_reason: str | None = None
    resume: str | None = None


@dataclass
class TrackAssessmentSet:
    state: str
    active_version: str | None
    stored_versions: list[str]
    message: str
    tracks: list[TrackAssessmentView]
    eligibility: EligibilityView | None = None


@dataclass
class LeadPanel:
    state: str
    profile_version: str
    priority: str
    priority_label: str
    primary_track_label: str
    vacancy: str
    eligibility: str
    official_application_url: str
    official_source_url: str
    verified: str
    technical_score: int | None
    location: str | None
    work_mode: str
    compensation: str | None
    internship_duration: str | None
    mandatory_experience: list[str]
    preferred_experience: list[str]
    eligibility_evidence: list[str]
    unresolved: list[str]
    priority_reasons: list[str]
    recommended_resume: str | None
    contacts: list[LeadContact]
    application_strategy: str
    outreach_draft: str | None
    cover_letter_draft: str | None


def lead_panel(row: JobLead | None, active_version: str | None, tz: ZoneInfo) -> LeadPanel | None:
    if row is None or (enrichment := _lead_enrichment(row)) is None:
        return None
    priority_label = PRIORITY_LABELS.get(
        row.application_priority, row.application_priority.replace("_", " ").title()
    )
    return LeadPanel(
        state=(
            "current"
            if active_version is not None and row.profile_version == active_version
            else "stale"
        ),
        profile_version=row.profile_version,
        priority=row.application_priority,
        priority_label=priority_label,
        primary_track_label=_track_label(row.primary_track),
        vacancy=row.vacancy_status.capitalize(),
        eligibility=ELIGIBILITY_LABELS.get(
            row.eligibility_status, row.eligibility_status.replace("_", " ").title()
        ),
        official_application_url=enrichment.official_application_url,
        official_source_url=enrichment.official_source_url,
        verified=when(enrichment.last_verified_at.isoformat(), tz),
        technical_score=enrichment.technical_score,
        location=enrichment.location,
        work_mode=enrichment.work_mode,
        compensation=enrichment.compensation,
        internship_duration=enrichment.internship_duration,
        mandatory_experience=list(enrichment.mandatory_experience),
        preferred_experience=list(enrichment.preferred_experience),
        eligibility_evidence=list(enrichment.eligibility_evidence),
        unresolved=list(enrichment.unresolved),
        priority_reasons=list(enrichment.priority_reasons),
        recommended_resume=enrichment.recommended_resume,
        contacts=list(enrichment.contacts),
        application_strategy=enrichment.application_strategy,
        outreach_draft=enrichment.outreach_draft,
        cover_letter_draft=enrichment.cover_letter_draft,
    )


@dataclass
class Detail:
    card: Card
    take: str  # Blip's one-line reading of the score
    breakdown: Breakdown | None
    url: str | None  # the job's own page
    apply_url: str | None  # where the job says to apply, when it's a different address
    score_reason: str | None
    facts: list[tuple[str, str]] = field(default_factory=list)
    skills_required: list[Skill] = field(default_factory=list)
    skills_preferred: list[Skill] = field(default_factory=list)
    summary: str = ""
    seen_in: list[Seen] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)
    files: list[File] = field(default_factory=list)
    building: bool = False  # a resume is queued or being built
    draft: Draft | None = None  # the follow-up JobRadar would send (Applied jobs)
    page_text: str | None = None
    track_assessments: TrackAssessmentSet | None = None
    lead: LeadPanel | None = None
    candidate_application: CandidateApplication | None = None
    historical_assessment: CandidateHistoricalAssessment | None = None
    candidate_assessments: list[CandidateJobAssessment] = field(default_factory=list)


def _job_skill_names(posting: JobPosting | None, preferred: bool) -> list[str]:
    if posting is None:
        return []
    structured = []
    if posting.requirements is not None:
        structured = (
            posting.requirements.preferred_skills
            if preferred
            else posting.requirements.required_skills
        )
    values = [skill for item in structured for skill in item.skills]
    if not values:
        values = posting.skills_preferred if preferred else posting.skills_required
    return list(dict.fromkeys(values))


def _eligibility_view(
    row: JobTrackMatch | CandidateJobAssessment,
) -> EligibilityView | None:
    try:
        result = EligibilityResult.model_validate_json(row.eligibility_json)
    except ValueError:
        return None
    return EligibilityView(
        result.state.value,
        ELIGIBILITY_LABELS.get(result.state.value, result.state.value.replace("_", " ").title()),
        list(result.blockers),
        list(result.unresolved),
        [
            EligibilityCheckView(
                check.dimension.replace("_", " ").title(),
                check.status,
                check.reason,
                list(
                    dict.fromkeys(
                        evidence.sentence or evidence.value for evidence in check.evidence
                    )
                ),
            )
            for check in result.checks
        ],
    )


def track_assessment_set(
    rows: Sequence[JobTrackMatch | CandidateJobAssessment],
    active_version: str | None,
    posting: JobPosting | None,
) -> TrackAssessmentSet:
    stored = sorted({str(row.profile_version) for row in rows})
    if active_version is None:
        return TrackAssessmentSet(
            "stale" if rows else "unavailable",
            None,
            stored,
            "The active profile has no evidence-backed four-track views.",
            [],
        )
    current = [row for row in rows if str(row.profile_version) == str(active_version)]
    if not current:
        return TrackAssessmentSet(
            "stale" if rows else "unavailable",
            active_version,
            stored,
            (
                "Stored assessments use an older profile version; rematching has not been run."
                if rows
                else "This job has not been assessed for the active profile version."
            ),
            [],
        )
    by_track = {row.track: row for row in current}
    required = _job_skill_names(posting, preferred=False)
    preferred = _job_skill_names(posting, preferred=True)
    panels: list[TrackAssessmentView] = []
    for track in TRACK_NAMES:
        row = by_track.get(track)
        assessment = _track_assessment(row) if row is not None else None
        if assessment is None:
            panels.append(
                TrackAssessmentView(
                    track,
                    TRACK_LABELS[track],
                    False,
                    explanation="No valid current assessment is stored for this track.",
                )
            )
            continue
        matched = {skill_key(name) for name in assessment.matched_skills}
        panels.append(
            TrackAssessmentView(
                track=track,
                label=TRACK_LABELS[track],
                available=True,
                score=assessment.score,
                status=assessment.status,
                evidence=round(100 * assessment.evidence_coverage),
                matched_required=[name for name in required if skill_key(name) in matched],
                missing_required=list(assessment.missing_required_skills),
                preferred_skills=preferred,
                matched_preferred=[name for name in preferred if skill_key(name) in matched],
                projects=list(assessment.relevant_projects),
                internships=list(assessment.relevant_internships),
                experience=list(assessment.relevant_experience),
                explanation=assessment.explanation,
                unscored_reason=assessment.unscored_reason,
                resume=assessment.recommended_resume,
            )
        )
    available = sum(panel.available for panel in panels)
    complete = available == len(TRACK_NAMES)
    return TrackAssessmentSet(
        "current" if complete else "partial",
        active_version,
        stored,
        "" if complete else f"Only {available} of {len(TRACK_NAMES)} current results are valid.",
        panels,
        _eligibility_view(current[0]),
    )


def _facts(job: Job, posting: JobPosting | None) -> list[tuple[str, str]]:
    facts: list[tuple[str, str]] = []
    if posting is None:
        return facts
    if posting.locations:
        facts.append(("Location", ", ".join(posting.locations)))
    if posting.work_mode != "unknown":
        facts.append(("Work mode", posting.work_mode.capitalize()))
    if posting.employment_type != "unknown":
        facts.append(("Type", posting.employment_type.replace("_", " ").capitalize()))
    if experience := experience_text(posting):
        facts.append(("Experience", experience))
    if posting.batch_years:
        facts.append(("Batch", ", ".join(str(y) for y in posting.batch_years)))
    if posting.degrees:
        facts.append(("Degree", ", ".join(posting.degrees)))
    if posting.salary_text:
        facts.append(("Salary", posting.salary_text))
    if closes := day_month(job.deadline):
        facts.append(("Closes", closes))
    if posting.published_on:
        facts.append(("Posted", day_month(posting.published_on.isoformat())))
    return facts


_VERSION = re.compile(r"_v\d+\.pdf$")


def artifact_file(
    settings: Settings | None, artifact: Artifact, cover: bool = False
) -> Path | None:
    """The PDF for this resume, or the cover letter beside it ({…}_cover_letter.pdf), if it's
    a file inside storage.local_dir. Never a path from anywhere else."""
    if not artifact.local_path:
        return None
    base = (settings.config.storage.local_dir if settings else Path("output")).resolve()
    path = Path(artifact.local_path)
    path = (path if path.is_absolute() else Path.cwd() / path).resolve()
    if cover:
        if not _VERSION.search(path.name):
            return None
        path = path.with_name(_VERSION.sub("_cover_letter.pdf", path.name))
    if path.suffix.lower() != ".pdf" or not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def _file(artifact: Artifact, tz: ZoneInfo, settings: Settings | None = None) -> File:
    path = artifact.local_path
    name = Path(path).name if path else f"{artifact.kind} v{artifact.version}"
    record: dict[str, Any] = {}
    try:
        record = json.loads(artifact.content_json or "{}")
    except ValueError:
        record = {}
    ats_report = record.get("ats") or {}
    has_file = artifact_file(settings, artifact) is not None
    has_cover = artifact_file(settings, artifact, cover=True) is not None
    return File(
        id=artifact.id,
        name=name,
        kind=artifact.kind.replace("_", " "),
        at=when(artifact.created_at, tz),
        version=artifact.version,
        url=f"/files/{artifact.id}" if has_file else None,
        cover_url=f"/files/{artifact.id}?cover=1" if has_cover else None,
        tailored=bool(record.get("tailored")),
        problems=[str(p) for p in ats_report.get("problems") or []],
        skills_on=[str(k) for k in ats_report.get("skills_on_resume") or []],
        skills_off=[str(k) for k in ats_report.get("skills_not_on_resume") or []],
    )


@dataclass(frozen=True)
class Skill:
    name: str
    have: bool | None  # on your profile; None when there's no profile to compare


def skills(names: list[str], profile: dict[str, Any] | None) -> list[Skill]:
    if not profile:
        return [Skill(n, None) for n in names]
    have, _ = compare_skills(names, profile)
    return [Skill(n, n in have) for n in names]


@dataclass(frozen=True)
class Part:
    """One weighted part of the score."""

    name: str
    value: int  # 0-100
    weight: int  # % of the score


@dataclass(frozen=True)
class Breakdown:
    parts: list[Part]
    eligible: int | None  # 0-100: how sure the job is open to you (a gate, not weighted)
    confidence: int  # 0-100
    by: str  # "AI read the job" / "Rules" / "Skills only"


PART_NAMES = {
    "skills_coverage": "Skills you have",
    "role_alignment": "Role fits what you want",
    "seniority_fit": "Right level for you",
    "location_ok": "Location works",
}
BY = {"llm": "AI read the whole job", "rules": "Rules (a hard filter)",
      "skills_rule": "Skills only (no AI)"}  # fmt: skip


def breakdown(answers_json: str | None, weights: dict[str, float]) -> Breakdown | None:
    if not answers_json:
        return None
    try:
        answers = MatchAnswers.model_validate_json(answers_json)
    except ValueError:
        return None
    total = sum(weights.values()) or 1
    parts = [
        Part(PART_NAMES.get(name, name), round(100 * value), round(100 * w / total))
        for name, w in weights.items()
        if (value := getattr(answers, name, None)) is not None and w > 0
    ]
    eligible = None if answers.eligible is None else round(100 * answers.eligible)
    if not parts and eligible is None:
        return None
    by = BY.get(answers.provider, answers.provider)
    return Breakdown(parts, eligible, round(100 * answers.confidence), by)


def job_detail(
    repo: Repo,
    job_id: str,
    tz: ZoneInfo,
    now: datetime,
    weights: dict[str, float] | None = None,
    profile: dict[str, Any] | None = None,
    settings: Settings | None = None,
    track_version: str | None = None,
    candidate_id: str | None = None,
) -> Detail | None:
    job = repo.get_job(job_id)
    if job is None:
        return None

    app: CandidateApplication | None = None
    cand_assessments: list[CandidateJobAssessment] = []
    historical: CandidateHistoricalAssessment | None = None
    primary_cand_assessment: CandidateJobAssessment | None = None

    if candidate_id:
        ctx_data = repo.get_job_with_candidate_context(job_id, candidate_id)
        if ctx_data:
            app = ctx_data.get("application")
            cand_assessments = ctx_data.get("assessments", [])
            historical = ctx_data.get("historical_assessment")
            if cand_assessments:
                if track_version:
                    matched = [
                        a for a in cand_assessments if str(a.profile_version) == str(track_version)
                    ]
                    primary_cand_assessment = matched[0] if matched else cand_assessments[0]
                else:
                    primary_cand_assessment = cand_assessments[0]

    draft = None
    app_is_applied = app is not None and app.status == CandidateApplicationStatus.APPLIED.value
    legacy_is_applied = app is None and job.status == JobStatus.APPLIED.value
    if settings is not None and (app_is_applied or legacy_is_applied):
        draft = FollowUpReminder(settings, repo, TaskQueue(repo)).draft_for(job)
    posting = posting_of(job)
    page = repo.get_page(job.id)
    apply_url = posting.apply_url if posting else None

    # Status history: candidate-scoped if candidate_id and candidate has history, else legacy
    if candidate_id:
        cand_hist = repo.list_candidate_application_history(candidate_id, job_id=job.id)
        if cand_hist:
            history = [(candidate_label(h.status), when(h.changed_at, tz)) for h in cand_hist]
        else:
            history = []
    else:
        history = [(label(st), when(at, tz)) for st, at in repo.status_history(job.id)]

    card_obj = card(
        job,
        tz,
        now,
        application=app,
        candidate_assessment=primary_cand_assessment,
        historical_assessment=historical,
    )

    return Detail(
        card=card_obj,
        take=take(split_reason(job.score_reason, posting)),
        breakdown=breakdown(repo.latest_match(job.id), weights or {}),
        url=job.canonical_url,
        apply_url=apply_url if apply_url and apply_url != job.canonical_url else None,
        score_reason=job.score_reason,
        facts=_facts(job, posting),
        skills_required=skills(posting.skills_required, profile) if posting else [],
        skills_preferred=skills(posting.skills_preferred, profile) if posting else [],
        summary=posting.summary if posting else "",
        seen_in=[
            Seen(source_label(s), when(s.posted_at, tz), message_link(s))
            for s in repo.job_sightings(job.id)
        ],
        history=history,
        files=[_file(a, tz, settings) for a in repo.artifacts_of(job.id)],
        building=repo.task_waiting(BUILD_RESUME, job.id),
        draft=draft,
        page_text=page.text if page and page.text else None,
        track_assessments=track_assessment_set(
            cand_assessments if cand_assessments else repo.track_matches_for_job(job.id),
            str(cand_assessments[0].profile_version) if cand_assessments else track_version,
            posting,
        ),
        lead=lead_panel(repo.get_job_lead(job.id), track_version, tz),
        candidate_application=app,
        historical_assessment=historical,
        candidate_assessments=cand_assessments,
    )


# ---- Blip ------------------------------------------------------------------------------------

AWAKE_MINUTES = 20  # read something this recently: JobRadar is running


@dataclass(frozen=True)
class Overview:
    """The numbers in the sidebar and on Today, and what Blip is up to."""

    today: int  # good matches you haven't acted on
    active: int  # on the Applications board, still open
    jobs: int  # All jobs (not hidden)
    applied: int
    interviews: int
    closing: int  # jobs you may still apply to, closing in the next 2 days
    awake: bool
    last_read: str  # "3 min ago", or ""
    name: str  # your first name, for the greeting


def overview(repo: Repo, min_score: int, now: datetime, tz: ZoneInfo, name: str) -> Overview:
    counts = repo.status_counts()

    def n(*statuses: JobStatus) -> int:
        return sum(counts.get(st.value, 0) for st in statuses)

    since = to_iso(now - timedelta(days=TODAY_DAYS))
    day = now.astimezone(tz).date()
    closing = repo.jobs_closing(day.isoformat(), (day + timedelta(days=2)).isoformat())
    last = repo.last_activity()
    awake = bool(last) and (now - _at(last or "")) < timedelta(minutes=AWAKE_MINUTES)
    return Overview(
        today=len(repo.good_new_jobs(min_score, since)),
        active=n(
            JobStatus.SHORTLISTED, JobStatus.RESUME_READY, JobStatus.APPLIED, JobStatus.INTERVIEW
        ),
        jobs=n(*SHOW_JOBS),
        applied=n(JobStatus.APPLIED, JobStatus.INTERVIEW, JobStatus.OFFER, JobStatus.REJECTED),
        interviews=n(JobStatus.INTERVIEW, JobStatus.OFFER),
        closing=len(closing),
        awake=awake,
        last_read=ago(last, now),
        name=name.split()[0] if name.strip() and name != "Your Name" else "",
    )


def greeting(now: datetime, tz: ZoneInfo, name: str) -> str:
    hour = now.astimezone(tz).hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if hour < 17 else "evening"
    return f"Good {part}, {name}." if name else f"Good {part}."


def blip_says(cards: list[Card], min_score: int) -> str:
    """Blip's speech bubble on Today."""
    if not cards:
        return f"Nothing new scored {min_score}+ in the last {TODAY_DAYS} days. I'll keep looking."
    best = cards[0]
    at = f" at {best.company}" if best.company else ""
    many = f"{len(cards)} good matches" if len(cards) > 1 else "One good match"
    return f"{many} waiting. The best is {best.role}{at}, a {best.score}."


def take(r: Reason) -> str:
    """'Strong fit. You're eligible, Pune works, and you have Python, SQL. Missing: Kafka.'"""
    bits: list[str] = []
    if r.eligible is True:
        bits.append("you're eligible")
    elif r.eligible is False and r.eligibility:
        bits.append(r.eligibility[0].lower() + r.eligibility[1:])
    elif r.eligibility:
        bits.append("eligibility is unclear")
    if r.place and r.place_ok:
        bits.append(f"{r.place} works")
    elif r.place_ok is False:
        bits.append("the location doesn't fit")
    if r.have:
        bits.append("you have " + ", ".join(r.have))
    text = (r.fit + ". ") if r.fit else ""
    if bits:
        joined = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
        text += joined[0].upper() + joined[1:] + "."
    if r.missing:
        text += " Missing: " + ", ".join(r.missing) + "."
    if r.unsure:
        text += " I'm not sure about this score."
    return text.strip() or "I couldn't read enough of this job to say."


# ---- charts ----------------------------------------------------------------------------------

PULSE_DAYS = 14


@dataclass(frozen=True)
class Day:
    day: str  # "19"
    weekday: str  # "Mon"
    found: int
    good: int
    today: bool


@dataclass(frozen=True)
class Pulse:
    days: list[Day]
    found: int
    good: int
    peak: int  # the tallest bar, for the scale


def pulse(repo: Repo, min_score: int, now: datetime, tz: ZoneInfo) -> Pulse:
    """Jobs first seen each day for the last two weeks, and how many were good matches."""
    today = now.astimezone(tz).date()
    first = today - timedelta(days=PULSE_DAYS - 1)
    start = datetime.combine(first, time(0), tz)
    found = dict.fromkeys(range(PULSE_DAYS), 0)
    good = dict.fromkeys(range(PULSE_DAYS), 0)
    for job in repo.jobs_created_since(to_iso(start)):
        if job.status == JobStatus.DISCARDED.value:
            continue
        i = (_at(job.created_at).astimezone(tz).date() - first).days
        if 0 <= i < PULSE_DAYS:
            found[i] += 1
            good[i] += (job.score or 0) >= min_score
    days = [
        Day(str(d.day), f"{d:%a}", found[i], good[i], d == today)
        for i in range(PULSE_DAYS)
        if (d := first + timedelta(days=i))
    ]
    return Pulse(days, sum(found.values()), sum(good.values()), max([1, *found.values()]))


@dataclass(frozen=True)
class Source:
    name: str
    jobs: int
    share: int  # % of the biggest source, for the bar


GROUPS = {"board": "Company boards", "page": "Watched pages", "manual": "Added by you",
          "email": "Email"}  # fmt: skip


def sources(repo: Repo, min_score: int, now: datetime, top: int = 5) -> list[Source]:
    """Where your good matches of the last two weeks came from: each Telegram chat, and
    company boards, pages and the rest as one line each."""
    since = to_iso(now - timedelta(days=PULSE_DAYS))
    counts: dict[str, int] = {}
    for platform, title, n in repo.good_job_sources(min_score, since):
        name = GROUPS.get(platform, title)
        counts[name] = counts.get(name, 0) + n
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    peak = max([1, *(n for _, n in ranked)])
    return [Source(name, n, round(100 * n / peak)) for name, n in ranked]


@dataclass(frozen=True)
class Step:
    name: str
    jobs: int
    width: int  # % of the first step, for the bar
    rate: str  # "12% of the step before", or ""


def funnel(repo: Repo, min_score: int) -> list[Step]:
    """From every job found to offers: how far your applications get."""
    counts = repo.status_counts()
    found = sum(n for st, n in counts.items() if st != JobStatus.DISCARDED.value)
    with_good = repo.search_good_count(min_score)
    after_shortlist = [JobStatus.SHORTLISTED, JobStatus.RESUME_READY, JobStatus.APPLIED,
                       JobStatus.INTERVIEW, JobStatus.OFFER]  # fmt: skip
    steps = [
        ("Found", found),
        (f"Scored {min_score}+", with_good),
        ("Shortlisted", repo.jobs_ever_in(after_shortlist, UNDO_MINUTES)),
        ("Applied", repo.jobs_ever_in(after_shortlist[2:], UNDO_MINUTES)),
        ("Interview", repo.jobs_ever_in([JobStatus.INTERVIEW, JobStatus.OFFER], UNDO_MINUTES)),
        ("Offer", repo.jobs_ever_in([JobStatus.OFFER], UNDO_MINUTES)),
    ]
    top = max(steps[0][1], 1)
    out: list[Step] = []
    for i, (name, n) in enumerate(steps):
        before = steps[i - 1][1] if i else 0
        rate = f"{round(100 * n / before)}% of {steps[i - 1][0].lower()}" if i and before else ""
        out.append(Step(name, n, max(round(100 * n / top), 1 if n else 0), rate))
    return out


@dataclass(frozen=True)
class SourceStrip:
    """Interviews by source, above the Applications board."""

    summary: str
    rows: list[tuple[SourceRow, list[tuple[Card, Application]]]]  # each row with its jobs


def source_strip(repo: Repo, tz: ZoneInfo, candidate_id: str | None = None) -> SourceStrip | None:
    """None until you've applied to something."""
    report = by_source(repo, candidate_id=candidate_id)
    if not report.rows:
        return None
    cards: dict[str, Card] = {}
    rows = []
    for row in report.rows:
        jobs = []
        for a in row.jobs:
            if a.job_id not in cards and (job := repo.get_job(a.job_id)):
                cards[a.job_id] = card(job, tz)
            if a.job_id in cards:
                jobs.append((cards[a.job_id], a))
        rows.append((row, jobs))
    return SourceStrip(report.summary, rows)


# ---- the command palette (Ctrl+K) -------------------------------------------------------------


def search(repo: Repo, words: str, limit: int = 8) -> list[dict[str, object]]:
    """Jobs whose company or role contains `words`, as plain data for the palette."""
    if not words.strip():
        return []
    jobs, _ = repo.search_jobs(None, words[:100], limit, 0)
    return [
        {"id": j.id, "role": j.role or "Role not stated", "company": j.company or "",
         "score": j.score, "status": label(j.status)}
        for j in jobs
    ]  # fmt: skip


# ---- the digest -------------------------------------------------------------------------------

DIGEST_WINDOWS = 8  # the current window and the digests before it


@dataclass(frozen=True)
class Window:
    key: str  # "now", or the digest time: "2026-10-19-1900"
    label: str  # "Now", "Evening · 19 Oct"
    since: datetime
    until: datetime
    current: bool


def windows(now: datetime, times: list[str]) -> list[Window]:
    """The window the next digest will cover, then each earlier digest's, newest first.
    `now` is in your timezone."""
    out = [Window("now", "So far", previous_slot(now - timedelta(minutes=1), times), now, True)]
    until = out[0].since
    for _ in range(DIGEST_WINDOWS - 1):
        since = previous_slot(until - timedelta(minutes=1), times)
        label = f"{part_of_day(until)} · {until.day} {until:%b}"
        out.append(Window(f"{until:%Y-%m-%d-%H%M}", label, since, until, False))
        until = since
    return out


@dataclass(frozen=True)
class Board:
    card: Card
    board: str


@dataclass
class DigestPage:
    window: Window
    windows: list[Window]
    title: str  # "Evening digest"
    span: str  # "since Mon 19 Oct, 09:00" / "19 Oct, 09:00 to 19:00"
    new: int
    strong: int
    alerts: int
    inbox: list[Card]
    hidden: list[tuple[str, int]]
    closing: list[Card]
    boards: list[Board]
    pipeline: str | None
    spent: str
    failed: int
    gaps: list[Gap]
    gap_jobs: int

    @property
    def hidden_total(self) -> int:
        return sum(n for _, n in self.hidden)


def digest(
    repo: Repo, settings: Settings, now: datetime, key: str, profile: dict[str, Any] | None
) -> DigestPage:
    """A digest on the page: what the next one will say ("now"), or an earlier one again."""
    notify = settings.config.notify
    tz = notify.tz
    local = now.astimezone(tz)
    all_windows = windows(local, notify.digest_times)
    # No choice: the latest digest that went out (what's in your inbox), not the window
    # that's only just begun.
    window = next((w for w in all_windows if w.key == key), all_windows[1])
    data = gather(repo, settings, window.since, window.until, bounded=not window.current)
    min_score = settings.config.scoring.alert_above
    gaps: list[Gap] = []
    gap_jobs = 0
    if profile:
        gaps, gap_jobs = skill_gaps(repo, profile, window.until - timedelta(days=7), min_score)
    since = f"{window.since:%a} {window.since.day} {window.since:%b}, {window.since:%H:%M}"
    span = (
        f"since {since}"
        if window.current
        else f"{since} to {window.until:%H:%M}"
        if window.since.date() == window.until.date()
        else f"{since} to {window.until:%a} {window.until.day} {window.until:%b}, "
        f"{window.until:%H:%M}"
    )
    title = "Next digest, so far" if window.current else f"{part_of_day(window.until)} digest"
    return DigestPage(
        window=window,
        windows=all_windows,
        title=title,
        span=span,
        new=data.new,
        strong=sum(1 for j in data.inbox if (j.score or 0) >= min_score),
        alerts=data.alerts,
        inbox=[card(j, tz, now) for j in data.inbox[:30]],
        hidden=sorted(data.hidden.items(), key=lambda kv: -kv[1]),
        closing=[card(j, tz, now) for j in data.closing[:10]],
        boards=[Board(card(j, tz, now), b) for j, b in data.boards[:10]],
        pipeline=pipeline_line(data),
        spent=f"₹{data.spent_inr:.0f} of ₹{data.budget_inr:.0f}",
        failed=data.failed,
        gaps=gaps,
        gap_jobs=gap_jobs,
    )


# ---- Skills ------------------------------------------------------------------------------------

SKILL_WINDOWS = [(7, "7 days"), (30, "30 days"), (90, "90 days")]
MAX_DOTS = 24  # per skill row; the rest show as "+n"


@dataclass(frozen=True)
class SkillRow:
    key: str
    name: str
    jobs: int  # for a skill you're missing: the jobs that need it (not "Java or C++")
    share: int  # % of the good matches looked at
    width: int  # % of the top skill, for the bar
    trend: int | None  # jobs asking for it vs the window before; None: nothing to compare
    samples: list[Card]  # the best few jobs that asked for it
    covered: int = 0  # jobs that named it but take a skill you have instead
    covered_by: str = ""  # those skills: "Java or Python"
    dots: list[str] = field(default_factory=list)  # one per job, how it asks (strictest first)
    more: int = 0  # jobs beyond the dots shown


@dataclass
class SkillsPage:
    days: int
    looked_at: int
    min_score: int
    missing: list[SkillRow]
    have: list[SkillRow]
    warnings: list[str]
    verdict: str = ""  # about the first missing skill: what its jobs really want
    counts: Counter[str] = field(default_factory=Counter)  # the first missing skill's kinds


def _skill_name(key: str, d: Demand, names: dict[str, str]) -> str:
    """Your spelling, else the usual one ("scikit-learn" stays lowercase), else capitalized."""
    if (yours := names.get(key)) and not yours.islower():
        return yours
    if (usual := d.name(key)) != key:
        return usual
    plain = yours or key
    return plain[:1].upper() + plain[1:]


def _either(words: list[str], joiner: str = "or") -> str:
    """'Java', 'Java or Python', 'Azure, GCP or IBM Cloud'."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {joiner} {words[-1]}"


def _rows(
    counts: Counter[str],
    earlier: Counter[str] | None,
    d: Demand,
    repo: Repo,
    tz: ZoneInfo,
    now: datetime,
    top: int,
    names: dict[str, str],
    details: dict[str, Needs] | None = None,
) -> list[SkillRow]:
    def order(kv: tuple[str, int]) -> tuple[int, str]:
        return -kv[1], _skill_name(kv[0], d, names).casefold()

    ranked = sorted(counts.items(), key=order)[:top]
    peak = max([1, *(n for _, n in ranked)])
    rows = []
    for key, n in ranked:
        samples = [card(j, tz, now) for i in d.jobs[key][:4] if (j := repo.get_job(i))]
        trend = None if earlier is None else n - earlier.get(key, 0)
        det = (details or {}).get(key)
        by = [_skill_name(k, d, names) for k in det.covered_by[:3]] if det else []
        open_ = [x.kind for x in det.needs if x.kind != "covered"] if det else ["have"] * n
        kinds: list[str] = sorted(open_, key=lambda k: ORDER_KINDS.index(k) if det else 0)
        rows.append(SkillRow(key, _skill_name(key, d, names), n,
                             round(100 * n / max(d.looked_at, 1)), round(100 * n / peak), trend,
                             samples, det.counts["covered"] if det else 0,
                             _either(by) if by else "", kinds[:MAX_DOTS],
                             max(0, len(kinds) - MAX_DOTS)))  # fmt: skip
    return rows


def _yours(profile: dict[str, Any]) -> dict[str, str]:
    """How you wrote each skill in profile.yaml ("Next.js"), by key."""
    yours: dict[str, str] = {}
    for full in skill_names(profile):
        plain = full.split(" (")[0]
        for part in [plain, *re.split(r"/|\s&\s", plain)]:  # "Docker & Docker Compose": Docker
            yours.setdefault(skill_key(part), part.strip())
    return yours


def _details(d: Demand) -> dict[str, Needs]:
    return {k: detail(k, d.jobs[k], d.texts, d.have_keys) for k in d.missing}


def _needed(details: dict[str, Needs]) -> Counter[str]:
    return Counter({k: x.needed for k, x in details.items() if x.needed})


def skills_page(
    repo: Repo, settings: Settings, profile: dict[str, Any] | None, now: datetime, days: int
) -> SkillsPage:
    """The skills your good matches ask for: the ones your profile lacks (what to learn, or
    to add if you know them) and the ones it has (what to put first on a resume). A missing
    skill counts only the jobs that need it: "Python, Java or C++" doesn't count for C++."""
    days = days if days in dict(SKILL_WINDOWS) else 30
    min_score = settings.config.scoring.alert_above
    tz = settings.config.notify.tz
    if not profile:
        return SkillsPage(days, 0, min_score, [], [], ["profile.yaml couldn't be read"])
    window = timedelta(days=days)
    d = demand(repo, profile, now - window, None, min_score)
    before = demand(repo, profile, now - 2 * window, now - window, min_score)
    yours = _yours(profile)
    details = _details(d)
    gone, had = (None, None) if not before.looked_at else (_needed(_details(before)), before.have)
    missing = _rows(_needed(details), gone, d, repo, tz, now, 20, yours, details)
    first = details[missing[0].key] if missing else None
    return SkillsPage(
        days=days,
        looked_at=d.looked_at,
        min_score=min_score,
        missing=missing,
        have=_rows(d.have, had, d, repo, tz, now, 12, yours),
        warnings=skill_warnings(profile),
        verdict=_verdict(missing[0].name, first, lambda k: _skill_name(k, d, yours))
        if first
        else "",
        counts=first.counts if first else Counter(),
    )


# ---- One skill: what exactly they want --------------------------------------------------------

KIND_WORDS = {
    "must": "Must have", "listed": "In its skill list", "either": "One of",
    "nice": "Nice to have", "covered": "You're covered",
}  # fmt: skip
ORDER_KINDS = ["must", "listed", "either", "nice", "covered"]  # strictest first
# How the quotes are grouped: "In its skill list" reads as a must have.
GROUP_OF = {"must": "must", "listed": "must", "either": "either", "nice": "nice",
            "covered": "covered"}  # fmt: skip
GROUP_WORDS = [("must", "Must have"), ("either", "Any one of a few"), ("nice", "Nice to have"),
               ("covered", "Covered by your skills")]  # fmt: skip
QUOTE_CHARS = 240


@dataclass(frozen=True)
class TopicRow:
    label: str
    jobs: int
    width: int
    cards: list[Card]
    kinds: list[str] = field(default_factory=list)  # each job's verdict, strictest first


@dataclass(frozen=True)
class Tile:
    """One job in the roll call: how it asks for the skill."""

    kind: str
    job_id: str
    title: str  # "Rubrik: must have", for the tooltip


@dataclass(frozen=True)
class QuoteGroup:
    kind: str
    title: str
    quotes: list[Quote]


@dataclass(frozen=True)
class Quote:
    card: Card
    kind: str
    label: str
    html: Markup
    note: str  # "or Azure, Google Cloud" / "you have Java"


@dataclass
class SkillDetailPage:
    key: str
    name: str
    days: int
    looked_at: int
    asked: int  # jobs that named it
    needed: int
    counts: Counter[str]
    verdict: str
    start: str  # what to learn first, from the topics
    topics: list[TopicRow]
    alongside: list[tuple[str, int, bool]]  # (name, jobs, you have it)
    quotes: list[Quote]
    missing: bool  # not in your profile (so "I know this" applies)
    tiles: list[Tile] = field(default_factory=list)
    groups: list[QuoteGroup] = field(default_factory=list)
    legend: list[tuple[str, str, int]] = field(default_factory=list)  # (kind, words, jobs)
    mood: str = ""  # Blip's: "happy" when your skills already cover most of these jobs


def _quote(text: str, pattern: re.Pattern[str]) -> Markup:
    """The sentence, cut to about QUOTE_CHARS around the skill, with the skill marked."""
    if len(text) > QUOTE_CHARS:
        m = pattern.search(text)
        a = max(0, (m.start() if m else 0) - QUOTE_CHARS // 3)
        b = min(len(text), a + QUOTE_CHARS)
        text = ("… " if a else "") + text[a:b].strip() + (" …" if b < len(text) else "")
    parts: list[Markup] = []
    last = 0
    for m in pattern.finditer(text):
        parts += [escape(text[last : m.start()]), Markup("<mark>%s</mark>") % m.group(0)]
        last = m.end()
    parts.append(escape(text[last:]))
    return Markup("").join(parts)


def _verdict(nm: str, det: Needs, names: Callable[[str], str]) -> str:
    c, total = det.counts, len(det.needs)
    if c["covered"] and c["covered"] * 2 >= total:
        by = _either([names(k) for k in det.covered_by[:3]])
        if not det.needed:
            if total == 1:
                return f"The one job naming {nm} takes {by} instead, which you have."
            return f"None of these {total} jobs needs {nm}: each takes {by}, which you have."
        return (f"{c['covered']} of {total} jobs take {by} instead, which you already have. "
                f"Only {det.needed} really need{'s' if det.needed == 1 else ''} {nm}.")  # fmt: skip
    if c["either"] * 2 >= total:
        alts = Counter(a for n in det.needs if n.kind == "either" for a in n.alternatives)
        choices = _either([nm, *(names(k) for k, _ in alts.most_common(3))])
        return (f"{c['either']} of {total} jobs take any one of {choices}. Get good at one "
                "and these are covered; the rest ask for it outright or as a plus.")  # fmt: skip
    outright = c["must"] + c["listed"]
    more = f", and {c['nice']} more call it a plus" if c["nice"] else ""
    return f"{outright} of {total} jobs ask for {nm} outright{more}."


def skill_detail_page(
    repo: Repo,
    settings: Settings,
    profile: dict[str, Any] | None,
    now: datetime,
    days: int,
    key: str,
) -> SkillDetailPage | None:
    """What your good matches want of one skill; None when none of them named it."""
    days = days if days in dict(SKILL_WINDOWS) else 30
    if not profile:
        return None
    tz = settings.config.notify.tz
    d = demand(repo, profile, now - timedelta(days=days), None, settings.config.scoring.alert_above)
    if key not in d.jobs:
        return None
    yours = _yours(profile)

    def names(k: str) -> str:
        return _skill_name(k, d, yours)

    det = detail(key, d.jobs[key], d.texts, d.have_keys)
    cards = {i: card(j, tz, now) for i in d.jobs[key] if (j := repo.get_job(i))}
    peak = max([1, *(len(t.jobs) for t in det.topics)])
    kind_of = {n.job_id: n.kind for n in det.needs}

    def strictest(ids: list[str]) -> list[str]:
        return sorted((kind_of[i] for i in ids), key=ORDER_KINDS.index)

    topics = [
        TopicRow(t.label, len(t.jobs), round(100 * len(t.jobs) / peak),
                 [cards[i] for i in t.jobs[:3] if i in cards], strictest(t.jobs))
        for t in det.topics
    ]  # fmt: skip
    pattern = spelling(key)
    quotes = []
    for n in sorted(det.needs, key=lambda n: ORDER_KINDS.index(n.kind)):
        if n.job_id not in cards:
            continue
        alts = [names(a) for a in n.alternatives]
        note = (_either([names(key), *alts]) if n.kind == "either" and alts else
                f"you have {_either(alts)}" if n.kind == "covered" and alts else "")  # fmt: skip
        html = _quote(n.quote.text, pattern) if n.quote else Markup("")
        quotes.append(Quote(cards[n.job_id], n.kind, KIND_WORDS[n.kind], html, note))
    often = [f"{t.label} ({len(t.jobs)} jobs)" for t in det.topics[:4] if len(t.jobs) >= 2]
    once = [t.label for t in det.topics if len(t.jobs) == 1][: max(0, 4 - len(often))]
    start = " ".join(
        [f"Named most often with it: {', '.join(often)}." if often else "",
         f"{'Also' if often else 'Named'} once each: {', '.join(once)}." if once else ""]
    ).strip()  # fmt: skip
    ordered = sorted((n for n in det.needs if n.job_id in cards),
                     key=lambda n: ORDER_KINDS.index(n.kind))  # fmt: skip
    tiles = [
        Tile(n.kind, n.job_id,
             f"{cards[n.job_id].company or cards[n.job_id].role}: {KIND_WORDS[n.kind].lower()}")
        for n in ordered
    ]  # fmt: skip
    groups = []
    for kind, title in GROUP_WORDS:
        members = [q for q in quotes if GROUP_OF[q.kind] == kind]
        if members:
            groups.append(QuoteGroup(kind, title, members))
    legend = [(g.kind, g.title, len(g.quotes)) for g in groups]
    return SkillDetailPage(
        tiles=tiles,
        groups=groups,
        legend=legend,
        mood="happy" if det.needed * 2 < len(det.needs) else "",
        key=key,
        name=names(key),
        days=days,
        looked_at=d.looked_at,
        asked=len(det.needs),
        needed=det.needed,
        counts=det.counts,
        verdict=_verdict(names(key), det, names),
        start=start,
        topics=topics,
        alongside=[(names(k), n, k in d.have_keys) for k, n in det.alongside],
        quotes=quotes,
        missing=key not in d.have_keys,
    )
