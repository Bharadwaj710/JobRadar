"""Regression test suite for track-specific Best Match ordering in Repo.find_jobs().

Validates that:
1. Without a track filter: ranks using candidate's highest score across all tracks.
2. With track='ai_backend': ranks exclusively by ai_backend technical_score.
3. With track='full_stack': ranks exclusively by full_stack technical_score.
4. With track='sde': ranks exclusively by sde technical_score.
5. With track='sdet': ranks exclusively by sdet technical_score.
6. Missing/unscored assessments (None) sort last deterministically.
7. Candidate A and Candidate B isolation: conflicting scores between candidates do not cross-leak.
8. Legacy behavior: when candidate_id is None, ranks by legacy j.score.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, create_engine

from jobradar.clock import to_iso, utcnow
from jobradar.db.models import CandidateJobAssessment, Job, JobStatus
from jobradar.db.repo import Repo, init_db


@pytest.fixture
def repo() -> Repo:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    return Repo(engine)


@pytest.fixture
def seeded_repo(repo: Repo) -> tuple[Repo, str, str]:
    """Seed 5 jobs and assessments for Candidate A and Candidate B with conflicting scores."""
    now = to_iso(utcnow())

    # Create Candidates
    cand_a = repo.create_candidate(name="Alice Backend", email="alice@test.local")
    cand_b = repo.create_candidate(name="Bob SDET", email="bob@test.local")

    # Seed 5 shared inventory jobs
    jobs = [
        Job(
            id="job_alpha",
            canonical_url="https://example.com/alpha",
            role="AI Backend Lead",
            company="AlphaCorp",
            status=JobStatus.NEW.value,
            score=50,  # Legacy score
            score_reason="Legacy 50",
            created_at="2026-01-01T10:00:00.000000Z",
            updated_at=now,
        ),
        Job(
            id="job_beta",
            canonical_url="https://example.com/beta",
            role="Full Stack Architect",
            company="BetaCorp",
            status=JobStatus.NEW.value,
            score=90,  # Legacy score
            score_reason="Legacy 90",
            created_at="2026-01-01T11:00:00.000000Z",
            updated_at=now,
        ),
        Job(
            id="job_gamma",
            canonical_url="https://example.com/gamma",
            role="Systems / SDE Core",
            company="GammaCorp",
            status=JobStatus.NEW.value,
            score=70,  # Legacy score
            score_reason="Legacy 70",
            created_at="2026-01-01T12:00:00.000000Z",
            updated_at=now,
        ),
        Job(
            id="job_delta",
            canonical_url="https://example.com/delta",
            role="SDET Automation Specialist",
            company="DeltaCorp",
            status=JobStatus.NEW.value,
            score=40,  # Legacy score
            score_reason="Legacy 40",
            created_at="2026-01-01T13:00:00.000000Z",
            updated_at=now,
        ),
        Job(
            id="job_epsilon",
            canonical_url="https://example.com/epsilon",
            role="General Platform Role",
            company="EpsilonCorp",
            status=JobStatus.NEW.value,
            score=60,  # Legacy score
            score_reason="Legacy 60",
            created_at="2026-01-01T14:00:00.000000Z",
            updated_at=now,
        ),
    ]

    with Session(repo.engine) as s:
        for j in jobs:
            s.add(j)
        s.commit()

    # Candidate A assessments (deliberately inverted across tracks):
    # job_alpha:   ai_backend=95, full_stack=60, sde=70, sdet=40
    # job_beta:    ai_backend=40, full_stack=95, sde=65, sdet=55
    # job_gamma:   ai_backend=60, full_stack=50, sde=95, sdet=45
    # job_delta:   ai_backend=30, full_stack=45, sde=50, sdet=95
    # job_epsilon: ai_backend=None (unscored), full_stack=75, sde=None, sdet=None
    assessments_a = [
        # job_alpha
        CandidateJobAssessment(
            id="a_alpha_ai",
            candidate_id=cand_a.id,
            job_id="job_alpha",
            profile_id="p1",
            profile_version=1,
            track="ai_backend",
            technical_score=95,
            evidence_coverage=0.9,
            eligibility_status="ELIGIBLE",
            explanation="strong ai backend",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_alpha_fs",
            candidate_id=cand_a.id,
            job_id="job_alpha",
            profile_id="p1",
            profile_version=1,
            track="full_stack",
            technical_score=60,
            evidence_coverage=0.6,
            eligibility_status="ELIGIBLE",
            explanation="medium full stack",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_alpha_sde",
            candidate_id=cand_a.id,
            job_id="job_alpha",
            profile_id="p1",
            profile_version=1,
            track="sde",
            technical_score=70,
            evidence_coverage=0.7,
            eligibility_status="ELIGIBLE",
            explanation="good sde",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_alpha_sdet",
            candidate_id=cand_a.id,
            job_id="job_alpha",
            profile_id="p1",
            profile_version=1,
            track="sdet",
            technical_score=40,
            evidence_coverage=0.4,
            eligibility_status="ELIGIBLE",
            explanation="fair sdet",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        # job_beta
        CandidateJobAssessment(
            id="a_beta_ai",
            candidate_id=cand_a.id,
            job_id="job_beta",
            profile_id="p1",
            profile_version=1,
            track="ai_backend",
            technical_score=40,
            evidence_coverage=0.4,
            eligibility_status="ELIGIBLE",
            explanation="fair ai backend",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_beta_fs",
            candidate_id=cand_a.id,
            job_id="job_beta",
            profile_id="p1",
            profile_version=1,
            track="full_stack",
            technical_score=95,
            evidence_coverage=0.9,
            eligibility_status="ELIGIBLE",
            explanation="strong full stack",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_beta_sde",
            candidate_id=cand_a.id,
            job_id="job_beta",
            profile_id="p1",
            profile_version=1,
            track="sde",
            technical_score=65,
            evidence_coverage=0.65,
            eligibility_status="ELIGIBLE",
            explanation="medium sde",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_beta_sdet",
            candidate_id=cand_a.id,
            job_id="job_beta",
            profile_id="p1",
            profile_version=1,
            track="sdet",
            technical_score=55,
            evidence_coverage=0.55,
            eligibility_status="ELIGIBLE",
            explanation="medium sdet",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        # job_gamma
        CandidateJobAssessment(
            id="a_gamma_ai",
            candidate_id=cand_a.id,
            job_id="job_gamma",
            profile_id="p1",
            profile_version=1,
            track="ai_backend",
            technical_score=60,
            evidence_coverage=0.6,
            eligibility_status="ELIGIBLE",
            explanation="medium ai backend",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_gamma_fs",
            candidate_id=cand_a.id,
            job_id="job_gamma",
            profile_id="p1",
            profile_version=1,
            track="full_stack",
            technical_score=50,
            evidence_coverage=0.5,
            eligibility_status="ELIGIBLE",
            explanation="fair full stack",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_gamma_sde",
            candidate_id=cand_a.id,
            job_id="job_gamma",
            profile_id="p1",
            profile_version=1,
            track="sde",
            technical_score=95,
            evidence_coverage=0.95,
            eligibility_status="ELIGIBLE",
            explanation="strong sde",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_gamma_sdet",
            candidate_id=cand_a.id,
            job_id="job_gamma",
            profile_id="p1",
            profile_version=1,
            track="sdet",
            technical_score=45,
            evidence_coverage=0.45,
            eligibility_status="ELIGIBLE",
            explanation="fair sdet",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        # job_delta
        CandidateJobAssessment(
            id="a_delta_ai",
            candidate_id=cand_a.id,
            job_id="job_delta",
            profile_id="p1",
            profile_version=1,
            track="ai_backend",
            technical_score=30,
            evidence_coverage=0.3,
            eligibility_status="ELIGIBLE",
            explanation="low ai backend",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_delta_fs",
            candidate_id=cand_a.id,
            job_id="job_delta",
            profile_id="p1",
            profile_version=1,
            track="full_stack",
            technical_score=45,
            evidence_coverage=0.45,
            eligibility_status="ELIGIBLE",
            explanation="fair full stack",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_delta_sde",
            candidate_id=cand_a.id,
            job_id="job_delta",
            profile_id="p1",
            profile_version=1,
            track="sde",
            technical_score=50,
            evidence_coverage=0.5,
            eligibility_status="ELIGIBLE",
            explanation="fair sde",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_delta_sdet",
            candidate_id=cand_a.id,
            job_id="job_delta",
            profile_id="p1",
            profile_version=1,
            track="sdet",
            technical_score=95,
            evidence_coverage=0.95,
            eligibility_status="ELIGIBLE",
            explanation="strong sdet",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        # job_epsilon (unscored on ai_backend, sde, sdet)
        CandidateJobAssessment(
            id="a_epsilon_ai",
            candidate_id=cand_a.id,
            job_id="job_epsilon",
            profile_id="p1",
            profile_version=1,
            track="ai_backend",
            technical_score=None,
            evidence_coverage=0.0,
            eligibility_status="ELIGIBLE",
            explanation="unscored",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_epsilon_fs",
            candidate_id=cand_a.id,
            job_id="job_epsilon",
            profile_id="p1",
            profile_version=1,
            track="full_stack",
            technical_score=75,
            evidence_coverage=0.75,
            eligibility_status="ELIGIBLE",
            explanation="good full stack",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_epsilon_sde",
            candidate_id=cand_a.id,
            job_id="job_epsilon",
            profile_id="p1",
            profile_version=1,
            track="sde",
            technical_score=None,
            evidence_coverage=0.0,
            eligibility_status="ELIGIBLE",
            explanation="unscored",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="a_epsilon_sdet",
            candidate_id=cand_a.id,
            job_id="job_epsilon",
            profile_id="p1",
            profile_version=1,
            track="sdet",
            technical_score=None,
            evidence_coverage=0.0,
            eligibility_status="ELIGIBLE",
            explanation="unscored",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
    ]

    # Candidate B assessments (inverting alpha and delta):
    # job_alpha: sdet=92 (vs Candidate A's 40)
    # job_delta: sdet=15 (vs Candidate A's 95)
    assessments_b = [
        CandidateJobAssessment(
            id="b_alpha_sdet",
            candidate_id=cand_b.id,
            job_id="job_alpha",
            profile_id="p2",
            profile_version=1,
            track="sdet",
            technical_score=92,
            evidence_coverage=0.92,
            eligibility_status="ELIGIBLE",
            explanation="candidate b alpha sdet",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
        CandidateJobAssessment(
            id="b_delta_sdet",
            candidate_id=cand_b.id,
            job_id="job_delta",
            profile_id="p2",
            profile_version=1,
            track="sdet",
            technical_score=15,
            evidence_coverage=0.15,
            eligibility_status="ELIGIBLE",
            explanation="candidate b delta sdet",
            assessment_json="{}",
            eligibility_json="{}",
            created_at=now,
            updated_at=now,
        ),
    ]

    with Session(repo.engine) as s:
        for a in [*assessments_a, *assessments_b]:
            s.add(a)
        s.commit()

    return repo, cand_a.id, cand_b.id


def test_overall_best_match_ordering(seeded_repo: tuple[Repo, str, str]) -> None:
    """Without track filter: ranks using candidate's highest available score across all tracks."""
    repo, cand_a_id, _ = seeded_repo
    jobs, total = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track=None,
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_a_id,
    )
    assert total == 5
    job_ids = [j.id for j, _ in jobs]
    # Jobs alpha, beta, gamma, delta all have a track with 95; epsilon has max 75.
    # Therefore, epsilon must rank last among scored jobs.
    assert job_ids[4] == "job_epsilon"
    assert set(job_ids[:4]) == {"job_alpha", "job_beta", "job_gamma", "job_delta"}


def test_ai_backend_specific_ordering(seeded_repo: tuple[Repo, str, str]) -> None:
    """With track='ai_backend': ranks exclusively by ai_backend technical_score."""
    repo, cand_a_id, _ = seeded_repo
    jobs, total = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track="ai_backend",
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_a_id,
    )
    assert total == 5
    job_ids = [j.id for j, _ in jobs]
    # Candidate A ai_backend scores:
    # job_alpha: 95
    # job_gamma: 60
    # job_beta:  40
    # job_delta: 30
    # job_epsilon: None (unscored, sorts last)
    assert job_ids == ["job_alpha", "job_gamma", "job_beta", "job_delta", "job_epsilon"]


def test_full_stack_specific_ordering(seeded_repo: tuple[Repo, str, str]) -> None:
    """With track='full_stack': ranks exclusively by full_stack technical_score."""
    repo, cand_a_id, _ = seeded_repo
    jobs, total = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track="full_stack",
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_a_id,
    )
    assert total == 5
    job_ids = [j.id for j, _ in jobs]
    # Candidate A full_stack scores:
    # job_beta:    95
    # job_epsilon: 75
    # job_alpha:   60
    # job_gamma:   50
    # job_delta:   45
    assert job_ids == ["job_beta", "job_epsilon", "job_alpha", "job_gamma", "job_delta"]


def test_sde_specific_ordering(seeded_repo: tuple[Repo, str, str]) -> None:
    """With track='sde': ranks exclusively by sde technical_score."""
    repo, cand_a_id, _ = seeded_repo
    jobs, total = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track="sde",
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_a_id,
    )
    assert total == 5
    job_ids = [j.id for j, _ in jobs]
    # Candidate A sde scores:
    # job_gamma:   95
    # job_alpha:   70
    # job_beta:    65
    # job_delta:   50
    # job_epsilon: None (unscored, sorts last)
    assert job_ids == ["job_gamma", "job_alpha", "job_beta", "job_delta", "job_epsilon"]


def test_sdet_specific_ordering(seeded_repo: tuple[Repo, str, str]) -> None:
    """With track='sdet': ranks exclusively by sdet technical_score."""
    repo, cand_a_id, _ = seeded_repo
    jobs, total = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track="sdet",
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_a_id,
    )
    assert total == 5
    job_ids = [j.id for j, _ in jobs]
    # Candidate A sdet scores:
    # job_delta:   95
    # job_beta:    55
    # job_gamma:   45
    # job_alpha:   40
    # job_epsilon: None (unscored, sorts last)
    assert job_ids == ["job_delta", "job_beta", "job_gamma", "job_alpha", "job_epsilon"]


def test_missing_and_unscored_assessments_sort_last(seeded_repo: tuple[Repo, str, str]) -> None:
    """Verifies that unscored (None) assessments sort after all scored assessments."""
    repo, cand_a_id, _ = seeded_repo
    jobs, _ = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track="sdet",
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_a_id,
    )
    # job_epsilon has score=None for sdet, so it must be last
    assert jobs[-1][0].id == "job_epsilon"


def test_candidate_isolation_in_track_sort(seeded_repo: tuple[Repo, str, str]) -> None:
    """Candidate A and Candidate B see distinct track rankings reflecting their own private scores."""
    repo, cand_a_id, cand_b_id = seeded_repo

    # Candidate A SDET order: delta (95) > alpha (40)
    jobs_a, _ = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track="sdet",
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_a_id,
    )
    ids_a = [j.id for j, _ in jobs_a if j.id in ("job_alpha", "job_delta")]
    assert ids_a == ["job_delta", "job_alpha"]

    # Candidate B SDET order: alpha (92) > delta (15)
    jobs_b, _ = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version="1",
        track="sdet",
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=cand_b_id,
    )
    ids_b = [j.id for j, _ in jobs_b if j.id in ("job_alpha", "job_delta")]
    assert ids_b == ["job_alpha", "job_delta"]


def test_legacy_no_candidate_behavior(seeded_repo: tuple[Repo, str, str]) -> None:
    """When candidate_id is None, ranks by legacy j.score regardless of candidate assessments."""
    repo, _, _ = seeded_repo
    jobs, total = repo.find_jobs(
        statuses=["new"],
        match=None,
        min_score=None,
        since=None,
        source_id=None,
        track_version=None,
        track=None,
        eligibility=None,
        technical_min=None,
        work_mode=None,
        published_since=None,
        priority=None,
        order="score",
        limit=10,
        offset=0,
        candidate_id=None,
    )
    assert total == 5
    job_ids = [j.id for j, _ in jobs]
    # Legacy j.score values:
    # job_beta:    90
    # job_gamma:   70
    # job_epsilon: 60
    # job_alpha:   50
    # job_delta:   40
    assert job_ids == ["job_beta", "job_gamma", "job_epsilon", "job_alpha", "job_delta"]
