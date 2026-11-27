import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from jobradar.db.models import JobStatus
from jobradar.db.repo import Repo, backup_database, init_db, make_engine
from jobradar.pipeline.eligibility import EligibilityResult, EligibilityState
from jobradar.pipeline.technical_match import FourTrackAssessment, TrackAssessment
from jobradar.profile import TRACK_NAMES, TrackName


def seed_job(repo: Repo, job_id: str = "job1") -> None:
    repo.get_or_create_job(job_id, f"https://example.com/jobs/{job_id}")


def eligibility(state: EligibilityState = EligibilityState.ELIGIBLE) -> EligibilityResult:
    return EligibilityResult(state=state, checks=[])


def assessment(
    track: TrackName,
    score: int | None = 80,
    coverage: float = 0.9,
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
        unscored_reason=None if score is not None else "insufficient evidence",
        evidence_coverage=coverage,
        matched_skills=["Python"],
        missing_required_skills=["Kafka"],
        relevant_projects=["Nexus AI"],
        relevant_internships=["AI Backend Engineer Intern"],
        evidence_quality={"resume_supported": 3},
        explanation=f"{track} assessment at {score}",
        recommended_resume=resume,
        components={},
    )


def four_tracks(
    *, state: EligibilityState = EligibilityState.ELIGIBLE, score: int = 80
) -> FourTrackAssessment:
    return FourTrackAssessment(
        eligibility=eligibility(state),
        tracks={track: assessment(track, score) for track in TRACK_NAMES},
    )


def test_new_database_has_additive_track_table_and_constraints(engine: Engine) -> None:
    with engine.connect() as c:
        columns = c.execute(text("PRAGMA table_info('job_track_match')")).all()
        names = [row[1] for row in columns]
        primary_key = {row[1]: row[5] for row in columns if row[5]}
        indexes = {row[1] for row in c.execute(text("PRAGMA index_list('job_track_match')"))}
        sql = c.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='job_track_match'")
        ).scalar_one()
    assert names == [
        "job_id",
        "profile_version",
        "track",
        "technical_score",
        "evidence_coverage",
        "eligibility_status",
        "recommended_resume",
        "explanation",
        "assessment_json",
        "eligibility_json",
        "created_at",
        "updated_at",
    ]
    assert primary_key == {"job_id": 1, "profile_version": 2, "track": 3}
    assert {"job_track_match_track_score", "job_track_match_profile_version"} <= indexes
    assert "technical_score BETWEEN 0 AND 100" in sql
    assert "evidence_coverage BETWEEN 0 AND 1" in sql


def test_existing_database_migration_preserves_stripe_and_legacy_match(
    tmp_path: Path,
) -> None:
    path = tmp_path / "existing.db"
    engine = make_engine(path)
    init_db(engine)
    stripe_id = "e8d341de03bfcce221e34da1f7623b2456c115c1"
    stripe_data = json.dumps({"company": "Stripe", "role": "Software Engineer, Intern"})
    with engine.begin() as c:
        c.execute(text("DROP TABLE job_track_match"))
        c.execute(
            text(
                "INSERT INTO job (id, canonical_url, data_json, score, score_reason, status, "
                "created_at, updated_at) VALUES (:id, :url, :data, NULL, 'not matched', "
                "'new', 'before', 'before')"
            ),
            {"id": stripe_id, "url": "https://stripe.example/8031833", "data": stripe_data},
        )
        c.execute(
            text(
                "INSERT INTO match_result (job_id, profile_version, provider, answers_json, "
                "created_at) VALUES (:id, 'legacy-v1', 'skills_rule', :answers, 'before')"
            ),
            {"id": stripe_id, "answers": '{"provider":"skills_rule"}'},
        )
    before_hash = hashlib.sha256(stripe_data.encode()).hexdigest()
    engine.dispose()

    engine = make_engine(path)
    init_db(engine)
    init_db(engine)
    with engine.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='job_track_match'"
                )
            ).scalar_one()
            == 1
        )
        stripe = c.execute(
            text("SELECT data_json, score, score_reason, status FROM job WHERE id=:id"),
            {"id": stripe_id},
        ).one()
        legacy = c.execute(
            text(
                "SELECT provider, answers_json, created_at FROM match_result "
                "WHERE job_id=:id AND profile_version='legacy-v1'"
            ),
            {"id": stripe_id},
        ).one()
        assert c.execute(text("SELECT count(*) FROM job_track_match")).scalar_one() == 0
        assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    assert hashlib.sha256(stripe.data_json.encode()).hexdigest() == before_hash
    assert (stripe.score, stripe.score_reason, stripe.status) == (None, "not matched", "new")
    assert tuple(legacy) == ("skills_rule", '{"provider":"skills_rule"}', "before")
    engine.dispose()


def test_save_one_track_keeps_technical_and_eligibility_payloads_separate(repo: Repo) -> None:
    seed_job(repo)
    item = assessment("ai_backend", 92, 0.85)
    result = eligibility(EligibilityState.NEEDS_VERIFICATION)
    repo.save_track_assessment("job1", "profile-v1", item, result)

    stored = repo.get_track_match("job1", "profile-v1", "ai_backend")
    assert stored is not None
    assert (stored.technical_score, stored.evidence_coverage) == (92, 0.85)
    assert stored.eligibility_status == "NEEDS_VERIFICATION"
    assert stored.recommended_resume == "ResumeAI.pdf"
    assert json.loads(stored.assessment_json)["matched_skills"] == ["Python"]
    assert json.loads(stored.eligibility_json)["state"] == "NEEDS_VERIFICATION"


def test_older_job_without_structured_evidence_can_persist_unscored(repo: Repo) -> None:
    seed_job(repo)
    item = assessment("sde", score=None, coverage=0.3)
    repo.save_track_assessment(
        "job1",
        "profile-v1",
        item,
        eligibility(EligibilityState.NEEDS_VERIFICATION),
    )
    stored = repo.get_track_match("job1", "profile-v1", "sde")
    assert stored is not None and stored.technical_score is None
    assert stored.evidence_coverage == 0.3
    assert json.loads(stored.assessment_json)["status"] == "unscored"


def test_one_job_persists_four_tracks_without_duplicate_job(repo: Repo) -> None:
    seed_job(repo)
    assert repo.save_track_assessments("job1", "profile-v1", four_tracks()) == 4
    rows = repo.track_matches_for_job("job1", "profile-v1")
    assert [row.track for row in rows] == sorted(TRACK_NAMES)
    with repo.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM job")).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM job_track_match")).scalar_one() == 4


def test_duplicate_upsert_updates_in_place_and_keeps_created_at(repo: Repo, clock: Any) -> None:
    seed_job(repo)
    repo.save_track_assessment("job1", "profile-v1", assessment("sde", 70), eligibility())
    first = repo.get_track_match("job1", "profile-v1", "sde")
    assert first is not None
    clock.advance(60)
    repo.save_track_assessment("job1", "profile-v1", assessment("sde", 91), eligibility())
    updated = repo.get_track_match("job1", "profile-v1", "sde")
    assert updated is not None
    assert updated.technical_score == 91
    assert updated.created_at == first.created_at
    assert updated.updated_at != first.updated_at
    assert len(repo.track_matches_for_job("job1", "profile-v1")) == 1


def test_profile_versions_preserve_history(repo: Repo) -> None:
    seed_job(repo)
    repo.save_track_assessments("job1", "profile-v1", four_tracks(score=75))
    repo.save_track_assessments("job1", "profile-v2", four_tracks(score=88))
    assert len(repo.track_matches_for_job("job1")) == 8
    assert {row.technical_score for row in repo.track_matches_for_version("profile-v1")} == {75}
    assert {row.technical_score for row in repo.track_matches_for_version("profile-v2")} == {88}


def test_jobs_to_track_match_requires_a_complete_versioned_set(repo: Repo) -> None:
    seed_job(repo)
    repo.set_job_status("job1", JobStatus.EXTRACTED)
    result = four_tracks()
    assert repo.jobs_to_track_match("profile-v1") == ["job1"]

    repo.save_track_assessment(
        "job1", "profile-v1", result.tracks["ai_backend"], result.eligibility
    )
    assert repo.jobs_to_track_match("profile-v1") == ["job1"]

    repo.save_track_assessments("job1", "profile-v1", result)
    assert repo.jobs_to_track_match("profile-v1") == []
    assert repo.jobs_to_track_match("profile-v2") == ["job1"]


def test_retrieval_by_job_track_and_version_and_missing_results(repo: Repo) -> None:
    seed_job(repo, "job1")
    seed_job(repo, "job2")
    repo.save_track_assessments("job1", "profile-v1", four_tracks())
    repo.save_track_assessment("job2", "profile-v1", assessment("sde", 65), eligibility())

    assert len(repo.track_matches_for_job("job1", "profile-v1")) == 4
    assert [(row.job_id, row.track) for row in repo.track_matches_for_track("sde")] == [
        ("job1", "sde"),
        ("job2", "sde"),
    ]
    assert len(repo.track_matches_for_version("profile-v1")) == 5
    assert repo.get_track_match("job2", "profile-v1", "ai_backend") is None
    assert repo.track_matches_for_track("sdet", "missing-version") == []


def test_four_track_save_rolls_back_if_any_row_is_invalid(repo: Repo) -> None:
    seed_job(repo)
    valid = assessment("ai_backend")
    invalid = TrackAssessment.model_construct(
        track=cast(Any, "invalid"),
        status="scored",
        score=80,
        unscored_reason=None,
        evidence_coverage=0.9,
        matched_skills=[],
        missing_required_skills=[],
        relevant_projects=[],
        relevant_internships=[],
        relevant_experience=[],
        evidence_quality={},
        explanation="invalid track for rollback test",
        recommended_resume=None,
        components={},
    )
    result = FourTrackAssessment.model_construct(
        eligibility=eligibility(),
        tracks={"ai_backend": valid, "sde": invalid},
    )
    with pytest.raises(IntegrityError):
        repo.save_track_assessments("job1", "profile-v1", result)
    assert repo.track_matches_for_job("job1") == []


def test_track_rows_do_not_change_legacy_match_result(repo: Repo) -> None:
    seed_job(repo)
    answers = '{"provider":"skills_rule","skills_coverage":0.5}'
    repo.save_match("job1", "legacy-v1", "skills_rule", answers)
    repo.save_track_assessments("job1", "profile-v2", four_tracks())
    with repo.engine.connect() as c:
        row = c.execute(
            text(
                "SELECT provider, answers_json FROM match_result "
                "WHERE job_id='job1' AND profile_version='legacy-v1'"
            )
        ).one()
        job = c.execute(text("SELECT score, score_reason FROM job WHERE id='job1'")).one()
    assert tuple(row) == ("skills_rule", answers)
    assert tuple(job) == (None, None)


def test_database_backup_is_consistent_and_refuses_overwrite(
    engine: Engine, repo: Repo, tmp_path: Path
) -> None:
    seed_job(repo)
    repo.save_track_assessments("job1", "profile-v1", four_tracks())
    destination = tmp_path / "backup" / "before-migration.db"
    assert backup_database(engine, destination) == destination
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert len(digest) == 64
    backup = sqlite3.connect(destination)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT count(*) FROM job").fetchone()[0] == 1
        assert backup.execute("SELECT count(*) FROM job_track_match").fetchone()[0] == 4
    finally:
        backup.close()
    with pytest.raises(FileExistsError):
        backup_database(engine, destination)


def test_constraints_reject_invalid_score_coverage_and_track(repo: Repo) -> None:
    seed_job(repo)
    base = {
        "job": "job1",
        "version": "v1",
        "eligibility": "ELIGIBLE",
        "assessment": "{}",
        "eligibility_json": "{}",
        "now": "now",
    }
    sql = text(
        "INSERT INTO job_track_match (job_id, profile_version, track, technical_score, "
        "evidence_coverage, eligibility_status, explanation, assessment_json, "
        "eligibility_json, created_at, updated_at) VALUES (:job, :version, :track, :score, "
        ":coverage, :eligibility, 'x', :assessment, :eligibility_json, :now, :now)"
    )
    bad = [
        {"track": "mobile", "score": 50, "coverage": 0.8},
        {"track": "sde", "score": 101, "coverage": 0.8},
        {"track": "sde", "score": 50, "coverage": 1.1},
    ]
    for values in bad:
        with pytest.raises(IntegrityError), repo.engine.begin() as c:
            c.execute(sql, base | values)


def test_retrieval_models_round_trip_exact_track_type(repo: Repo) -> None:
    seed_job(repo)
    repo.save_track_assessment("job1", "v1", assessment("full_stack"), eligibility())
    stored = repo.get_track_match("job1", "v1", "full_stack")
    assert stored is not None
    assert cast(TrackName, stored.track) == "full_stack"
