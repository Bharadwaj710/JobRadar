"""Legacy Score & Application Workflow Migration Service.

Safely migrates legacy owner-specific matching scores and application workflow
states into candidate-specific tables (CandidateHistoricalAssessment,
CandidateApplication, CandidateApplicationHistory) without modifying shared job
inventory records or contaminating J4 career-track technical scores.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, col, select

from jobradar.config import Settings
from jobradar.db.models import (
    Artifact,
    Candidate,
    CandidateApplication,
    CandidateApplicationStatus,
    CandidateHistoricalAssessment,
    Job,
    JobStatus,
    MatchResult,
    StatusChange,
)
from jobradar.db.repo import (
    OwnerResolutionError,
    Repo,
    backup_database,
)

log = logging.getLogger(__name__)

# Established mapping from legacy JobStatus to CandidateApplicationStatus
LEGACY_STATUS_MAPPING: dict[str, str] = {
    JobStatus.SHORTLISTED.value: CandidateApplicationStatus.SAVED.value,
    JobStatus.RESUME_READY.value: CandidateApplicationStatus.SAVED.value,
    JobStatus.APPLIED.value: CandidateApplicationStatus.APPLIED.value,
    JobStatus.INTERVIEW.value: CandidateApplicationStatus.INTERVIEWING.value,
    JobStatus.OFFER.value: CandidateApplicationStatus.OFFERED.value,
    JobStatus.REJECTED.value: CandidateApplicationStatus.REJECTED.value,
    JobStatus.SKIPPED.value: CandidateApplicationStatus.SKIPPED.value,
}

# Lifecycle / system statuses that do NOT represent user application workflow states
SYSTEM_STATUSES: set[str] = {
    JobStatus.DISCOVERED.value,
    JobStatus.EXTRACTED.value,
    JobStatus.NEEDS_REVIEW.value,
    JobStatus.DISCARDED.value,
    JobStatus.NEW.value,
    JobStatus.HIDDEN.value,
}


@dataclass
class MigrationAuditReport:
    """Pre-migration audit findings."""

    owner_candidate_id: str
    owner_candidate_name: str
    total_jobs: int
    scores_eligible: int
    scores_already_migrated: int
    applications_eligible: int
    applications_already_migrated: int
    manual_review_records: list[dict[str, Any]] = field(default_factory=list)
    score_details: list[dict[str, Any]] = field(default_factory=list)
    application_details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MigrationResult:
    """Outcome of migration execution or dry-run simulation."""

    dry_run: bool
    owner_candidate_id: str
    backup_path: Path | None
    scores_migrated: int
    scores_skipped: int
    applications_migrated: int
    applications_skipped: int
    history_rows_migrated: int
    manual_review_count: int
    success: bool
    message: str
    error: str | None = None


_DEFAULT_MATCH_WEIGHTS: dict[str, float] = {
    "skills_coverage": 0.40,
    "role_alignment": 0.30,
    "seniority_fit": 0.15,
    "location_ok": 0.15,
}


def _resolve_legacy_score_timestamp(
    job: Job,
    match_results_by_job: dict[str, list[MatchResult]],
    weights: dict[str, float] | None = None,
) -> str | None:
    """Resolve genuine scoring timestamp from MatchResult only when correspondence is proven.

    Requirements:
    1. A timestamp is ONLY assigned when it genuinely belongs to that legacy scoring event.
    2. If correspondence cannot be proven, return None (migrated as NULL).
    3. Do NOT infer correspondence from matching job IDs alone if the scoring systems
       or evaluation events are different.
    4. For rule-based skills matches ('skills only'), verify provider is 'skills_rule'
       and that skills_coverage matches job.score.
    5. For LLM evaluations, verify provider is an LLM provider and answers_json
       contains verifiable score evidence matching job.score (explicit score or weighted
       dimensions score).
    6. Non-scoring providers ('rules') and unrelated evaluations return None.
    7. Never substitute job.updated_at, user_status_at, or migration execution time.
    """
    if job.score is None:
        return None

    mrs = match_results_by_job.get(job.id, [])
    if not mrs:
        return None

    reason = (job.score_reason or "").strip()

    # Rule-based skills matching ("skills only ...")
    if reason.startswith("skills only"):
        for mr in mrs:
            if mr.provider == "skills_rule":
                try:
                    data = json.loads(mr.answers_json) if mr.answers_json else {}
                    cov = data.get("skills_coverage")
                    if cov is not None and round(100 * cov) == job.score:
                        return mr.created_at
                except (json.JSONDecodeError, TypeError):
                    continue
        return None

    # LLM-based evaluation: only assign timestamp when exact score correspondence is proven
    candidate_weights = [weights] if weights else []
    if _DEFAULT_MATCH_WEIGHTS not in candidate_weights:
        candidate_weights.append(_DEFAULT_MATCH_WEIGHTS)

    for mr in mrs:
        if mr.provider in ("llm", "gemini", "groq", "jev"):
            try:
                data = json.loads(mr.answers_json) if mr.answers_json else {}
            except (json.JSONDecodeError, TypeError):
                continue

            if not isinstance(data, dict):
                continue

            # 1. Direct explicit score in data or data["raw"]
            score_val = data.get("score")
            if score_val is None and isinstance(data.get("raw"), dict):
                score_val = data["raw"].get("score")
            if isinstance(score_val, (int, float)) and round(float(score_val)) == job.score:
                return mr.created_at

            # 2. Weighted dimension score matching legacy Matcher formula
            for w_dict in candidate_weights:
                parts = [(w, data.get(name)) for name, w in w_dict.items()]
                valid_parts = [
                    (w, float(v))
                    for w, v in parts
                    if v is not None and isinstance(v, (int, float)) and w > 0
                ]
                total = sum(w for w, _ in valid_parts)
                if total > 0:
                    derived = round(100 * sum(w * v for w, v in valid_parts) / total)
                    if derived == job.score:
                        return mr.created_at

    return None


class LegacyMigrationService:
    """Orchestrates safe, idempotent migration of legacy scores and application states."""

    def __init__(self, repo: Repo, settings: Settings):
        self.repo = repo
        self.settings = settings

    def resolve_owner(self, explicit_owner_id: str | None = None) -> Candidate:
        """Resolve target owner candidate through explicit ID or Stage 3A resolution."""
        if explicit_owner_id and explicit_owner_id.strip():
            return self.repo.resolve_owner_candidate(configured_id=explicit_owner_id.strip())
        return self.repo.resolve_owner_candidate_from_settings(self.settings)

    def audit(self, explicit_owner_id: str | None = None) -> MigrationAuditReport:
        """Inspect repository and produce pre-migration audit without mutations."""
        owner = self.resolve_owner(explicit_owner_id)
        owner_id = owner.id
        owner_name = owner.display_name or owner.email or owner.id

        with Session(self.repo.engine) as s:
            all_jobs = list(s.exec(select(Job).order_by(col(Job.created_at))).all())
            existing_apps = {
                app.job_id: app
                for app in s.exec(
                    select(CandidateApplication).where(
                        CandidateApplication.candidate_id == owner_id
                    )
                ).all()
            }
            existing_hist_scores = {
                h.job_id: h
                for h in s.exec(
                    select(CandidateHistoricalAssessment).where(
                        CandidateHistoricalAssessment.candidate_id == owner_id
                    )
                ).all()
            }
            all_artifacts = list(s.exec(select(Artifact)).all())
            all_status_changes = list(
                s.exec(select(StatusChange).order_by(col(StatusChange.changed_at))).all()
            )
            all_match_results = list(
                s.exec(select(MatchResult).order_by(col(MatchResult.created_at).desc())).all()
            )

        artifacts_by_job: dict[str, list[Artifact]] = {}
        for art in all_artifacts:
            artifacts_by_job.setdefault(art.job_id, []).append(art)

        changes_by_job: dict[str, list[StatusChange]] = {}
        for sc in all_status_changes:
            changes_by_job.setdefault(sc.job_id, []).append(sc)

        match_results_by_job: dict[str, list[MatchResult]] = {}
        for mr in all_match_results:
            match_results_by_job.setdefault(mr.job_id, []).append(mr)

        inventory_statuses = {
            JobStatus.DISCOVERED.value,
            JobStatus.EXTRACTED.value,
            JobStatus.NEEDS_REVIEW.value,
            JobStatus.NEW.value,
            JobStatus.HIDDEN.value,
            JobStatus.DISCARDED.value,
        }

        report = MigrationAuditReport(
            owner_candidate_id=owner_id,
            owner_candidate_name=owner_name,
            total_jobs=len(all_jobs),
            scores_eligible=0,
            scores_already_migrated=0,
            applications_eligible=0,
            applications_already_migrated=0,
        )

        for job in all_jobs:
            # 1. Audit legacy scores
            if job.score is not None:
                # Genuine score timestamp exists only if recorded in match_result AND
                # proven to correspond to the scoring event.
                # If correspondence cannot be proven, scored_at MUST remain None (NULL).
                # Do NOT substitute user_status_at or updated_at.
                genuine_scored_at = _resolve_legacy_score_timestamp(
                    job, match_results_by_job, weights=self.settings.config.matching.weights
                )

                existing_hist = existing_hist_scores.get(job.id)
                if existing_hist is not None and existing_hist.score == job.score:
                    report.scores_already_migrated += 1
                else:
                    report.scores_eligible += 1
                    report.score_details.append(
                        {
                            "job_id": job.id,
                            "company": job.company or "",
                            "role": job.role or "",
                            "score": job.score,
                            "score_reason": job.score_reason or "",
                            "scored_at": genuine_scored_at,
                        }
                    )

            # 2. Audit legacy application statuses
            has_user_status = job.user_status_at is not None
            is_workflow_status = job.status in LEGACY_STATUS_MAPPING

            # Flag completely unrecognized statuses
            if job.status not in LEGACY_STATUS_MAPPING and job.status not in inventory_statuses:
                report.manual_review_records.append(
                    {
                        "job_id": job.id,
                        "company": job.company or "",
                        "role": job.role or "",
                        "status": job.status,
                        "user_status_at": job.user_status_at,
                        "reason": (
                            f"Unrecognized job status '{job.status}' "
                            "(neither workflow nor inventory status)"
                        ),
                    }
                )
                continue

            # Flag inventory statuses that have user_status_at set (ambiguous state)
            if job.status in inventory_statuses and has_user_status:
                report.manual_review_records.append(
                    {
                        "job_id": job.id,
                        "company": job.company or "",
                        "role": job.role or "",
                        "status": job.status,
                        "user_status_at": job.user_status_at,
                        "reason": (
                            f"Ambiguous record: inventory status '{job.status}' "
                            "has user_status_at set"
                        ),
                    }
                )
                continue

            if is_workflow_status:
                mapped_status = LEGACY_STATUS_MAPPING[job.status]
                existing_app = existing_apps.get(job.id)
                if existing_app is not None:
                    report.applications_already_migrated += 1
                else:
                    report.applications_eligible += 1
                    report.application_details.append(
                        {
                            "job_id": job.id,
                            "company": job.company or "",
                            "role": job.role or "",
                            "legacy_status": job.status,
                            "mapped_status": mapped_status,
                            "user_status_at": job.user_status_at,
                            "artifacts_count": len(artifacts_by_job.get(job.id, [])),
                            "changes_count": len(changes_by_job.get(job.id, [])),
                        }
                    )

        return report

    def migrate(
        self,
        dry_run: bool = True,
        explicit_owner_id: str | None = None,
        backup_dir: Path | None = None,
    ) -> MigrationResult:
        """Execute legacy score and application workflow migration with full safety checks."""
        try:
            owner = self.resolve_owner(explicit_owner_id)
        except OwnerResolutionError as e:
            return MigrationResult(
                dry_run=dry_run,
                owner_candidate_id="",
                backup_path=None,
                scores_migrated=0,
                scores_skipped=0,
                applications_migrated=0,
                applications_skipped=0,
                history_rows_migrated=0,
                manual_review_count=0,
                success=False,
                message=f"Owner resolution failed: {e}",
                error=str(e),
            )

        audit_report = self.audit(owner.id)

        if dry_run:
            msg = (
                f"[DRY-RUN] Audit found {audit_report.scores_eligible} eligible scores "
                f"({audit_report.scores_already_migrated} already migrated) and "
                f"{audit_report.applications_eligible} eligible applications "
                f"({audit_report.applications_already_migrated} already migrated) "
                f"for owner '{owner.id}'. No changes written."
            )
            return MigrationResult(
                dry_run=True,
                owner_candidate_id=owner.id,
                backup_path=None,
                scores_migrated=audit_report.scores_eligible,
                scores_skipped=audit_report.scores_already_migrated,
                applications_migrated=audit_report.applications_eligible,
                applications_skipped=audit_report.applications_already_migrated,
                history_rows_migrated=sum(
                    d.get("changes_count", 0) for d in audit_report.application_details
                ),
                manual_review_count=len(audit_report.manual_review_records),
                success=True,
                message=msg,
            )

        # Non-dry-run: create backup first
        backup_path: Path | None = None
        if db_path := self.repo.engine.url.database:
            dest_dir = backup_dir or Path(db_path).parent
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
            backup_path = dest_dir / f"jobradar_pre_migration_{stamp}.db"
            backup_database(self.repo.engine, backup_path)
            log.info("Created pre-migration database backup at %s", backup_path)

        scores_migrated = 0
        apps_migrated = 0
        history_migrated = 0

        # Execute migration atomically within a single transaction
        try:
            now = self.repo._now()
            with self.repo._tx() as c:
                # 1. Migrate scores to candidate_historical_assessment
                for s_item in audit_report.score_details:
                    job_id = s_item["job_id"]
                    score = s_item["score"]
                    reason = s_item["score_reason"]
                    scored_at = s_item["scored_at"]
                    hist_id = uuid.uuid4().hex

                    c.execute(
                        text(
                            """
                            INSERT INTO candidate_historical_assessment (
                                id, candidate_id, job_id, score, score_reason,
                                scoring_system, scored_at, created_at, updated_at
                            ) VALUES (
                                :id, :cand_id, :job_id, :score, :reason,
                                'legacy_matcher', :scored_at, :now, :now
                            )
                            ON CONFLICT (candidate_id, job_id) DO UPDATE SET
                                score = excluded.score,
                                score_reason = excluded.score_reason,
                                scored_at = coalesce(
                                    excluded.scored_at,
                                    candidate_historical_assessment.scored_at
                                ),
                                updated_at = excluded.updated_at
                            """
                        ),
                        {
                            "id": hist_id,
                            "cand_id": owner.id,
                            "job_id": job_id,
                            "score": score,
                            "reason": reason,
                            "scored_at": scored_at,
                            "now": now,
                        },
                    )
                    scores_migrated += 1

                # 2. Migrate application statuses to candidate_application
                for a_item in audit_report.application_details:
                    job_id = a_item["job_id"]
                    mapped_status = a_item["mapped_status"]
                    user_status_at = a_item["user_status_at"]
                    app_id = uuid.uuid4().hex

                    # Retrieve associated artifacts
                    art_rows = c.execute(
                        text(
                            "SELECT id, kind, local_path, version FROM artifact "
                            "WHERE job_id = :job_id ORDER BY version ASC"
                        ),
                        {"job_id": job_id},
                    ).fetchall()
                    notes_parts: list[str] = []
                    if a_item["legacy_status"] == JobStatus.RESUME_READY.value:
                        notes_parts.append("Migrated from legacy resume_ready state.")
                    for art_id, art_kind, art_path, art_ver in art_rows:
                        notes_parts.append(
                            f"Associated {art_kind} artifact (id={art_id}, version={art_ver}): "
                            f"{art_path or 'stored'}"
                        )
                    notes = "\n".join(notes_parts)

                    # Determine applied_at timestamp (only from actual application records)
                    applied_at: str | None = None
                    if mapped_status in (
                        CandidateApplicationStatus.APPLIED.value,
                        CandidateApplicationStatus.INTERVIEWING.value,
                        CandidateApplicationStatus.OFFERED.value,
                        CandidateApplicationStatus.REJECTED.value,
                    ):
                        first_applied = c.execute(
                            text(
                                "SELECT changed_at FROM status_change "
                                "WHERE job_id = :job_id AND status = 'applied' "
                                "ORDER BY changed_at ASC LIMIT 1"
                            ),
                            {"job_id": job_id},
                        ).scalar()
                        applied_at = first_applied or user_status_at

                    app_created_at = user_status_at or now
                    c.execute(
                        text(
                            """
                            INSERT INTO candidate_application (
                                id, candidate_id, job_id, status, applied_at, notes,
                                created_at, updated_at
                            ) VALUES (
                                :id, :cand_id, :job_id, :status, :applied_at, :notes,
                                :created_at, :updated_at
                            )
                            ON CONFLICT (candidate_id, job_id) DO NOTHING
                            """
                        ),
                        {
                            "id": app_id,
                            "cand_id": owner.id,
                            "job_id": job_id,
                            "status": mapped_status,
                            "applied_at": applied_at,
                            "notes": notes,
                            "created_at": app_created_at,
                            "updated_at": now,
                        },
                    )
                    apps_migrated += 1

                    # Migrate status_change history
                    sc_rows = c.execute(
                        text(
                            "SELECT status, changed_at FROM status_change "
                            "WHERE job_id = :job_id ORDER BY changed_at ASC"
                        ),
                        {"job_id": job_id},
                    ).fetchall()
                    for sc_status, sc_changed_at in sc_rows:
                        existing_sc = c.execute(
                            text(
                                "SELECT 1 FROM candidate_application_history "
                                "WHERE candidate_id = :cand_id AND job_id = :job_id "
                                "AND changed_at = :changed_at"
                            ),
                            {
                                "cand_id": owner.id,
                                "job_id": job_id,
                                "changed_at": sc_changed_at,
                            },
                        ).first()
                        if existing_sc:
                            continue
                        sc_mapped = LEGACY_STATUS_MAPPING.get(sc_status, sc_status)
                        c.execute(
                            text(
                                """
                                INSERT INTO candidate_application_history (
                                    candidate_id, job_id, status, notes, changed_at
                                ) VALUES (
                                    :cand_id, :job_id, :status,
                                    'Migrated from legacy status_change', :changed_at
                                )
                                """
                            ),
                            {
                                "cand_id": owner.id,
                                "job_id": job_id,
                                "status": sc_mapped,
                                "changed_at": sc_changed_at,
                            },
                        )
                        history_migrated += 1

                # Verify foreign key integrity
                fk_problems = c.execute(text("PRAGMA foreign_key_check")).fetchall()
                if fk_problems:
                    raise RuntimeError(f"foreign key violations after migration: {fk_problems[:3]}")

            success_msg = (
                f"Successfully migrated {scores_migrated} legacy scores, "
                f"{apps_migrated} applications, and {history_migrated} history entries "
                f"to owner '{owner.id}'. Backup saved at '{backup_path}'."
            )
            log.info(success_msg)
            return MigrationResult(
                dry_run=False,
                owner_candidate_id=owner.id,
                backup_path=backup_path,
                scores_migrated=scores_migrated,
                scores_skipped=audit_report.scores_already_migrated,
                applications_migrated=apps_migrated,
                applications_skipped=audit_report.applications_already_migrated,
                history_rows_migrated=history_migrated,
                manual_review_count=len(audit_report.manual_review_records),
                success=True,
                message=success_msg,
            )
        except Exception as e:
            log.error("Migration failed and was rolled back: %s", e, exc_info=True)
            return MigrationResult(
                dry_run=False,
                owner_candidate_id=owner.id,
                backup_path=backup_path,
                scores_migrated=0,
                scores_skipped=audit_report.scores_already_migrated,
                applications_migrated=0,
                applications_skipped=audit_report.applications_already_migrated,
                history_rows_migrated=0,
                manual_review_count=len(audit_report.manual_review_records),
                success=False,
                message=f"Migration failed and was rolled back: {e}",
                error=str(e),
            )
