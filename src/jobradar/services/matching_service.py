"""Candidate-specific matching service.

Orchestrates candidate profile retrieval, canonical adaptation, J4 eligibility
evaluation, four-track technical fit calculation, and isolated assessment persistence.
Shared job records remain completely immutable across all candidates.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date

from jobradar.clock import utcnow
from jobradar.db.models import CandidateJobAssessment, CandidateProfile, Job
from jobradar.db.repo import Repo
from jobradar.pipeline.candidate_adapter import adapt_candidate_profile
from jobradar.pipeline.eligibility import (
    EligibilityResult,
    candidate_eligibility,
    evaluate_eligibility,
)
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.technical_match import (
    FourTrackAssessment,
    evaluate_technical_fit,
)


class CandidateMatchingService:
    """Service for evaluating jobs against specific candidate profiles."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    def assess_candidate_job(
        self,
        candidate_id: str,
        job: tuple[str, JobPosting] | Job | JobPosting | str,
        profile_version: int | None = None,
        as_of: date | None = None,
    ) -> list[CandidateJobAssessment]:
        """Assess a single job for a candidate and persist isolated results."""
        results = self.assess_candidate_jobs(
            candidate_id=candidate_id,
            jobs=[job],
            profile_version=profile_version,
            as_of=as_of,
        )
        return results

    def assess_candidate_jobs(
        self,
        candidate_id: str,
        jobs: Sequence[tuple[str, JobPosting] | Job | JobPosting | str],
        profile_version: int | None = None,
        as_of: date | None = None,
    ) -> list[CandidateJobAssessment]:
        """Assess multiple jobs against a candidate's confirmed profile.

        Steps:
        1. Verify candidate exists.
        2. Resolve confirmed profile (by specified version or latest active).
        3. Adapt profile to J4 canonical evidence-backed format.
        4. For each job, evaluate eligibility and technical fit.
        5. Persist assessments in candidate_job_assessment table.
        6. Return persisted assessments.

        Raises:
            ValueError: If candidate not found, profile not found, or profile not confirmed.
        """
        # 1. Verify candidate
        candidate = self.repo.get_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"Candidate '{candidate_id}' not found.")

        # 2. Resolve confirmed profile
        profile: CandidateProfile | None = None
        if profile_version is not None:
            profiles = self.repo.get_candidate_profiles(candidate_id)
            for p in profiles:
                if p.version == profile_version:
                    profile = p
                    break
            if profile is None:
                raise ValueError(
                    f"Candidate '{candidate_id}' has no profile version {profile_version}."
                )
        else:
            profile = self.repo.get_active_profile(candidate_id)
            if profile is None:
                # Fallback: check all candidate profiles for a confirmed one
                all_p = self.repo.get_candidate_profiles(candidate_id)
                confirmed = [p for p in all_p if p.review_status == "confirmed"]
                if confirmed:
                    profile = confirmed[0]

        if profile is None:
            raise ValueError(f"Candidate '{candidate_id}' has no confirmed active profile.")

        if profile.review_status != "confirmed":
            raise ValueError(
                f"Candidate profile '{profile.id}' is in '{profile.review_status}' status. "
                "Only confirmed profiles may receive job assessments."
            )

        # 3. Adapt profile into J4 format
        canonical_profile = adapt_candidate_profile(profile, allow_draft=False)
        today = as_of or utcnow().date()
        cand_eligibility = candidate_eligibility(canonical_profile, today)

        # 4. Process each job
        persisted_assessments: list[CandidateJobAssessment] = []
        for item in jobs:
            job_id, posting = self._resolve_job_and_posting(item)

            # Ensure shared job row exists in the database to satisfy foreign keys
            self.repo.get_or_create_job(
                job_id, posting.apply_url or f"https://example.com/jobs/{job_id}"
            )

            # Evaluate eligibility
            eligibility_result: EligibilityResult = evaluate_eligibility(posting, cand_eligibility)

            # Evaluate technical fit across 4 tracks
            four_track: FourTrackAssessment = evaluate_technical_fit(
                posting, canonical_profile, eligibility_result
            )

            # 5. Persist candidate assessments atomically
            saved = self.repo.save_candidate_four_track_assessment(
                candidate_id=candidate.id,
                job_id=job_id,
                profile_id=profile.id,
                profile_version=profile.version,
                result=four_track,
            )
            persisted_assessments.extend(saved)

        return persisted_assessments

    def _resolve_job_and_posting(
        self, item: tuple[str, JobPosting] | Job | JobPosting | str
    ) -> tuple[str, JobPosting]:
        """Extract job_id and JobPosting instance from varying input types."""
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], JobPosting):
            return str(item[0]), item[1]

        if isinstance(item, JobPosting):
            job_id = getattr(item, "apply_url", "synthetic_job")
            return str(job_id), item

        if isinstance(item, Job):
            job_id = item.id
            if item.data_json:
                data = json.loads(item.data_json)
                posting = JobPosting.model_validate(data)
            else:
                posting = JobPosting(
                    company=item.company or "",
                    role=item.role or "",
                    apply_url=item.canonical_url or "",
                )
            return job_id, posting

        if isinstance(item, str):
            # Treat as job_id to look up in repository
            db_job = self.repo.get_job(item)
            if db_job is None:
                raise ValueError(f"Job with ID '{item}' not found in database.")
            return self._resolve_job_and_posting(db_job)

        raise TypeError(f"Unsupported job type: {type(item)}")
