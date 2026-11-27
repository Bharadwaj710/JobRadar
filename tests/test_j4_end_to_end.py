import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import text

from jobradar.config import AppConfig, FiltersConfig, MatchingConfig, Secrets, Settings
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline.extract import Extractor
from jobradar.pipeline.fetch import Page
from jobradar.pipeline.liveness import Liveness
from jobradar.pipeline.match import Matcher
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import EXTRACT, FETCH, MATCH, NOTION_BODY, NOTION_UPSERT
from jobradar.pipeline.technical_match import TrackAssessment, track_profile_version
from jobradar.profile import TRACK_NAMES, compact_profile, profile_version
from jobradar.providers.skills_rule import SkillsRule, skill_key
from jobradar.queue.tasks import TaskQueue
from jobradar.web import views
from jobradar.web.app import create_app
from tests.conftest import FakeClock
from tests.test_technical_match import PROFILE


@dataclass(frozen=True)
class Scenario:
    job_id: str
    company: str
    role: str
    page_text: str
    expected_tracks: tuple[str, ...]
    expected_required: tuple[str, ...] = ()
    expected_preferred: tuple[str, ...] = ()
    expected_eligibility: str = "ELIGIBLE"
    expected_scored: tuple[str, ...] = ()
    expected_unscored: tuple[str, ...] = ()
    employment: str = "Internship"
    batch: str | None = "2027"
    degree: str | None = "B.Tech and BE"
    location: str | None = "Bengaluru, India"
    work_mode: str | None = "Onsite"
    experience_min: float | None = 0
    closed: bool = False

    @property
    def url(self) -> str:
        return f"https://careers.example.com/jobs/{self.job_id}"

    def post(self) -> str:
        fields = [f"Company: {self.company}", f"Role: {self.role}"]
        if self.batch:
            fields.append(f"Batch: {self.batch}")
        if self.degree:
            fields.append(f"Eligibility: {self.degree}")
        if self.location:
            fields.append(f"Location: {self.location}")
        if self.work_mode:
            fields.append(f"Work mode: {self.work_mode}")
        fields.extend((f"Job type: {self.employment}", self.url))
        return "\n".join(fields)


SCENARIOS = (
    Scenario(
        "ai-intern",
        "Synthetic AI",
        "AI Backend Engineer Intern",
        """Responsibilities:
        Build backend APIs using Python and FastAPI.
        Minimum requirements:
        Currently pursuing a B.Tech degree. No experience required.""",
        ("ai_backend", "sde"),
        ("Python", "FastAPI"),
        expected_scored=("ai_backend",),
    ),
    Scenario(
        "full-stack-intern",
        "Synthetic Web",
        "Full Stack Developer Intern",
        """Responsibilities:
        Develop product features using React, Node.js, and Express.js.
        Minimum requirements:
        No experience required.""",
        ("full_stack", "sde"),
        ("React", "Node.js", "Express.js"),
        expected_scored=("full_stack",),
    ),
    Scenario(
        "graduate-sde",
        "Synthetic Product",
        "Graduate Engineer",
        """Responsibilities:
        Design and develop software systems and product features.
        Minimum requirements:
        Git is required. Fresh graduates welcome. No experience required.""",
        ("sde",),
        ("Git",),
        expected_scored=("sde",),
        employment="Full-time",
    ),
    Scenario(
        "sdet-intern",
        "Synthetic Quality",
        "SDET Automation Intern",
        """Minimum requirements:
        Required skills: API testing, test automation, and Pytest.
        No experience required.""",
        ("sdet",),
        ("API testing", "Test automation", "Pytest"),
        expected_scored=("sdet",),
    ),
    Scenario(
        "senior-ai",
        "Synthetic Models",
        "Senior AI Engineer",
        """Minimum requirements:
        Python, RAG, and LLMs are required.
        5+ years of professional experience required.""",
        ("ai_backend",),
        ("Python", "RAG", "LLMs"),
        expected_eligibility="NOT_ELIGIBLE",
        expected_scored=("ai_backend",),
        employment="Full-time",
        experience_min=5,
    ),
    Scenario(
        "senior-mern",
        "Synthetic Commerce",
        "Senior MERN Developer",
        """Minimum requirements:
        Build applications with React, Node.js, Express.js, and MongoDB.
        3+ years of professional experience required.""",
        ("full_stack",),
        ("React", "Node.js", "Express.js", "MongoDB"),
        expected_eligibility="NOT_ELIGIBLE",
        expected_scored=("full_stack",),
        employment="Full-time",
        experience_min=3,
    ),
    Scenario(
        "remote-outside-india",
        "Synthetic Remote",
        "Python Backend Engineer Intern",
        """Responsibilities:
        Build backend APIs using Python and FastAPI.
        Minimum requirements:
        No experience required.""",
        ("ai_backend", "sde"),
        ("Python", "FastAPI"),
        expected_eligibility="NOT_ELIGIBLE",
        expected_scored=("ai_backend",),
        location="Remote - United States only",
        work_mode="Remote",
    ),
    Scenario(
        "unknown-2027",
        "Synthetic Graduate",
        "Software Engineer Intern",
        """Responsibilities:
        Build reliable software with Git.
        Minimum requirements:
        No experience required.""",
        ("sde",),
        ("Git",),
        expected_eligibility="NEEDS_VERIFICATION",
        expected_scored=("sde",),
        batch=None,
    ),
    Scenario(
        "multi-track",
        "Synthetic Applied AI",
        "Applied AI Software Engineer Intern",
        """Responsibilities:
        Build Python and FastAPI services for RAG product features.
        Minimum requirements:
        Python and FastAPI are required. No experience required.""",
        ("ai_backend", "sde"),
        ("Python", "FastAPI"),
        expected_scored=("ai_backend", "sde"),
    ),
    Scenario(
        "insufficient-evidence",
        "Synthetic Sparse",
        "AI Backend Engineer",
        "Minimum requirements:\nPython is required for Python backend services.",
        ("ai_backend",),
        ("Python",),
        expected_eligibility="NEEDS_VERIFICATION",
        expected_unscored=("ai_backend",),
        employment="Full-time",
        batch=None,
        degree=None,
        location=None,
        work_mode=None,
        experience_min=None,
    ),
    Scenario(
        "preferred-only",
        "Synthetic Preferred",
        "AI Backend Engineer Intern",
        """Preferred qualifications:
        FastAPI preferred.
        Minimum requirements:
        No experience required.""",
        ("ai_backend",),
        expected_preferred=("FastAPI",),
        expected_unscored=("ai_backend",),
    ),
    Scenario(
        "inactive-posting",
        "Synthetic Closed",
        "Software Engineer Intern",
        """Responsibilities:
        Build software using Git.
        Minimum requirements:
        No experience required.""",
        ("sde",),
        ("Git",),
        expected_scored=("sde",),
        closed=True,
    ),
)


class FixtureFetcher:
    def __init__(self, pages: dict[str, Page]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def fetch(self, url: str) -> Page:
        self.urls.append(url)
        return self.pages[url]


class FixtureLiveness:
    def __init__(self, closed: set[str]) -> None:
        self.closed = closed
        self.urls: list[str] = []

    async def check(self, url: str | None) -> Liveness:
        assert url is not None
        self.urls.append(url)
        return Liveness.CLOSED if url in self.closed else Liveness.OPEN


def _settings(profile_path: Path) -> Settings:
    return Settings(
        config=AppConfig.model_validate(
            {
                "sources": {"telegram": {"enabled": False}},
                "filters": {
                    "batch_years": [2027],
                    "degrees": ["B.Tech", "BE"],
                    "max_experience_years": 1,
                },
                "matching": {
                    "profile_path": str(profile_path),
                    "providers": ["skills_rule"],
                },
                "notion": {"enabled": False},
                "notify": {"dry_run": True, "timezone": "Asia/Kolkata"},
                "web": {"host": "127.0.0.1", "port": 8080},
            }
        ),
        secrets=Secrets(),
    )


def _matcher(
    repo: Repo,
    queue: TaskQueue,
    clock: FakeClock,
    profile: dict[str, Any],
    track_version: str,
    liveness: FixtureLiveness,
) -> Matcher:
    filters = FiltersConfig(batch_years=[2027], degrees=["B.Tech", "BE"], max_experience_years=1)
    matching = MatchingConfig(providers=["skills_rule"])
    legacy = compact_profile(profile, filters, matching)
    return Matcher(
        repo,
        queue,
        [SkillsRule()],
        legacy,
        profile_version(legacy),
        matching,
        filters,
        clock=clock,
        tz=ZoneInfo("Asia/Kolkata"),
        liveness=liveness,  # type: ignore[arg-type]
        track_profile=profile,
        track_version=track_version,
    )


def _claim(queue: TaskQueue, type_: str, job_id: str) -> TaskRecord:
    task = queue.claim([type_])
    assert task is not None and task.key == job_id
    return task


def _seed(repo: Repo, scenario: Scenario) -> None:
    source = repo.upsert_source(Platform.TELEGRAM, "-j4f", "J4-F synthetic fixtures")
    assert source.id is not None
    raw_id, _ = repo.save_raw_message(
        source.id,
        scenario.job_id,
        "2026-10-07T08:00:00.000000Z",
        scenario.post(),
    )
    repo.get_or_create_job(scenario.job_id, scenario.url)
    repo.add_job_source(scenario.job_id, raw_id)


def _page(scenario: Scenario) -> Page:
    structured = {
        "@type": "JobPosting",
        "title": scenario.role,
        "hiringOrganization": {"name": scenario.company},
        "datePosted": "2026-10-07",
    }
    return Page(
        scenario.url,
        f"{scenario.role} | {scenario.company}",
        scenario.page_text,
        structured=structured,
    )


async def _dispatch(
    queue: TaskQueue,
    type_: str,
    job_id: str,
    handler: Any,
) -> TaskRecord:
    task = _claim(queue, type_, job_id)
    await handler(task)
    queue.complete(task)
    return task


@pytest.mark.asyncio
async def test_j4_complete_synthetic_flow(
    repo: Repo,
    queue: TaskQueue,
    clock: FakeClock,
    tmp_path: Path,
) -> None:
    profile = copy.deepcopy(PROFILE)
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    configured = _settings(profile_path)
    as_of = clock().astimezone(configured.config.notify.tz).date()
    version = track_profile_version(profile, as_of)
    assert version is not None

    pages = {scenario.url: _page(scenario) for scenario in SCENARIOS}
    fetcher = FixtureFetcher(pages)
    liveness = FixtureLiveness({scenario.url for scenario in SCENARIOS if scenario.closed})
    extractor = Extractor(repo, queue, fetcher, None, "", match=True, clock=clock)
    matcher = _matcher(repo, queue, clock, profile, version, liveness)

    for scenario in SCENARIOS:
        _seed(repo, scenario)
        queue.enqueue(FETCH, scenario.job_id)
        await _dispatch(queue, FETCH, scenario.job_id, extractor.fetch)
        await _dispatch(queue, EXTRACT, scenario.job_id, extractor.extract)

        job = repo.get_job(scenario.job_id)
        assert job is not None and job.data_json
        posting = JobPosting.model_validate_json(job.data_json)
        assert (posting.company, posting.role) == (scenario.company, scenario.role)
        assert set(scenario.expected_tracks) <= {item.track for item in posting.role_tracks}, (
            scenario.job_id
        )
        assert {skill_key(item) for item in scenario.expected_required} <= {
            skill_key(item) for item in posting.skills_required
        }
        assert {skill_key(item) for item in scenario.expected_preferred} <= {
            skill_key(item) for item in posting.skills_preferred
        }
        assert posting.experience_min == scenario.experience_min
        assert posting.published_on is not None and posting.published_on.isoformat() == "2026-10-07"

        await _dispatch(queue, MATCH, scenario.job_id, matcher.match)
        job = repo.get_job(scenario.job_id)
        assert job is not None
        rows = repo.track_matches_for_job(scenario.job_id, version)
        if scenario.closed:
            assert rows == []
            assert job.status == JobStatus.HIDDEN
            assert job.score_reason == "posting closed on the hiring site"
            continue

        assert len(rows) == len(TRACK_NAMES)
        assert {row.track for row in rows} == set(TRACK_NAMES)
        assert {row.eligibility_status for row in rows} == {scenario.expected_eligibility}
        assessments = {
            row.track: TrackAssessment.model_validate_json(row.assessment_json) for row in rows
        }
        for track in scenario.expected_scored:
            assert assessments[track].status == "scored"
            assert assessments[track].score is not None
        for track in scenario.expected_unscored:
            assert assessments[track].status == "unscored"
            assert assessments[track].score is None

    assert fetcher.urls == [scenario.url for scenario in SCENARIOS]
    assert liveness.urls == [scenario.url for scenario in SCENARIOS]

    # A duplicate delivery of the same match task updates, rather than duplicates, four rows.
    retry_id = "multi-track"
    queue.enqueue(MATCH, retry_id, {"legacy": False, "tracks": True})
    retry = _claim(queue, MATCH, retry_id)
    await matcher.match(retry)
    await matcher.match(retry)
    queue.complete(retry)
    assert len(repo.track_matches_for_job(retry_id, version)) == len(TRACK_NAMES)

    # A changed approved profile produces a separate version and keeps the earlier result history.
    changed = copy.deepcopy(profile)
    changed["evidence"]["facts"]["skills.python"]["value"].append("SQL")
    next_version = track_profile_version(changed, as_of)
    assert next_version is not None and next_version != version
    rematcher = _matcher(repo, queue, clock, changed, next_version, liveness)
    queue.enqueue(MATCH, retry_id, {"legacy": False, "tracks": True})
    await _dispatch(queue, MATCH, retry_id, rematcher.match)
    assert len(repo.track_matches_for_job(retry_id, version)) == len(TRACK_NAMES)
    assert len(repo.track_matches_for_job(retry_id, next_version)) == len(TRACK_NAMES)

    # Only local Notion synchronization tasks were left pending; no integration handler ran.
    with repo.engine.connect() as connection:
        pending = connection.execute(
            text("SELECT DISTINCT type FROM task WHERE status = 'pending' ORDER BY type")
        ).scalars()
        assert set(pending) <= {NOTION_BODY, NOTION_UPSERT}

    client = TestClient(create_app(configured, repo, clock), base_url="http://127.0.0.1:8080")
    listing = client.get("/jobs", params={"show": "everything"})
    assert listing.status_code == 200
    for scenario in SCENARIOS:
        assert listing.text.count(f'data-id="{scenario.job_id}"') == 1
        detail = client.get(f"/jobs/{scenario.job_id}")
        assert detail.status_code == 200
        assert scenario.company in detail.text and scenario.role in detail.text
        assert "Four-track technical fit" in detail.text
        if scenario.closed:
            assert "not been assessed for the active profile version" in detail.text
        else:
            assert version in detail.text

    for track, job_id in {
        "ai_backend": "ai-intern",
        "full_stack": "full-stack-intern",
        "sde": "graduate-sde",
        "sdet": "sdet-intern",
    }.items():
        filtered = client.get("/jobs", params={"show": "everything", "track": track})
        assert filtered.status_code == 200
        assert filtered.text.count(f'data-id="{job_id}"') == 1

    multi = client.get("/jobs", params={"show": "everything", "track": "ai_backend"})
    assert multi.text.count('data-id="multi-track"') == 1
    ineligible = client.get("/jobs", params={"show": "everything", "eligibility": "NOT_ELIGIBLE"})
    assert ineligible.status_code == 200
    for job_id in ("senior-ai", "senior-mern", "remote-outside-india"):
        assert ineligible.text.count(f'data-id="{job_id}"') == 1
    uncertain = client.get(
        "/jobs", params={"show": "everything", "eligibility": "NEEDS_VERIFICATION"}
    )
    assert uncertain.status_code == 200
    for job_id in ("unknown-2027", "insufficient-evidence"):
        assert uncertain.text.count(f'data-id="{job_id}"') == 1

    fresh_remote = client.get(
        "/jobs",
        params={
            "show": "everything",
            "mode": "remote",
            "published": 7,
            "place": "United States",
        },
    )
    assert fresh_remote.status_code == 200
    assert fresh_remote.text.count('data-id="remote-outside-india"') == 1
    empty = client.get(
        "/jobs",
        params={"show": "everything", "track": "sdet", "eligibility": "NOT_ELIGIBLE"},
    )
    assert empty.status_code == 200 and "No jobs match" in empty.text

    page = views.all_jobs(
        repo,
        configured.config.notify.tz,
        "everything",
        "",
        1,
        clock(),
        track_version=version,
    )
    assert (page.total, page.pages) == (len(SCENARIOS), 1)
    assert len({card.id for card in page.cards}) == len(SCENARIOS)
    assert page.assessments is not None
    assert (page.assessments.total, page.assessments.assessed) == (len(SCENARIOS), 11)
    assert page.assessments.ineligible == 3
    assert page.assessments.verification == 2

    # Activating the changed profile shows only its one current result and marks older-only
    # assessments stale; it never silently mixes the two profile versions.
    changed_path = tmp_path / "profile-changed.yaml"
    changed_path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    changed_client = TestClient(
        create_app(_settings(changed_path), repo, clock), base_url="http://127.0.0.1:8080"
    )
    changed_page = changed_client.get("/jobs", params={"show": "everything", "track": "sde"})
    assert changed_page.status_code == 200
    assert changed_page.text.count('data-id="multi-track"') == 1
    assert 'data-id="graduate-sde"' not in changed_page.text
    changed_summary = views.assessment_summary(repo, next_version)
    assert (changed_summary.assessed, changed_summary.stale) == (1, 10)

    with repo.engine.connect() as connection:
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert connection.execute(text("SELECT count(*) FROM job")).scalar_one() == len(SCENARIOS)
        assert connection.execute(text("SELECT count(*) FROM raw_message")).scalar_one() == len(
            SCENARIOS
        )
