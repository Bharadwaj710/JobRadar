#!/usr/bin/env python3
"""Isolated Demo Environment Data Preparation & Seeding Script.

Creates a completely isolated, reproducible demonstration database
for Spektra Systems evaluation without touching the production database.

Scenarios populated:
1. Synthetic Candidate: Alex Dev (B.Tech CSE 2026, Python/FastAPI/Docker/Redis/PyTorch).
2. [Demo] Stripe Technologies: AI Systems Engineer (Eligible, AI Backend track).
3. [Demo] Vercel Cloud: Full Stack Engineer (Eligible, Full Stack track).
4. [Demo] Databricks Systems: Distributed Platform Engineer (Needs Verification, amber banner).
5. [Demo] Cloudflare Edge: Backend Infrastructure Engineer (Needs Verification, amber banner).
6. [Demo] Oracle Enterprise: Lead Backend Architect (Not Eligible, graduation batch & exp ceiling).
7. [Demo] Rubrik Core: SDET / Automation Engineer (Eligible, SDET track).
8. Candidate Application Tracking: Saved -> Applied -> Interviewing lifecycle with history logs.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jobradar.clock import to_iso, utcnow  # noqa: E402
from jobradar.db.models import (  # noqa: E402
    CandidateApplicationStatus,
    Job,
    JobStatus,
)
from jobradar.db.repo import Repo, init_db, make_engine  # noqa: E402
from jobradar.pipeline.schemas import JobPosting  # noqa: E402
from jobradar.services.matching_service import CandidateMatchingService  # noqa: E402
from jobradar.web.session import sign_candidate_session  # noqa: E402

DEFAULT_DEMO_DB = PROJECT_ROOT / "data" / "demo" / "jobradar_demo.db"
PRODUCTION_DB = PROJECT_ROOT / "data" / "jobradar.db"


def evidence(sentence: str, section: str = "Requirements") -> dict[str, str]:
    """Helper to structure requirement evidence citations."""
    return {"sentence": sentence, "section": section, "source": "page"}


def skill_req(*skills: str, relation: str = "all") -> dict[str, Any]:
    """Helper to structure skill requirements."""
    return {
        "skills": list(skills),
        "relation": relation,
        "evidence": evidence(f"Required skills: {', '.join(skills)}."),
    }


def make_demo_posting(
    job_id: str,
    title: str,
    company: str,
    tracks: list[tuple[str, str]],
    required_skills: list[dict[str, Any]],
    batch_years: list[int] | None = None,
    min_exp: float = 0.0,
    max_exp: float = 1.0,
    degrees: list[str] | None = None,
    locations: list[str] | None = None,
    custom_evidence: str | None = None,
) -> tuple[str, JobPosting]:
    """Build a validated synthetic JobPosting for demonstration purposes."""
    exp_evidence_text = (
        custom_evidence
        if custom_evidence
        else f"{min_exp} to {max_exp} years of professional experience required."
    )
    reqs = {
        "required_skills": required_skills,
        "preferred_skills": [],
        "experience": [
            {
                "need": "required",
                "experience_type": "professional",
                "minimum_years": min_exp,
                "maximum_years": max_exp,
                "evidence": evidence(exp_evidence_text),
            }
        ],
        "experience_status": "known",
        "degrees": degrees or ["B.Tech", "BE", "B.S."],
    }
    track_roles = [
        {
            "track": trk,
            "strength": st,
            "evidence": [evidence(f"Responsibilities align with {trk}.", "Role")],
        }
        for trk, st in tracks
    ]
    data = {
        "company": company,
        "role": title,
        "employment_type": "internship" if max_exp <= 1.0 else "full_time",
        "batch_years": [] if batch_years is None else batch_years,
        "degrees": degrees or ["B.Tech", "BE", "B.S."],
        "locations": locations or ["Bengaluru, India", "Remote"],
        "work_mode": "hybrid",
        "role_tracks": track_roles,
        "requirements": reqs,
        "skills_required": [s for item in required_skills for s in item.get("skills", [])],
        "skills_preferred": [],
        "apply_url": f"https://example.com/demo/jobs/{job_id}",
    }
    return job_id, JobPosting.model_validate(data)


def seed_job_record(repo: Repo, job_id: str, posting: JobPosting) -> Job:
    """Insert or replace a job record into the isolated demo inventory."""
    now = to_iso(utcnow())
    job = Job(
        id=job_id,
        canonical_url=posting.apply_url or f"https://example.com/demo/jobs/{job_id}",
        role=posting.role,
        company=posting.company,
        status=JobStatus.NEW.value,
        score=85,
        score_reason="Demo inventory posting",
        data_json=json.dumps(posting.model_dump()),
        created_at=now,
        updated_at=now,
    )
    with repo.engine.begin() as conn:
        conn.exec_driver_sql(
            """
            INSERT OR REPLACE INTO job (
                id, canonical_url, role, company, status,
                score, score_reason, data_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.canonical_url,
                job.role,
                job.company,
                job.status,
                job.score,
                job.score_reason,
                job.data_json,
                job.created_at,
                job.updated_at,
            ),
        )
    return job


def seed_demo_environment(
    db_path: Path, reset: bool = False, as_of: date | None = None
) -> dict[str, Any]:
    """Execute deterministic seeding of the isolated demo environment."""
    resolved_target = db_path.resolve()
    resolved_prod = PRODUCTION_DB.resolve()

    # 1. Strict safety guard against production database tampering
    if resolved_target == resolved_prod:
        raise RuntimeError(
            "CRITICAL SAFETY VIOLATION: Target database path equals production database! "
            f"Target: {resolved_target}. Refusing to proceed."
        )

    if "demo" not in str(resolved_target).lower() and "sandbox" not in str(resolved_target).lower():
        raise RuntimeError(
            "CRITICAL SAFETY VIOLATION: Database path must contain 'demo' or 'sandbox'. "
            f"Provided: {resolved_target}"
        )

    print(f"[Safety Check] Target demo database verified: {resolved_target}")
    print(f"[Safety Check] Production database protected:  {resolved_prod}")

    # 2. Reset logic if requested
    if reset and resolved_target.exists():
        print(f"[Reset] Removing existing demo database: {resolved_target}")
        resolved_target.unlink()
        for ext in ["-shm", "-wal"]:
            sidecar = resolved_target.with_name(resolved_target.name + ext)
            if sidecar.exists():
                sidecar.unlink()

    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(resolved_target)
    init_db(engine)
    repo = Repo(engine)

    # Configure web token for demo web access
    # (environment override, existing setting, or secure random token)
    demo_web_token = (
        os.environ.get("JOBRADAR_WEB_TOKEN")
        or repo.get_setting("web_token")
        or secrets.token_urlsafe(24)
    )
    repo.set_setting("web_token", demo_web_token)

    # 3. Create synthetic candidate: Alex Dev
    candidate = repo.create_candidate(
        name="Alex Dev",
        email="alex.demo@synthetic-test.local",
    )
    print(f"[Candidate] Created synthetic candidate: {candidate.name} ({candidate.id})")

    # 4. Create and confirm candidate profile
    profile_data = {
        "basics": {
            "name": "Alex Dev",
            "email": "alex.demo@synthetic-test.local",
            "phone": "+91 98765 43210",
            "location": "Bengaluru, Karnataka, India",
            "github": "https://github.com/alex-dev-synthetic",
            "linkedin": "https://linkedin.com/in/alex-dev-synthetic",
        },
        "education": [
            {
                "degree": "B.Tech",
                "branch": "Computer Science and Engineering",
                "institution": "National Institute of Technology",
                "graduation_year": 2026,
                "cgpa": "8.9/10",
            }
        ],
        "skills": [
            "Python",
            "FastAPI",
            "Docker",
            "PostgreSQL",
            "Redis",
            "PyTorch",
            "Pytest",
            "Git",
            "REST APIs",
        ],
        "experience": [
            {
                "title": "Software Engineering Intern",
                "company": "[Demo] Synthetix Labs",
                "location": "Bengaluru, India",
                "start_date": "2025-01",
                "end_date": "Present",
                "bullets": [
                    "Engineered asynchronous microservices using Python, FastAPI, and PostgreSQL.",
                    "Implemented Redis caching reducing average API response latency by 42%.",
                    "Authored automated integration suites using Pytest maintaining 95% coverage.",
                ],
            }
        ],
        "projects": [
            {
                "name": "Distributed Stream Processing Pipeline",
                "technologies": ["Python", "FastAPI", "Redis", "Docker"],
                "description": "High-throughput asynchronous event ingest and processing pipeline.",
            },
            {
                "name": "Neural Semantic Search Service",
                "technologies": ["PyTorch", "Python", "FastAPI"],
                "description": "Vector similarity ranking microservice for technical documents.",
            },
        ],
        "target_roles": ["AI Backend Engineer", "Full Stack Engineer", "Software Engineer"],
        "preferences": {
            "locations": ["Bengaluru, India", "Remote"],
            "roles": ["AI Backend Engineer", "Software Engineer"],
            "remote": True,
            "work_mode": ["hybrid", "remote"],
        },
    }

    draft_profile = repo.save_profile_draft(candidate.id, profile_data=profile_data)
    confirmed_profile = repo.confirm_profile(draft_profile.id, profile_data)
    print(
        f"[Profile] Confirmed profile v{confirmed_profile.version} for candidate {candidate.name}"
    )

    # 5. Define the 6 synthetic demonstration postings
    demo_jobs = [
        # Scenario 1: Eligible, AI Backend Track (Confirmed Match)
        make_demo_posting(
            job_id="demo_job_01_stripe_ai",
            title="AI Systems Engineer",
            company="[Demo] Stripe Technologies",
            tracks=[("ai_backend", "strong"), ("sde", "strong")],
            required_skills=[
                skill_req("Python", "FastAPI"),
                skill_req("Docker", "PostgreSQL"),
            ],
            batch_years=[2025, 2026],
            min_exp=0.0,
            max_exp=1.0,
            locations=["Bengaluru, India", "Remote"],
            custom_evidence="0 to 1 years experience. Target batches: 2025 and 2026 graduates.",
        ),
        # Scenario 2: Eligible, Full Stack Track (Confirmed Match)
        make_demo_posting(
            job_id="demo_job_02_vercel_fs",
            title="Full Stack Engineer",
            company="[Demo] Vercel Cloud",
            tracks=[("full_stack", "strong"), ("sde", "possible")],
            required_skills=[
                skill_req("Python", "FastAPI"),
                skill_req("PostgreSQL", "REST APIs"),
            ],
            batch_years=[2026],
            min_exp=0.0,
            max_exp=1.0,
            locations=["Bengaluru, India", "Remote"],
            custom_evidence="Targeting 2026 college graduates for Full Stack product engineering.",
        ),
        # Scenario 3: Needs Verification, AI Backend Track (Opportunity Discovery)
        make_demo_posting(
            job_id="demo_job_03_databricks_dist",
            title="Distributed Platform Engineer",
            company="[Demo] Databricks Systems",
            tracks=[("ai_backend", "strong"), ("sde", "strong")],
            required_skills=[
                skill_req("Python", "Redis"),
                skill_req("Docker", "FastAPI"),
            ],
            batch_years=[],  # Unspecified graduation year triggers NEEDS_VERIFICATION
            min_exp=0.0,
            max_exp=1.5,
            locations=["Bengaluru, India", "Remote"],
            custom_evidence="Early career role. Graduation batch unspecified.",
        ),
        # Scenario 4: Needs Verification, SDE Systems Track (Opportunity Discovery)
        make_demo_posting(
            job_id="demo_job_04_cloudflare_infra",
            title="Backend Infrastructure Engineer",
            company="[Demo] Cloudflare Edge",
            tracks=[("sde", "strong"), ("ai_backend", "possible")],
            required_skills=[
                skill_req("Python", "PostgreSQL"),
                skill_req("Git", "Docker"),
            ],
            batch_years=[],  # Unspecified graduation year triggers NEEDS_VERIFICATION
            min_exp=0.0,
            max_exp=1.5,
            locations=["Bengaluru, India", "Remote"],
            custom_evidence="Infrastructure engineering position. Graduation year not specified.",
        ),
        # Scenario 5: Not Eligible, Experience Ceiling & Batch Mismatch (Ineligible Transparency)
        make_demo_posting(
            job_id="demo_job_05_oracle_arch",
            title="Lead Backend Architect",
            company="[Demo] Oracle Enterprise",
            tracks=[("sde", "strong"), ("ai_backend", "possible")],
            required_skills=[
                skill_req("Python", "Docker"),
            ],
            batch_years=[2020, 2021, 2022],  # Excludes 2026
            min_exp=5.0,  # Requires 5+ years experience
            max_exp=10.0,
            locations=["Bengaluru, India"],
            custom_evidence=(
                "Minimum 5 years of professional backend architecture experience. "
                "Target graduation batch 2020 to 2022."
            ),
        ),
        # Scenario 6: Eligible, SDET Track (Track Filtering Demo)
        make_demo_posting(
            job_id="demo_job_06_rubrik_sdet",
            title="SDET / Automation Engineer",
            company="[Demo] Rubrik Core",
            tracks=[("sdet", "strong"), ("sde", "possible")],
            required_skills=[
                skill_req("Python", "Pytest"),
                skill_req("Git", "Docker"),
            ],
            batch_years=[2026],
            min_exp=0.0,
            max_exp=1.0,
            locations=["Bengaluru, India", "Remote"],
            custom_evidence=(
                "Campus hiring 2026 batch for automated test and platform quality engineering."
            ),
        ),
    ]

    # 6. Seed jobs into global inventory
    for jid, posting in demo_jobs:
        seed_job_record(repo, jid, posting)
        print(f"[Inventory] Seeded job: {posting.role} at {posting.company} ({jid})")

    # 7. Evaluate real assessments using CandidateMatchingService
    matching_service = CandidateMatchingService(repo)
    eval_date = as_of or date(2026, 11, 27)
    assessments = matching_service.assess_candidate_jobs(
        candidate_id=candidate.id,
        jobs=demo_jobs,
        profile_version=confirmed_profile.version,
        as_of=eval_date,
    )
    print(f"[Assessments] Generated {len(assessments)} track evaluations across 4 J4 tracks.")

    # 8. Create application tracking progression for demo
    # Job 1: Saved -> Applied -> Interviewing
    repo.save_candidate_application(
        candidate_id=candidate.id,
        job_id="demo_job_01_stripe_ai",
        status=CandidateApplicationStatus.SAVED.value,
        notes="Identified as top match for AI Systems role.",
    )
    repo.save_candidate_application(
        candidate_id=candidate.id,
        job_id="demo_job_01_stripe_ai",
        status=CandidateApplicationStatus.APPLIED.value,
        notes="Application submitted with tailored resume.",
    )
    repo.save_candidate_application(
        candidate_id=candidate.id,
        job_id="demo_job_01_stripe_ai",
        status=CandidateApplicationStatus.INTERVIEWING.value,
        notes="Technical screening completed; system design round scheduled.",
    )

    # Job 2: Applied
    repo.save_candidate_application(
        candidate_id=candidate.id,
        job_id="demo_job_02_vercel_fs",
        status=CandidateApplicationStatus.APPLIED.value,
        notes="Applied via employee referral on careers portal.",
    )
    print("[Applications] Configured application tracking progression and history logs.")

    # Generate session cookie for automated verification and demo login
    session_cookie = sign_candidate_session(candidate.id, demo_web_token)

    summary = {
        "database_path": str(resolved_target),
        "candidate_id": candidate.id,
        "candidate_name": candidate.name,
        "candidate_email": candidate.email,
        "profile_version": confirmed_profile.version,
        "jobs_seeded": len(demo_jobs),
        "assessments_created": len(assessments),
        "web_token": demo_web_token,
        "session_cookie": session_cookie,
    }

    print("\n" + "=" * 60)
    print("DEMO ENVIRONMENT SEEDING COMPLETE")
    print("=" * 60)
    print(f"Database:        {summary['database_path']}")
    print(f"Candidate:       {summary['candidate_name']} ({summary['candidate_id']})")
    print(f"Jobs Seeded:     {summary['jobs_seeded']}")
    print(f"Assessments:     {summary['assessments_created']}")
    print(f"Session Cookie:  jobradar_candidate_session={summary['session_cookie']}")
    print("=" * 60)

    return summary


def verify_demo_environment(db_path: Path) -> bool:
    """Verify integrity and expected scenario distribution of the demo database."""
    resolved = db_path.resolve()
    if not resolved.exists():
        print(f"[Verification Failed] Database file does not exist: {resolved}")
        return False

    engine = make_engine(resolved)
    repo = Repo(engine)

    candidates = repo.list_candidates()
    if not candidates:
        print("[Verification Failed] No candidates found in demo database.")
        return False

    cand = candidates[0]
    prof = repo.get_active_profile(cand.id)
    if not prof or prof.review_status != "confirmed":
        print(f"[Verification Failed] Active confirmed profile missing for {cand.id}")
        return False

    assessments = repo.list_candidate_assessments(cand.id)
    if len(assessments) < 24:  # 6 jobs * 4 tracks
        print(f"[Verification Failed] Expected at least 24 assessments, found {len(assessments)}")
        return False

    # Check eligibility verdicts
    verdicts = {a.eligibility_status for a in assessments}
    print(f"[Verification] Observed eligibility statuses: {verdicts}")
    if "ELIGIBLE" not in verdicts or "NEEDS_VERIFICATION" not in verdicts:
        print("[Verification Failed] Missing expected eligibility verdicts in assessments.")
        return False

    # Check applications
    apps = repo.list_candidate_applications(cand.id)
    if len(apps) < 2:
        print(f"[Verification Failed] Expected at least 2 applications, found {len(apps)}")
        return False

    history = repo.list_candidate_application_history(cand.id, "demo_job_01_stripe_ai")
    if len(history) < 3:
        print(f"[Verification Failed] Expected at least 3 history entries, found {len(history)}")
        return False

    print("[Verification Succeeded] Demo environment is fully initialized and valid.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare and seed isolated demo environment for JobRadar."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DEMO_DB,
        help="Path to demo SQLite database (must contain 'demo' or 'sandbox').",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset and recreate the demo database from scratch.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify existing demo database without re-seeding.",
    )

    args = parser.parse_args()

    if args.verify:
        success = verify_demo_environment(args.db)
        sys.exit(0 if success else 1)

    seed_demo_environment(args.db, reset=args.reset)
    success = verify_demo_environment(args.db)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
