from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.pipeline.leads import LeadContact, LeadEnrichment, prioritize_lead
from jobradar.pipeline.technical_match import track_profile_version
from jobradar.web import views
from jobradar.web.app import create_app
from tests.conftest import FakeClock
from tests.test_track_dashboard import add_assessed, web_settings


def enrichment(
    *,
    priority: str = "review",
    vacancy: str = "active",
    eligibility: str = "NEEDS_VERIFICATION",
    score: int | None = 72,
) -> LeadEnrichment:
    return LeadEnrichment.model_validate(
        {
            "primary_track": "sde",
            "technical_score": score,
            "vacancy_status": vacancy,
            "eligibility_status": eligibility,
            "application_priority": priority,
            "official_application_url": "https://jobs.example.com/123",
            "official_source_url": "https://boards.example.com/jobs/123",
            "last_verified_at": "2026-11-17T16:42:54Z",
            "publication_date": "2026-10-01",
            "location": "Bengaluru, India",
            "work_mode": "onsite",
            "internship_duration": "January to June 2027",
            "eligibility_evidence": ["The official page describes a paid internship."],
            "unresolved": ["The graduation window is not stated."],
            "priority_reasons": ["A critical eligibility condition still needs verification."],
            "recommended_resume": "Resume_bharadwaj.pdf",
            "contacts": [
                {
                    "kind": "official_route",
                    "position": "Internship questions",
                    "company": "Example",
                    "public_work_email": "internships@example.com",
                    "source_url": "https://jobs.example.com/123",
                    "verified_on": "2026-11-17",
                    "confidence": "high",
                }
            ],
            "application_strategy": "Verify the graduation window before applying.",
            "outreach_draft": "Hello, could you confirm the graduation-year requirement?",
        }
    )


def test_lead_models_accept_only_evidence_backed_public_routes() -> None:
    item = enrichment()
    assert item.contacts[0].public_work_email == "internships@example.com"
    assert item.technical_score == 72

    with pytest.raises(ValidationError):
        LeadContact(
            kind="official_route",
            position="Questions",
            company="Example",
            source_url="not-a-public-url",
            verified_on=date(2026, 11, 17),
            confidence="high",
        )
    with pytest.raises(ValidationError):
        LeadEnrichment.model_validate(
            enrichment().model_dump() | {"official_application_url": "javascript:alert(1)"}
        )


@pytest.mark.parametrize(
    ("vacancy", "eligibility", "score", "url", "expected"),
    [
        ("expired", "ELIGIBLE", 90, True, "exclude"),
        ("active", "NOT_ELIGIBLE", 90, True, "exclude"),
        ("active", "NEEDS_VERIFICATION", 90, True, "review"),
        ("unknown", "ELIGIBLE", 90, True, "review"),
        ("active", "ELIGIBLE", 80, True, "apply_now"),
        ("active", "ELIGIBLE", 57, True, "low"),
        ("active", "ELIGIBLE", None, True, "review"),
        ("active", "ELIGIBLE", 90, False, "review"),
    ],
)
def test_priority_keeps_technical_fit_eligibility_and_freshness_separate(
    vacancy: str, eligibility: str, score: int | None, url: bool, expected: str
) -> None:
    priority, reasons = prioritize_lead(vacancy, eligibility, score, url)  # type: ignore[arg-type]
    assert priority == expected
    assert reasons


def test_job_lead_schema_is_additive_and_migrates_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    engine = make_engine(path)
    init_db(engine)
    repo = Repo(engine)
    repo.get_or_create_job("stripe", "https://stripe.example/8031833")
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE job_lead"))
    engine.dispose()

    engine = make_engine(path)
    init_db(engine)
    with engine.connect() as connection:
        columns = [row[1] for row in connection.execute(text("PRAGMA table_info('job_lead')"))]
        assert columns == [
            "job_id",
            "profile_version",
            "primary_track",
            "vacancy_status",
            "eligibility_status",
            "application_priority",
            "official_application_url",
            "last_verified_at",
            "enrichment_json",
            "created_at",
            "updated_at",
        ]
        assert connection.execute(text("SELECT count(*) FROM job")).scalar_one() == 1
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    engine.dispose()


def test_lead_upsert_is_one_row_per_job_and_retrievable(repo: Repo, clock: FakeClock) -> None:
    repo.get_or_create_job("job1", "https://jobs.example.com/123")
    first = enrichment()
    repo.save_job_lead("job1", "profile-v1", first)
    clock.advance(1)
    updated = enrichment(priority="apply_now", eligibility="ELIGIBLE", score=82)
    repo.save_job_lead("job1", "profile-v2", updated)

    row = repo.get_job_lead("job1")
    assert row is not None
    assert (row.profile_version, row.application_priority) == ("profile-v2", "apply_now")
    assert LeadEnrichment.model_validate_json(row.enrichment_json) == updated
    assert repo.leads_for_jobs(["job1"]) == [row]
    assert repo.leads_by_priority("apply_now", "profile-v2") == [row]
    with repo.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM job_lead")).scalar_one() == 1


def test_priority_filter_and_dashboard_enrichment_are_profile_versioned(
    repo: Repo, clock: FakeClock, tmp_path: Path
) -> None:
    configured, profile = web_settings(tmp_path)
    version = track_profile_version(profile, clock().astimezone(configured.config.notify.tz).date())
    assert version is not None
    add_assessed(repo, "review", version, "sde", 72, company="HackerRank")
    add_assessed(repo, "other", version, "sde", 80, company="Other")
    repo.save_job_lead("review", version, enrichment())
    repo.save_job_lead(
        "other", "older-profile", enrichment(priority="apply_now", eligibility="ELIGIBLE")
    )

    found = views.all_jobs(
        repo,
        configured.config.notify.tz,
        "jobs",
        "",
        1,
        clock(),
        filters=views.Filters(priority="review"),
        track_version=version,
    )
    assert [card.id for card in found.cards] == ["review"]
    assert found.cards[0].lead is not None and found.cards[0].lead.state == "current"

    stale = views.job_detail(
        repo, "other", configured.config.notify.tz, clock(), track_version=version
    )
    assert stale is not None and stale.lead is not None and stale.lead.state == "stale"

    client = TestClient(create_app(configured, repo, clock), base_url="http://127.0.0.1:8080")
    listing = client.get("/jobs", params={"priority": "review"})
    assert listing.status_code == 200
    assert listing.text.count('data-id="review"') == 1
    assert "Application priority" in listing.text and "Vacancy active" in listing.text
    detail = client.get("/jobs/review")
    assert detail.status_code == 200
    assert "Application lead" in detail.text
    assert "Reviewed track" in detail.text and "SDE" in detail.text
    assert "Needs manual verification" in detail.text
    assert "Official application" in detail.text
    assert "internships@example.com" in detail.text


def test_lead_verification_timestamp_round_trips() -> None:
    item = enrichment()
    assert item.last_verified_at == datetime(2026, 11, 17, 16, 42, 54, tzinfo=UTC)
