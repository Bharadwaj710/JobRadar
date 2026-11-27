"""Tests for Stage 3B: Legacy Score & Application Data Migration.

Verifies:
1. Legacy score preservation without J4 technical_score contamination.
2. Correct owner candidate association and candidate isolation.
3. Every application status mapping (shortlisted, resume_ready, applied, interview, offer, rejected, skipped).
4. Resume Artifact relationship preservation and notes generation.
5. Status change audit history migration.
6. Timestamp preservation (user_status_at, applied_at, changed_at).
7. Deduplication when a matching CandidateApplication already exists.
8. Shared job lifecycle preservation (hidden, closed, discarded jobs are never reset).
9. Idempotent migration when run repeatedly.
10. Transactional rollback after injected failure.
11. Dry-run audit accuracy.
12. Ambiguous records flagged for manual review.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import (
    Artifact,
    Candidate,
    CandidateApplication,
    CandidateApplicationStatus,
    Job,
    JobStatus,
    MatchResult,
    StatusChange,
)
from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.services.migration_service import LegacyMigrationService
from tests.conftest import FakeClock
from tests.test_digest import settings


@pytest.fixture
def test_db(tmp_path: Path, clock: FakeClock) -> tuple[Repo, Settings, Candidate]:
    db_path = tmp_path / "stage3b_test.db"
    eng = make_engine(db_path)
    init_db(eng)
    repo = Repo(eng, clock=clock)

    owner = repo.create_candidate(
        id="cand_owner_3b", display_name="Owner 3B", email="owner3b@jobradar.local"
    )
    repo.save_profile_draft(owner.id, {"skills": ["Python", "FastAPI"]})
    prof = repo.get_candidate_profiles(owner.id)[0]
    repo.confirm_profile(owner.id, prof.id, {"skills": ["Python", "FastAPI"]})

    yaml_file = tmp_path / "owner_profile.yaml"
    yaml_file.write_text(
        "basics:\n  name: Owner 3B\n  email: owner3b@jobradar.local\nskills:\n  additional: [Python]\n",
        encoding="utf-8",
    )

    base_cfg = settings().config.model_dump()
    base_cfg["matching"]["profile_path"] = str(yaml_file)
    base_cfg["owner_candidate_id"] = owner.id
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="owner3b@jobradar.local"),
    )
    return repo, st, owner


def test_legacy_score_migration_preserves_score_without_j4_contamination(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """1. Legacy job.score is migrated to CandidateHistoricalAssessment without touching J4 scores."""
    repo, st, owner = test_db
    job, _ = repo.get_or_create_job("j_score_1", "https://example.com/j1")

    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.score = 88
        db_job.score_reason = "skills only · 88 · Python ✓"
        s.add(db_job)
        s.commit()

    service = LegacyMigrationService(repo, st)
    result = service.migrate(dry_run=False)

    assert result.success is True
    assert result.scores_migrated == 1

    # Verify CandidateHistoricalAssessment record
    hist = repo.get_historical_assessment(owner.id, job.id)
    assert hist is not None
    assert hist.score == 88
    assert hist.score_reason == "skills only · 88 · Python ✓"
    assert hist.scoring_system == "legacy_matcher"
    assert hist.candidate_id == owner.id
    assert hist.job_id == job.id
    # Crucial: rule score has no genuine timestamp, so scored_at must remain NULL
    assert hist.scored_at is None

    # Verify NO J4 CandidateJobAssessment records were created or contaminated
    j4_assessments = repo.list_candidate_assessments(owner.id, job_id=job.id)
    assert len(j4_assessments) == 0

    # Verify shared job record still holds its original columns
    refreshed_job = repo.get_job(job.id)
    assert refreshed_job is not None
    assert refreshed_job.score == 88
    assert refreshed_job.score_reason == "skills only · 88 · Python ✓"


def test_genuine_score_timestamp_preserved_from_match_result(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """1b. Genuine score timestamp from MatchResult is preserved; rule score has scored_at=NULL."""
    repo, st, owner = test_db

    # Job 1 has genuine MatchResult timestamp
    j1, _ = repo.get_or_create_job("j_mr_1", "https://example.com/mr1")
    # Job 2 is rule-based only (no MatchResult)
    j2, _ = repo.get_or_create_job("j_rule_2", "https://example.com/rule2")

    with Session(repo.engine) as s:
        db_j1 = s.get(Job, j1.id)
        assert db_j1 is not None
        db_j1.score = 80
        db_j1.score_reason = "LLM evaluated"
        db_j1.user_status_at = "2026-11-25T12:00:00Z"
        db_j1.updated_at = "2026-11-25T14:00:00Z"
        s.add(db_j1)

        mr = MatchResult(
            job_id=j1.id,
            profile_version="v5",
            provider="gemini",
            answers_json='{"score": 80, "provider": "gemini"}',
            created_at="2026-11-19T08:30:00Z",
        )
        s.add(mr)

        db_j2 = s.get(Job, j2.id)
        assert db_j2 is not None
        db_j2.score = 70
        db_j2.score_reason = "skills only · Python"
        db_j2.user_status_at = "2026-11-24T12:00:00Z"
        db_j2.updated_at = "2026-11-24T15:00:00Z"
        s.add(db_j2)
        s.commit()

    service = LegacyMigrationService(repo, st)
    res = service.migrate(dry_run=False)
    assert res.success is True

    hist1 = repo.get_historical_assessment(owner.id, j1.id)
    assert hist1 is not None
    # Genuine timestamp from MatchResult is preserved
    assert hist1.scored_at == "2026-11-19T08:30:00Z"

    hist2 = repo.get_historical_assessment(owner.id, j2.id)
    assert hist2 is not None
    # Unknown timestamp remains strictly NULL; never substituted with user_status_at or updated_at
    assert hist2.scored_at is None


def test_unrelated_or_mismatched_match_result_yields_null_scored_at(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """1c. MatchResult from unrelated provider or with mismatched score must NOT be assigned."""
    repo, st, owner = test_db

    # Job 1: skills only score, but MatchResult provider is 'rules' (hard rule rejection)
    j1, _ = repo.get_or_create_job("j_unrel_1", "https://example.com/unrel1")
    # Job 2: skills only score (50), but MatchResult has skills_coverage=0.1 (score=10 mismatch)
    j2, _ = repo.get_or_create_job("j_mismatch_2", "https://example.com/mismatch2")
    # Job 3: skills only score (60), MatchResult has provider='skills_rule' and coverage=0.60 (proven)
    j3, _ = repo.get_or_create_job("j_proven_3", "https://example.com/proven3")

    with Session(repo.engine) as s:
        db_j1 = s.get(Job, j1.id)
        assert db_j1 is not None
        db_j1.score = 50
        db_j1.score_reason = "skills only · 50 · Python ✓"
        s.add(db_j1)
        s.add(
            MatchResult(
                job_id=j1.id,
                profile_version="v5",
                provider="rules",
                answers_json='{"eligible": 0.0}',
                created_at="2026-11-20T10:00:00Z",
            )
        )

        db_j2 = s.get(Job, j2.id)
        assert db_j2 is not None
        db_j2.score = 50
        db_j2.score_reason = "skills only · 50 · Python ✓"
        s.add(db_j2)
        s.add(
            MatchResult(
                job_id=j2.id,
                profile_version="v5",
                provider="skills_rule",
                answers_json='{"skills_coverage": 0.1}',
                created_at="2026-11-20T11:00:00Z",
            )
        )

        db_j3 = s.get(Job, j3.id)
        assert db_j3 is not None
        db_j3.score = 60
        db_j3.score_reason = "skills only · 60 · Python, SQL ✓"
        s.add(db_j3)
        s.add(
            MatchResult(
                job_id=j3.id,
                profile_version="v5",
                provider="skills_rule",
                answers_json='{"skills_coverage": 0.6}',
                created_at="2026-11-20T12:00:00Z",
            )
        )
        s.commit()

    service = LegacyMigrationService(repo, st)
    res = service.migrate(dry_run=False)
    assert res.success is True

    # Job 1 has unrelated MatchResult ('rules'), so scored_at MUST remain NULL
    h1 = repo.get_historical_assessment(owner.id, j1.id)
    assert h1 is not None
    assert h1.score == 50
    assert h1.scored_at is None

    # Job 2 has mismatched coverage (0.1 vs 50), so scored_at MUST remain NULL
    h2 = repo.get_historical_assessment(owner.id, j2.id)
    assert h2 is not None
    assert h2.score == 50
    assert h2.scored_at is None

    # Job 3 has proven correspondence (skills_rule, coverage 0.6 == 60), so timestamp is preserved
    h3 = repo.get_historical_assessment(owner.id, j3.id)
    assert h3 is not None
    assert h3.score == 60
    assert h3.scored_at == "2026-11-20T12:00:00Z"


def test_conservative_llm_timestamp_provenance(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """1d. LLM MatchResult timestamp is assigned only with proven score correspondence."""
    repo, st, owner = test_db

    # 1. Unrelated / empty LLM MatchResult -> produces NULL
    j_unrelated, _ = repo.get_or_create_job("j_llm_unrel", "https://example.com/llm_unrel")
    # 2. Mismatched explicit LLM score (45 vs 80) -> produces NULL
    j_mismatched, _ = repo.get_or_create_job("j_llm_mismatch", "https://example.com/llm_mismatch")
    # 3. Mismatched dimension LLM score (evaluates to 50 vs 80) -> produces NULL
    j_mismatch_dims, _ = repo.get_or_create_job(
        "j_llm_dims_mis", "https://example.com/llm_dims_mis"
    )
    # 4. Genuinely corresponding explicit LLM score (85 == 85) -> preserves timestamp
    j_proven_exp, _ = repo.get_or_create_job("j_llm_prov_exp", "https://example.com/llm_prov_exp")
    # 5. Genuinely corresponding weighted dimension LLM score (80 == 80) -> preserves timestamp
    j_proven_dims, _ = repo.get_or_create_job(
        "j_llm_prov_dims", "https://example.com/llm_prov_dims"
    )

    with Session(repo.engine) as s:
        # Case 1: unrelated / uninformative answers_json
        db_unrel = s.get(Job, j_unrelated.id)
        assert db_unrel is not None
        db_unrel.score = 80
        db_unrel.score_reason = "Good fit · 80"
        s.add(db_unrel)
        s.add(
            MatchResult(
                job_id=j_unrelated.id,
                profile_version="v5",
                provider="gemini",
                answers_json='{"answers": []}',
                created_at="2026-11-20T08:30:00Z",
            )
        )

        # Case 2: mismatched explicit score
        db_mismatch = s.get(Job, j_mismatched.id)
        assert db_mismatch is not None
        db_mismatch.score = 80
        db_mismatch.score_reason = "Good fit · 80"
        s.add(db_mismatch)
        s.add(
            MatchResult(
                job_id=j_mismatched.id,
                profile_version="v5",
                provider="gemini",
                answers_json='{"score": 45, "provider": "gemini"}',
                created_at="2026-11-20T09:30:00Z",
            )
        )

        # Case 3: mismatched dimension score
        db_mis_dims = s.get(Job, j_mismatch_dims.id)
        assert db_mis_dims is not None
        db_mis_dims.score = 80
        db_mis_dims.score_reason = "Good fit · 80"
        s.add(db_mis_dims)
        s.add(
            MatchResult(
                job_id=j_mismatch_dims.id,
                profile_version="v5",
                provider="groq",
                answers_json='{"skills_coverage": 0.5, "role_alignment": 0.5, "seniority_fit": 0.5, "location_ok": 0.5}',
                created_at="2026-11-20T10:30:00Z",
            )
        )

        # Case 4: proven explicit score
        db_prov_exp = s.get(Job, j_proven_exp.id)
        assert db_prov_exp is not None
        db_prov_exp.score = 85
        db_prov_exp.score_reason = "Strong fit · 85 · eligible ✓"
        s.add(db_prov_exp)
        s.add(
            MatchResult(
                job_id=j_proven_exp.id,
                profile_version="v5",
                provider="gemini",
                answers_json='{"score": 85, "provider": "gemini"}',
                created_at="2026-11-20T11:30:00Z",
            )
        )

        # Case 5: proven weighted dimension score
        db_prov_dims = s.get(Job, j_proven_dims.id)
        assert db_prov_dims is not None
        db_prov_dims.score = 80
        db_prov_dims.score_reason = "Good fit · 80 · Python ✓"
        s.add(db_prov_dims)
        s.add(
            MatchResult(
                job_id=j_proven_dims.id,
                profile_version="v5",
                provider="llm",
                answers_json='{"skills_coverage": 0.8, "role_alignment": 0.8, "seniority_fit": 0.8, "location_ok": 0.8}',
                created_at="2026-11-20T12:30:00Z",
            )
        )

        s.commit()

    service = LegacyMigrationService(repo, st)
    res = service.migrate(dry_run=False)
    assert res.success is True
    assert res.scores_migrated == 5

    # Case 1: uninformative answers_json produces NULL
    h1 = repo.get_historical_assessment(owner.id, j_unrelated.id)
    assert h1 is not None
    assert h1.score == 80
    assert h1.score_reason == "Good fit · 80"
    assert h1.scored_at is None

    # Case 2: mismatched explicit score produces NULL
    h2 = repo.get_historical_assessment(owner.id, j_mismatched.id)
    assert h2 is not None
    assert h2.score == 80
    assert h2.score_reason == "Good fit · 80"
    assert h2.scored_at is None

    # Case 3: mismatched dimension score produces NULL
    h3 = repo.get_historical_assessment(owner.id, j_mismatch_dims.id)
    assert h3 is not None
    assert h3.score == 80
    assert h3.score_reason == "Good fit · 80"
    assert h3.scored_at is None

    # Case 4: proven explicit score preserves timestamp
    h4 = repo.get_historical_assessment(owner.id, j_proven_exp.id)
    assert h4 is not None
    assert h4.score == 85
    assert h4.score_reason == "Strong fit · 85 · eligible ✓"
    assert h4.scored_at == "2026-11-20T11:30:00Z"

    # Case 5: proven weighted dimension score preserves timestamp
    h5 = repo.get_historical_assessment(owner.id, j_proven_dims.id)
    assert h5 is not None
    assert h5.score == 80
    assert h5.score_reason == "Good fit · 80 · Python ✓"
    assert h5.scored_at == "2026-11-20T12:30:00Z"

    # Assert NO J4 assessments were created or modified
    for jid in [
        j_unrelated.id,
        j_mismatched.id,
        j_mismatch_dims.id,
        j_proven_exp.id,
        j_proven_dims.id,
    ]:
        assert len(repo.list_candidate_assessments(owner.id, job_id=jid)) == 0


def test_every_application_status_mapping(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """2. Every legacy user workflow status maps accurately to CandidateApplicationStatus."""
    repo, st, owner = test_db

    expected_mapping = {
        JobStatus.SHORTLISTED.value: CandidateApplicationStatus.SAVED.value,
        JobStatus.RESUME_READY.value: CandidateApplicationStatus.SAVED.value,
        JobStatus.APPLIED.value: CandidateApplicationStatus.APPLIED.value,
        JobStatus.INTERVIEW.value: CandidateApplicationStatus.INTERVIEWING.value,
        JobStatus.OFFER.value: CandidateApplicationStatus.OFFERED.value,
        JobStatus.REJECTED.value: CandidateApplicationStatus.REJECTED.value,
        JobStatus.SKIPPED.value: CandidateApplicationStatus.SKIPPED.value,
    }

    with Session(repo.engine) as s:
        for idx, (leg_st, _) in enumerate(expected_mapping.items()):
            jid = f"j_status_{idx}"
            j = Job(
                id=jid,
                canonical_url=f"https://example.com/{jid}",
                status=leg_st,
                user_status_at=f"2026-11-20T1{idx}:00:00Z",
                created_at="2026-11-20T10:00:00Z",
                updated_at="2026-11-20T10:00:00Z",
            )
            s.add(j)
        s.commit()

    service = LegacyMigrationService(repo, st)
    result = service.migrate(dry_run=False)

    assert result.success is True
    assert result.applications_migrated == len(expected_mapping)

    for idx, (_leg_st, exp_app_st) in enumerate(expected_mapping.items()):
        jid = f"j_status_{idx}"
        app = repo.get_candidate_application(owner.id, jid)
        assert app is not None
        assert app.status == exp_app_st
        assert app.candidate_id == owner.id
        assert app.job_id == jid


def test_resume_ready_artifact_association_and_notes(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """3. resume_ready jobs link associated resume Artifact in notes; Artifact records preserved."""
    repo, st, owner = test_db

    job, _ = repo.get_or_create_job("j_art_1", "https://example.com/art1")
    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.status = JobStatus.RESUME_READY.value
        db_job.user_status_at = "2026-11-21T12:00:00Z"
        s.add(db_job)
        art = Artifact(
            job_id="j_art_1",
            kind="resume",
            version=1,
            local_path="data/resumes/j_art_1_v1.pdf",
            created_at="2026-11-21T12:00:00Z",
        )
        s.add(art)
        s.commit()
        art_id = art.id

    service = LegacyMigrationService(repo, st)
    result = service.migrate(dry_run=False)

    assert result.success is True
    app = repo.get_candidate_application(owner.id, "j_art_1")
    assert app is not None
    assert app.status == CandidateApplicationStatus.SAVED.value
    assert "resume_ready" in app.notes
    assert "data/resumes/j_art_1_v1.pdf" in app.notes
    assert f"id={art_id}" in app.notes

    # Verify existing Artifact is preserved completely intact (never modified or deleted)
    with Session(repo.engine) as s:
        arts = s.exec(select(Artifact).where(Artifact.job_id == "j_art_1")).all()
        assert len(arts) == 1
        assert arts[0].id == art_id
        assert arts[0].version == 1
        assert arts[0].kind == "resume"
        assert arts[0].local_path == "data/resumes/j_art_1_v1.pdf"


def test_status_change_history_and_timestamps_migrated(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """4. status_change entries are preserved in CandidateApplicationHistory without inventions."""
    repo, st, owner = test_db

    with Session(repo.engine) as s:
        job = Job(
            id="j_hist_1",
            canonical_url="https://example.com/hist1",
            status=JobStatus.APPLIED.value,
            user_status_at="2026-11-22T15:00:00Z",
            created_at="2026-11-20T10:00:00Z",
            updated_at="2026-11-22T15:00:00Z",
        )
        s.add(job)
        sc1 = StatusChange(
            job_id="j_hist_1",
            status=JobStatus.SHORTLISTED.value,
            changed_at="2026-11-21T09:00:00Z",
        )
        sc2 = StatusChange(
            job_id="j_hist_1",
            status=JobStatus.APPLIED.value,
            changed_at="2026-11-22T15:00:00Z",
        )
        s.add(sc1)
        s.add(sc2)

        # Job with status='applied' but ZERO status_change rows
        j_no_sc = Job(
            id="j_no_sc",
            canonical_url="https://example.com/no_sc",
            status=JobStatus.APPLIED.value,
            user_status_at="2026-11-23T10:00:00Z",
            created_at="2026-11-20T10:00:00Z",
            updated_at="2026-11-23T10:00:00Z",
        )
        s.add(j_no_sc)
        s.commit()

    service = LegacyMigrationService(repo, st)
    result = service.migrate(dry_run=False)

    assert result.success is True
    app = repo.get_candidate_application(owner.id, "j_hist_1")
    assert app is not None
    assert app.status == CandidateApplicationStatus.APPLIED.value
    assert app.applied_at == "2026-11-22T15:00:00Z"

    history = repo.list_candidate_application_history(owner.id, "j_hist_1")
    assert len(history) == 2
    # list_candidate_application_history orders descending (most recent first)
    assert history[0].status == CandidateApplicationStatus.APPLIED.value
    assert history[0].changed_at == "2026-11-22T15:00:00Z"
    assert history[1].status == CandidateApplicationStatus.SAVED.value
    assert history[1].changed_at == "2026-11-21T09:00:00Z"

    # Confirm no historical transitions were invented for the job without status_change
    history_no_sc = repo.list_candidate_application_history(owner.id, "j_no_sc")
    assert len(history_no_sc) == 0

    # Confirm re-running migration preserves history without duplicate entries
    res_repeat = service.migrate(dry_run=False)
    assert res_repeat.success is True
    history_repeat = repo.list_candidate_application_history(owner.id, "j_hist_1")
    assert len(history_repeat) == 2


def test_existing_candidate_application_deduplicated_and_idempotent(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """5. Existing CandidateApplication is never overwritten; notes and timestamps remain."""
    repo, st, owner = test_db

    job, _ = repo.get_or_create_job("j_dedup_1", "https://example.com/dedup1")
    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.status = JobStatus.APPLIED.value
        db_job.score = 92
        db_job.user_status_at = "2026-11-23T10:00:00Z"
        s.add(db_job)
        s.commit()

    # Pre-existing candidate application with custom notes, status, and timestamps
    pre_app = repo.save_candidate_application(
        owner.id,
        job.id,
        status="interviewing",
        notes="Pre-existing note from candidate tracker",
    )
    pre_created_at = pre_app.created_at
    pre_updated_at = pre_app.updated_at

    service = LegacyMigrationService(repo, st)

    # First migration run
    res1 = service.migrate(dry_run=False)
    assert res1.success is True
    assert res1.scores_migrated == 1
    assert res1.applications_migrated == 0  # Already exists -> deduplicated/preserved
    assert res1.applications_skipped == 1

    app1 = repo.get_candidate_application(owner.id, job.id)
    assert app1 is not None
    # Existing record was NOT overwritten by legacy status='applied' or empty notes
    assert app1.status == "interviewing"
    assert app1.notes == "Pre-existing note from candidate tracker"
    assert app1.created_at == pre_created_at
    assert app1.updated_at == pre_updated_at

    # Second migration run (idempotency verification)
    res2 = service.migrate(dry_run=False)
    assert res2.success is True
    assert res2.scores_migrated == 0
    assert res2.scores_skipped == 1
    assert res2.applications_migrated == 0
    assert res2.applications_skipped == 1

    # Row count strictly remains 1
    with Session(repo.engine) as s:
        total_apps = s.exec(
            select(CandidateApplication).where(
                CandidateApplication.candidate_id == owner.id,
                CandidateApplication.job_id == job.id,
            )
        ).all()
        assert len(total_apps) == 1


def test_hidden_closed_discarded_job_lifecycle_preserved(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """6. Genuinely hidden, closed, or discarded jobs are NEVER reset to new."""
    repo, st, owner = test_db

    with Session(repo.engine) as s:
        j_hidden = Job(
            id="j_hidden_lifecycle",
            canonical_url="https://example.com/h1",
            status=JobStatus.HIDDEN.value,
            score=30,
            created_at="2026-11-20T10:00:00Z",
            updated_at="2026-11-20T10:00:00Z",
        )
        j_discarded = Job(
            id="j_discarded_lifecycle",
            canonical_url="https://example.com/d1",
            status=JobStatus.DISCARDED.value,
            score=15,
            created_at="2026-11-20T10:00:00Z",
            updated_at="2026-11-20T10:00:00Z",
        )
        s.add(j_hidden)
        s.add(j_discarded)
        s.commit()

    service = LegacyMigrationService(repo, st)
    res = service.migrate(dry_run=False)
    assert res.success is True

    # Scores are migrated
    assert repo.get_historical_assessment(owner.id, "j_hidden_lifecycle") is not None
    assert repo.get_historical_assessment(owner.id, "j_discarded_lifecycle") is not None

    # Job statuses remain strictly HIDDEN and DISCARDED
    check_hidden = repo.get_job("j_hidden_lifecycle")
    assert check_hidden is not None
    assert check_hidden.status == JobStatus.HIDDEN.value

    check_discarded = repo.get_job("j_discarded_lifecycle")
    assert check_discarded is not None
    assert check_discarded.status == JobStatus.DISCARDED.value


def test_injected_failure_rolls_back_and_preserves_database(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """7. An injected failure during migration rolls back cleanly without partial records."""
    repo, st, owner = test_db

    job, _ = repo.get_or_create_job("j_fail_test", "https://example.com/fail")
    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.status = JobStatus.SHORTLISTED.value
        db_job.score = 75
        db_job.user_status_at = "2026-11-24T10:00:00Z"
        s.add(db_job)
        s.commit()

    service = LegacyMigrationService(repo, st)

    # Patch execution to raise an error during application migration
    with patch.object(
        service.repo, "_now", side_effect=RuntimeError("Injected crash midway through migration")
    ):
        res = service.migrate(dry_run=False)

    assert res.success is False
    assert "Injected crash midway through migration" in (res.error or "")

    # Verify zero records committed
    assert repo.get_historical_assessment(owner.id, job.id) is None
    assert repo.get_candidate_application(owner.id, job.id) is None


def test_candidate_isolation_and_owner_association(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """8. Migrated data is strictly isolated to owner candidate; other candidates unaffected."""
    repo, st, owner = test_db

    other_user = repo.create_candidate(
        id="cand_unrelated_user", display_name="Bob User", email="bob@other.local"
    )

    job, _ = repo.get_or_create_job("j_iso_1", "https://example.com/iso1")
    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.status = JobStatus.INTERVIEW.value
        db_job.score = 90
        db_job.user_status_at = "2026-11-25T10:00:00Z"
        s.add(db_job)
        s.commit()

    service = LegacyMigrationService(repo, st)
    res = service.migrate(dry_run=False)
    assert res.success is True

    # Owner has assessment and application
    assert repo.get_historical_assessment(owner.id, job.id) is not None
    assert repo.get_candidate_application(owner.id, job.id) is not None

    # Other candidate has zero records
    assert repo.get_historical_assessment(other_user.id, job.id) is None
    assert repo.get_candidate_application(other_user.id, job.id) is None


def test_dry_run_audit_simulation(test_db: tuple[Repo, Settings, Candidate]) -> None:
    """9. Dry-run mode reports exact counts without modifying the database."""
    repo, st, owner = test_db

    job, _ = repo.get_or_create_job("j_dry_1", "https://example.com/dry1")
    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.status = JobStatus.OFFER.value
        db_job.score = 95
        db_job.user_status_at = "2026-11-26T10:00:00Z"
        s.add(db_job)
        s.commit()

    service = LegacyMigrationService(repo, st)
    dry_result = service.migrate(dry_run=True)

    assert dry_result.dry_run is True
    assert dry_result.scores_migrated == 1
    assert dry_result.applications_migrated == 1
    assert dry_result.backup_path is None

    # Verify database was NOT modified
    assert repo.get_historical_assessment(owner.id, job.id) is None
    assert repo.get_candidate_application(owner.id, job.id) is None


def test_ambiguous_records_flagged_for_manual_review(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """10. Unrecognized and ambiguous status strings are flagged for manual review."""
    repo, st, _owner = test_db
    job, _ = repo.get_or_create_job("j_ambig_1", "https://example.com/ambig")
    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.status = JobStatus.NEW.value
        db_job.user_status_at = "2026-11-26T10:00:00Z"
        s.add(db_job)
        s.commit()

    # Raw insert for custom/unrecognized status ignoring SQLite check constraint
    with repo.engine.connect() as conn:
        conn.execute(text("PRAGMA ignore_check_constraints = ON"))
        conn.execute(
            text(
                "INSERT INTO job (id, canonical_url, status, created_at, updated_at) "
                "VALUES ('j_unk_2', 'https://example.com/unk2', 'legacy_custom_val', 'now', 'now')"
            )
        )
        conn.commit()

    service = LegacyMigrationService(repo, st)
    audit = service.audit()

    assert len(audit.manual_review_records) == 2
    rec_ambig = next(r for r in audit.manual_review_records if r["job_id"] == "j_ambig_1")
    assert "Ambiguous record: inventory status 'new'" in rec_ambig["reason"]

    rec_unk = next(r for r in audit.manual_review_records if r["job_id"] == "j_unk_2")
    assert "neither workflow nor inventory status" in rec_unk["reason"]


def test_global_job_lifecycle_statuses_remain_untouched(
    test_db: tuple[Repo, Settings, Candidate],
) -> None:
    """11. Global Job lifecycle statuses (new, hidden, closed, etc.) are NEVER modified."""
    repo, st, _owner = test_db

    lifecycle_statuses = [
        ("j_lc_1", JobStatus.NEW.value),
        ("j_lc_2", JobStatus.DISCOVERED.value),
        ("j_lc_3", JobStatus.EXTRACTED.value),
        ("j_lc_4", JobStatus.NEEDS_REVIEW.value),
        ("j_lc_5", JobStatus.HIDDEN.value),
        ("j_lc_6", JobStatus.DISCARDED.value),
    ]
    with Session(repo.engine) as s:
        for jid, st_val in lifecycle_statuses:
            j = Job(
                id=jid,
                canonical_url=f"https://example.com/{jid}",
                status=st_val,
                score=50,
                created_at="2026-11-20T10:00:00Z",
                updated_at="2026-11-20T10:00:00Z",
            )
            s.add(j)
        s.commit()

    service = LegacyMigrationService(repo, st)
    res = service.migrate(dry_run=False)
    assert res.success is True

    # Confirm all job records retain exact original status on the shared job table
    with Session(repo.engine) as s:
        for jid, expected_st in lifecycle_statuses:
            db_j = s.get(Job, jid)
            assert db_j is not None
            assert db_j.status == expected_st


def test_cli_migrate_legacy_dry_run_and_safety(
    test_db: tuple[Repo, Settings, Candidate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """12. CLI migrate-legacy dry-run writes zero changes and fails safely on missing owner."""
    repo, st, owner = test_db
    from typer.testing import CliRunner

    from jobradar import __main__ as main_mod
    from jobradar import app as application

    runner = CliRunner()
    monkeypatch.setattr(application, "open_store", lambda: (repo, None))
    monkeypatch.setattr(main_mod, "_settings", lambda: st)

    # 1. Non-existent owner -> fails cleanly with exit code 1
    res_bad = runner.invoke(main_mod.app, ["migrate-legacy", "--owner-id", "cand_non_existent"])
    assert res_bad.exit_code == 1
    assert "Owner resolution error" in res_bad.output

    # 2. Dry run with valid owner -> succeeds with exit code 0 and makes 0 writes
    job, _ = repo.get_or_create_job("j_cli_1", "https://example.com/cli1")
    with Session(repo.engine) as s:
        db_job = s.get(Job, job.id)
        assert db_job is not None
        db_job.score = 77
        s.add(db_job)
        s.commit()

    res_dry = runner.invoke(main_mod.app, ["migrate-legacy", "--owner-id", owner.id])
    assert res_dry.exit_code == 0
    assert "Legacy Migration Audit (DRY-RUN)" in res_dry.output
    assert "Dry-run complete. No changes written." in res_dry.output

    # Verify zero database modifications
    assert repo.get_historical_assessment(owner.id, job.id) is None
    assert repo.get_candidate_application(owner.id, job.id) is None
