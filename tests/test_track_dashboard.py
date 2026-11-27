from datetime import date
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from jobradar.config import AppConfig, Settings
from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo
from jobradar.pipeline.eligibility import (
    EligibilityCheck,
    EligibilityEvidence,
    EligibilityResult,
    EligibilityState,
)
from jobradar.pipeline.technical_match import (
    FitComponent,
    FourTrackAssessment,
    TrackAssessment,
    track_profile_version,
)
from jobradar.profile import TRACK_NAMES, TrackName
from jobradar.web import views
from jobradar.web.app import create_app
from tests.conftest import FakeClock
from tests.test_digest import settings
from tests.test_track_worker import canonical_profile
from tests.test_web import add


def eligibility(state: EligibilityState = EligibilityState.ELIGIBLE) -> EligibilityResult:
    unresolved = (
        ["Confirm the graduation window"] if state == EligibilityState.NEEDS_VERIFICATION else []
    )
    return EligibilityResult(
        state=state,
        checks=[
            EligibilityCheck(
                dimension="graduation_year",
                status="unknown" if unresolved else "met",
                critical=True,
                reason=(
                    "The posting does not state a graduation window."
                    if unresolved
                    else "The posting includes 2027."
                ),
                evidence=[
                    EligibilityEvidence(
                        source="job.batch_years", value="2027", sentence="Class of 2027"
                    )
                ],
            )
        ],
        unresolved=unresolved,
    )


def assessment(
    track: TrackName,
    score: int | None,
    *,
    aligned: bool = False,
    coverage: float = 0.8,
) -> TrackAssessment:
    resume = {
        "ai_backend": "ResumeAI.pdf",
        "full_stack": "ResumeFS.pdf",
        "sde": "Resume_bharadwaj.pdf",
        "sdet": "Resume_T.pdf",
    }[track]
    return TrackAssessment(
        track=track,
        status="scored" if score is not None else "unscored",
        score=score,
        unscored_reason=None if score is not None else "required technical skills are not stated",
        evidence_coverage=coverage,
        matched_skills=["Python", "FastAPI"] if aligned else [],
        missing_required_skills=["Kafka"] if aligned else [],
        relevant_projects=["Nexus AI"] if aligned else [],
        relevant_internships=["AI Backend Engineer Intern"] if aligned else [],
        relevant_experience=[],
        evidence_quality={"resume_supported": 3} if aligned else {},
        explanation=f"{track} has {'supporting' if aligned else 'insufficient'} evidence.",
        recommended_resume=resume,
        components={
            "role_alignment": FitComponent(
                value=1 if aligned else 0,
                weight=25,
                reason="role evidence",
            )
        },
    )


def result(
    primary: TrackName,
    score: int | None,
    *,
    secondary: TrackName | None = None,
    state: EligibilityState = EligibilityState.ELIGIBLE,
) -> FourTrackAssessment:
    tracks = {
        track: assessment(
            track,
            score
            if track == primary
            else (max((score or 0) - 10, 0) if track == secondary else None),
            aligned=track in {primary, secondary},
        )
        for track in TRACK_NAMES
    }
    return FourTrackAssessment(eligibility=eligibility(state), tracks=tracks)


def add_assessed(
    repo: Repo,
    job_id: str,
    version: str,
    primary: TrackName,
    score: int | None,
    *,
    secondary: TrackName | None = None,
    state: EligibilityState = EligibilityState.ELIGIBLE,
    **posting: Any,
) -> None:
    add(repo, job_id, **posting)
    repo.save_track_assessments(
        job_id, version, result(primary, score, secondary=secondary, state=state)
    )


def web_settings(tmp_path: Path) -> tuple[Settings, dict[str, Any]]:
    profile = canonical_profile()
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    base = settings()
    config = base.config.model_dump(mode="python")
    config["matching"]["profile_path"] = path
    return Settings(config=AppConfig.model_validate(config), secrets=base.secrets), profile


def test_track_and_eligibility_filters_keep_multi_track_jobs_unique(
    repo: Repo, clock: FakeClock
) -> None:
    version = "profile-v1"
    add_assessed(repo, "multi", version, "ai_backend", 92, secondary="sde")
    add_assessed(repo, "web", version, "full_stack", 86)
    add_assessed(
        repo,
        "verify",
        version,
        "sdet",
        None,
        state=EligibilityState.NEEDS_VERIFICATION,
    )

    all_jobs = views.all_jobs(
        repo, settings().config.notify.tz, "jobs", "", 1, clock(), track_version=version
    )
    assert [card.id for card in all_jobs.cards].count("multi") == 1
    assert all_jobs.assessments is not None and all_jobs.assessments.assessed == 3
    assert all_jobs.assessments.total == 3

    ai = views.all_jobs(
        repo,
        settings().config.notify.tz,
        "jobs",
        "",
        1,
        clock(),
        filters=views.Filters(track="ai_backend"),
        track_version=version,
    )
    assert [card.id for card in ai.cards] == ["multi"]
    assert ai.cards[0].track_match is not None
    assert ai.cards[0].track_match.primary == "ai_backend"
    assert ai.cards[0].track_match.secondary == ["SDE"]

    uncertain = views.all_jobs(
        repo,
        settings().config.notify.tz,
        "jobs",
        "",
        1,
        clock(),
        filters=views.Filters(eligibility="NEEDS_VERIFICATION", track="sdet"),
        track_version=version,
    )
    assert [card.id for card in uncertain.cards] == ["verify"]
    assert uncertain.cards[0].track_match is not None
    assert uncertain.cards[0].track_match.score is None
    assert uncertain.cards[0].track_match.eligibility_label == "Needs verification"


def test_technical_filter_applies_to_the_selected_track(repo: Repo, clock: FakeClock) -> None:
    version = "profile-v1"
    add_assessed(repo, "ai", version, "ai_backend", 95)
    add_assessed(repo, "web", version, "full_stack", 85)
    tz = settings().config.notify.tz

    found = views.all_jobs(
        repo,
        tz,
        "jobs",
        "",
        1,
        clock(),
        filters=views.Filters(track="full_stack", technical_min=80),
        track_version=version,
    )
    assert [card.id for card in found.cards] == ["web"]


def test_location_work_mode_publication_and_status_filters_still_combine(
    repo: Repo, clock: FakeClock
) -> None:
    version = "profile-v1"
    add_assessed(
        repo,
        "fresh",
        version,
        "sde",
        80,
        locations=["Pune, India"],
        work_mode="hybrid",
        published_on="2026-10-07",
    )
    add_assessed(
        repo,
        "old",
        version,
        "sde",
        90,
        locations=["Bengaluru, India"],
        work_mode="onsite",
        published_on="2026-08-01",
        status=JobStatus.HIDDEN,
    )
    found = views.all_jobs(
        repo,
        settings().config.notify.tz,
        "jobs",
        "",
        1,
        clock(),
        filters=views.Filters(place="Pune", work_mode="hybrid", published_within=3),
        track_version=version,
    )
    assert [card.id for card in found.cards] == ["fresh"]
    assert found.cards[0].work_mode == "Hybrid" and found.cards[0].published == "7 Oct"


def test_missing_and_stale_track_results_are_explicit(repo: Repo, clock: FakeClock) -> None:
    add(repo, "partial")
    partial_result = result("sde", 75)
    repo.save_track_assessment(
        "partial",
        "profile-v1",
        partial_result.tracks["sde"],
        partial_result.eligibility,
    )
    page = views.all_jobs(
        repo, settings().config.notify.tz, "jobs", "", 1, clock(), track_version="profile-v1"
    )
    match = page.cards[0].track_match
    assert match is not None and match.state == "partial"
    assert "Only 1 of 4" in match.message

    detail = views.job_detail(
        repo, "partial", settings().config.notify.tz, clock(), track_version="profile-v2"
    )
    assert detail is not None and detail.track_assessments is not None
    assert detail.track_assessments.state == "stale"
    assert detail.track_assessments.stored_versions == ["profile-v1"]

    filtered = views.all_jobs(
        repo,
        settings().config.notify.tz,
        "jobs",
        "",
        1,
        clock(),
        filters=views.Filters(track="sde"),
        track_version="profile-v2",
    )
    assert filtered.cards == []


def test_detail_explains_tracks_eligibility_and_resume(repo: Repo, clock: FakeClock) -> None:
    version = "profile-v1"
    add_assessed(
        repo,
        "detail",
        version,
        "ai_backend",
        88,
        state=EligibilityState.NEEDS_VERIFICATION,
        skills_required=["Python", "Kafka"],
        skills_preferred=["FastAPI"],
    )

    detail = views.job_detail(
        repo, "detail", settings().config.notify.tz, clock(), track_version=version
    )
    assert detail is not None and detail.track_assessments is not None
    tracks = {track.track: track for track in detail.track_assessments.tracks}
    ai = tracks["ai_backend"]
    assert ai.score == 88 and ai.matched_required == ["Python"]
    assert ai.missing_required == ["Kafka"]
    assert ai.preferred_skills == ["FastAPI"] and ai.matched_preferred == ["FastAPI"]
    assert ai.projects == ["Nexus AI"]
    assert ai.internships == ["AI Backend Engineer Intern"]
    assert ai.resume == "ResumeAI.pdf"
    assert detail.track_assessments.eligibility is not None
    assert detail.track_assessments.eligibility.label == "Needs verification"
    assert detail.track_assessments.eligibility.unresolved == ["Confirm the graduation window"]


def test_track_filtered_pagination_has_no_duplicate_cards(repo: Repo, clock: FakeClock) -> None:
    version = "profile-v1"
    for number in range(51):
        job_id = f"job-{number:02}"
        add(repo, job_id)
        one = result("sde", 75)
        repo.save_track_assessment(job_id, version, one.tracks["sde"], one.eligibility)
        clock.advance(1)

    first = views.all_jobs(
        repo,
        settings().config.notify.tz,
        "jobs",
        "",
        1,
        clock(),
        filters=views.Filters(track="sde"),
        track_version=version,
    )
    second = views.all_jobs(
        repo,
        settings().config.notify.tz,
        "jobs",
        "",
        2,
        clock(),
        filters=views.Filters(track="sde"),
        track_version=version,
    )
    ids = [card.id for card in first.cards + second.cards]
    assert (first.total, first.pages, len(first.cards), len(second.cards)) == (51, 2, 50, 1)
    assert len(ids) == len(set(ids)) == 51


def test_dashboard_renders_current_results_and_legacy_empty_states(
    repo: Repo, clock: FakeClock, tmp_path: Path
) -> None:
    configured, profile = web_settings(tmp_path)
    version = track_profile_version(profile, clock().astimezone(configured.config.notify.tz).date())
    assert version is not None
    add_assessed(
        repo,
        "current",
        version,
        "ai_backend",
        88,
        state=EligibilityState.NEEDS_VERIFICATION,
        skills_required=["Python", "Kafka"],
        skills_preferred=["FastAPI"],
    )
    add(repo, "legacy", company="Legacy Co")
    client = TestClient(create_app(configured, repo, clock), base_url="http://127.0.0.1:8080")

    jobs = client.get("/jobs", params={"track": "ai_backend"})
    assert jobs.status_code == 200
    assert jobs.text.count('data-id="current"') == 1
    assert "Technical fit 88" in jobs.text and "Needs verification" in jobs.text
    assert version in jobs.text and "Legacy Co" not in jobs.text

    detail = client.get("/jobs/current")
    assert detail.status_code == 200
    assert "Four-track technical fit" in detail.text
    assert "Why there is no score" in detail.text
    assert "ResumeAI.pdf" in detail.text
    assert "Confirm the graduation window" in detail.text

    empty = client.get("/jobs", params={"track": "sdet", "technical": 90})
    assert empty.status_code == 200 and "No jobs match" in empty.text
    legacy = client.get("/jobs/legacy")
    assert legacy.status_code == 200
    assert "not been assessed for the active profile version" in legacy.text


def test_stripe_without_track_rows_stays_unscored_and_visible(
    repo: Repo, clock: FakeClock, tmp_path: Path
) -> None:
    configured, profile = web_settings(tmp_path)
    version = track_profile_version(profile, date(2026, 10, 8))
    assert version is not None
    stripe_id = "e8d341de03bfcce221e34da1f7623b2456c115c1"
    add(
        repo,
        stripe_id,
        company="Stripe",
        role="Software Engineer, Intern",
        score=None,
        locations=["Bengaluru, India"],
        work_mode="onsite",
    )
    client = TestClient(create_app(configured, repo, clock), base_url="http://127.0.0.1:8080")

    page = client.get(f"/jobs/{stripe_id}")
    assert page.status_code == 200
    assert "Stripe" in page.text and "Software Engineer, Intern" in page.text
    assert "not been assessed for the active profile version" in page.text
    assert repo.track_matches_for_job(stripe_id) == []
    job = repo.get_job(stripe_id)
    assert job is not None and job.score is None
