import asyncio
import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from jobradar.app import make_matcher
from jobradar.config import AppConfig, FiltersConfig, MatchingConfig, Secrets, Settings
from jobradar.db.models import JobStatus, TaskStatus
from jobradar.db.repo import Repo
from jobradar.pipeline import technical_match
from jobradar.pipeline.match import Matcher
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.task_types import MATCH
from jobradar.pipeline.technical_match import track_profile_version
from jobradar.profile import TRACK_NAMES
from jobradar.providers.base import JobView, MatchAnswers
from jobradar.queue.tasks import TaskQueue
from jobradar.queue.worker import Worker
from tests.conftest import FakeClock


def _fact(value: Any, category: str, source: str, *references: str) -> dict[str, Any]:
    return {
        "value": value,
        "status": "resume_supported",
        "category": category,
        "sources": [source],
        "evidence_refs": list(references),
    }


def canonical_profile() -> dict[str, Any]:
    sources = {
        name: {"kind": "resume", "file": f"D:\\resumes\\{name}.pdf", "view": view}
        for name, view in {
            "overall": "overall",
            "ai": "ai_backend",
            "web": "full_stack",
            "test": "sdet",
        }.items()
    }
    facts = {
        "education": _fact("B.Tech CSE, 2027", "education", "overall", "education"),
        "internship": _fact("Backend Engineering Intern", "experience", "overall", "internship"),
        "project": _fact("API Platform", "project", "overall", "api-project"),
        "python": _fact("Python", "skill", "overall", "internship", "api-project"),
    }
    resumes = {"ai_backend": "ai", "full_stack": "web", "sde": "overall", "sdet": "test"}
    return {
        "basics": {
            "name": "Private Name",
            "email": "private@example.com",
            "location": "Visakhapatnam, Andhra Pradesh, India",
        },
        "education": [
            {
                "degree": "B.Tech",
                "branch": "Computer Science and Engineering",
                "graduation_year": 2027,
            }
        ],
        "experience": [{"title": "Backend Engineering Intern"}],
        "projects": [{"name": "API Platform", "on_resume": True}],
        "preferences": {"work_modes": ["Remote", "Hybrid", "Onsite in India"]},
        "evidence": {"sources": sources, "facts": facts},
        "career_tracks": {
            track: {
                "label": track.replace("_", " ").title(),
                "resume": resumes[track],
                "target_roles": [track.replace("_", " ")],
                "fact_refs": list(facts),
            }
            for track in TRACK_NAMES
        },
    }


def posting(*, structured: bool = True, eligibility: bool = True) -> JobPosting:
    requirements: dict[str, Any] = {}
    if structured:
        requirements["required_skills"] = [
            {
                "skills": ["Python"],
                "relation": "all",
                "evidence": {
                    "sentence": "Python is required.",
                    "section": "Requirements",
                    "source": "page",
                },
            }
        ]
    if eligibility:
        requirements.update(
            {
                "experience": [
                    {
                        "need": "required",
                        "experience_type": "professional",
                        "minimum_years": 0,
                        "maximum_years": 1,
                        "evidence": {
                            "sentence": "0-1 years of professional experience.",
                            "section": "Requirements",
                            "source": "page",
                        },
                    }
                ],
                "experience_status": "known",
            }
        )
    return JobPosting.model_validate(
        {
            "company": "Example",
            "role": "Graduate Software Engineer",
            "employment_type": "internship",
            "batch_years": [2027] if eligibility else [],
            "degrees": ["B.Tech"] if eligibility else [],
            "locations": ["Bengaluru, India"] if eligibility else [],
            "work_mode": "onsite" if eligibility else "unknown",
            "skills_required": ["Python"] if structured else [],
            "role_tracks": [
                {
                    "track": "sde",
                    "strength": "strong",
                    "evidence": [
                        {
                            "sentence": "Build and maintain software systems.",
                            "section": "Responsibilities",
                            "source": "page",
                        }
                    ],
                }
            ],
            "requirements": requirements or None,
        }
    )


def seed_job(repo: Repo, job_id: str = "job1", value: JobPosting | None = None) -> None:
    value = value or posting()
    repo.get_or_create_job(job_id, f"https://example.com/jobs/{job_id}")
    repo.save_extraction(
        job_id,
        value.model_dump_json(),
        value.company,
        value.role,
        None,
        "rules-v1",
        JobStatus.EXTRACTED,
    )


GOOD = MatchAnswers(
    provider="skills_rule",
    eligible=1,
    location_ok=1,
    role_alignment=1,
    skills_coverage=1,
    seniority_fit=1,
    overall_fit=1,
    confidence=1,
)


class Provider:
    name = "skills_rule"

    def __init__(self) -> None:
        self.calls = 0

    async def match(self, job: JobView, profile: dict[str, Any]) -> MatchAnswers:
        self.calls += 1
        return GOOD


def matcher(
    repo: Repo,
    queue: TaskQueue,
    provider: Provider,
    clock: FakeClock,
    *,
    track_version: str = "track-v1",
    with_tracks: bool = True,
) -> Matcher:
    return Matcher(
        repo,
        queue,
        [provider],
        {"skills": ["Python"]},
        "legacy-v1",
        MatchingConfig(providers=["skills_rule"]),
        FiltersConfig(batch_years=[2027], max_experience_years=1),
        clock=clock,
        track_profile=canonical_profile() if with_tracks else None,
        track_version=track_version if with_tracks else None,
    )


async def run_match(queue: TaskQueue, worker: Matcher) -> dict[str, Any]:
    task = queue.claim([MATCH])
    assert task is not None
    await worker.match(task)
    queue.complete(task)
    return task.payload


@pytest.mark.asyncio
async def test_match_task_persists_four_tracks_and_preserves_legacy_match(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed_job(repo)
    provider = Provider()
    worker = matcher(repo, queue, provider, clock)
    queue.enqueue(MATCH, "job1")

    task_worker = Worker(queue, {MATCH: worker.match}, concurrency=1, poll_interval=0.01)
    runner = asyncio.create_task(task_worker.run())
    async with asyncio.timeout(5):
        while repo.task_counts()[TaskStatus.DONE] < 1:
            await asyncio.sleep(0.01)
    task_worker.stop()
    await runner

    rows = repo.track_matches_for_job("job1", "track-v1")
    assert [row.track for row in rows] == sorted(TRACK_NAMES)
    assert all(row.eligibility_status == "ELIGIBLE" for row in rows)
    assert provider.calls == 1
    assert repo.match_answers("legacy-v1")[0][0] == "job1"
    job = repo.get_job("job1")
    assert job is not None and job.score == 100


@pytest.mark.asyncio
async def test_unscored_tracks_and_unknown_eligibility_are_persisted(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed_job(repo, value=posting(structured=False, eligibility=False))
    worker = matcher(repo, queue, Provider(), clock)
    queue.enqueue(MATCH, "job1", {"tracks": True, "legacy": False})

    await run_match(queue, worker)

    rows = repo.track_matches_for_job("job1", "track-v1")
    assert len(rows) == 4
    assert all(row.technical_score is None for row in rows)
    assert all(row.eligibility_status == "NEEDS_VERIFICATION" for row in rows)
    sde = next(row for row in rows if row.track == "sde")
    assert json.loads(sde.assessment_json)["unscored_reason"] == (
        "required technical skills are not stated"
    )


@pytest.mark.asyncio
async def test_one_track_failure_is_isolated_and_other_tracks_persist(
    repo: Repo,
    queue: TaskQueue,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_job(repo)
    original = technical_match._assess_track

    def fail_sdet(*args: Any, **kwargs: Any) -> Any:
        track = args[2]
        if track == "sdet":
            raise ValueError("broken optional track")
        return original(*args, **kwargs)

    monkeypatch.setattr(technical_match, "_assess_track", fail_sdet)
    worker = matcher(repo, queue, Provider(), clock)
    queue.enqueue(MATCH, "job1", {"tracks": True, "legacy": False})

    await run_match(queue, worker)

    rows = repo.track_matches_for_job("job1", "track-v1")
    assert len(rows) == 4
    failed = next(row for row in rows if row.track == "sdet")
    assert json.loads(failed.assessment_json)["unscored_reason"] == (
        "track assessment failed safely (ValueError)"
    )
    assert all("failed safely" not in row.explanation for row in rows if row.track != "sdet")


@pytest.mark.asyncio
async def test_track_transaction_failure_precedes_legacy_provider_and_score(
    repo: Repo,
    queue: TaskQueue,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_job(repo)
    provider = Provider()
    worker = matcher(repo, queue, provider, clock)

    def fail_save(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("transaction rolled back")

    monkeypatch.setattr(repo, "save_track_assessments", fail_save)
    queue.enqueue(MATCH, "job1")
    task = queue.claim([MATCH])
    assert task is not None

    with pytest.raises(RuntimeError, match="transaction rolled back"):
        await worker.match(task)

    assert provider.calls == 0
    assert repo.track_matches_for_job("job1") == []
    assert repo.match_answers("legacy-v1") == []
    job = repo.get_job("job1")
    assert job is not None and job.score is None and job.status == JobStatus.EXTRACTED


@pytest.mark.asyncio
async def test_track_retry_is_idempotent_and_does_not_repeat_legacy_matching(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed_job(repo)
    provider = Provider()
    worker = matcher(repo, queue, provider, clock)
    queue.enqueue(MATCH, "job1")
    await run_match(queue, worker)

    queue.enqueue(MATCH, "job1", {"tracks": True, "legacy": False})
    await run_match(queue, worker)

    assert len(repo.track_matches_for_job("job1", "track-v1")) == 4
    assert provider.calls == 1
    assert len(repo.match_answers("legacy-v1")) == 1


@pytest.mark.asyncio
async def test_track_profile_version_preserves_history(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed_job(repo)
    provider = Provider()
    first = matcher(repo, queue, provider, clock, track_version="track-v1")
    queue.enqueue(MATCH, "job1")
    await run_match(queue, first)

    second = matcher(repo, queue, provider, clock, track_version="track-v2")
    queue.enqueue(MATCH, "job1", {"tracks": True, "legacy": False})
    await run_match(queue, second)

    assert len(repo.track_matches_for_job("job1")) == 8
    assert len(repo.track_matches_for_job("job1", "track-v1")) == 4
    assert len(repo.track_matches_for_job("job1", "track-v2")) == 4
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_track_only_catch_up_does_not_rerun_legacy_provider(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed_job(repo)
    provider = Provider()
    first = matcher(repo, queue, provider, clock, track_version="track-v1")
    queue.enqueue(MATCH, "job1")
    await run_match(queue, first)

    second = matcher(repo, queue, provider, clock, track_version="track-v2")
    assert second.catch_up() == (0, 1)
    payload = await run_match(queue, second)

    assert payload == {"legacy": False, "tracks": True}
    assert provider.calls == 1
    assert len(repo.track_matches_for_job("job1", "track-v2")) == 4


@pytest.mark.asyncio
async def test_legacy_matcher_runs_without_creating_track_rows(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    seed_job(repo)
    provider = Provider()
    worker = matcher(repo, queue, provider, clock, with_tracks=False)
    queue.enqueue(MATCH, "job1")

    await run_match(queue, worker)

    assert provider.calls == 1
    assert repo.track_matches_for_job("job1") == []
    job = repo.get_job("job1")
    assert job is not None and job.score == 100


def test_track_profile_version_uses_only_approved_matching_data() -> None:
    profile = canonical_profile()
    original = track_profile_version(profile, date(2026, 11, 17))
    private_change = copy.deepcopy(profile)
    private_change["basics"]["name"] = "Another Private Name"
    private_change["basics"]["email"] = "another@example.com"
    evidence_change = copy.deepcopy(profile)
    evidence_change["evidence"]["facts"]["python"]["value"] = "Go"

    assert original is not None
    assert track_profile_version(private_change, date(2026, 11, 17)) == original
    assert track_profile_version(evidence_change, date(2026, 11, 17)) != original
    assert track_profile_version({"education": []}, date(2026, 11, 17)) is None


def test_make_matcher_wires_canonical_profile_and_track_version(
    repo: Repo, queue: TaskQueue, tmp_path: Path
) -> None:
    profile = canonical_profile()
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    settings = Settings(
        config=AppConfig.model_validate(
            {
                "sources": {"telegram": {"enabled": False}},
                "matching": {"profile_path": str(path), "providers": ["skills_rule"]},
                "resume": {"enabled": False},
            }
        ),
        secrets=Secrets(),
    )

    worker = make_matcher(settings, repo, queue, llm=None)

    assert worker is not None
    assert worker.track_profile == profile
    assert worker.track_version is not None
