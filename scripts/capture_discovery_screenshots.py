"""Capture discovery presentation screenshots using Playwright."""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, ".")
sys.path.insert(0, "src")

import uvicorn
from playwright.sync_api import sync_playwright
from sqlmodel import create_engine
from tests.test_digest import settings

from jobradar.clock import to_iso, utcnow
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo, init_db
from jobradar.pipeline.schemas import JobPosting
from jobradar.services.matching_service import CandidateMatchingService
from jobradar.web.app import create_app
from jobradar.web.session import CANDIDATE_SESSION_COOKIE, sign_candidate_session

SCREENSHOTS_DIR = Path("tests/screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def evidence(sentence: str, section: str = "Requirements") -> dict[str, str]:
    return {"sentence": sentence, "section": section, "source": "page"}


def skill_req(*skills: str) -> dict[str, Any]:
    return {
        "skills": list(skills),
        "relation": "all",
        "evidence": evidence(f"Required skills: {' or '.join(skills)}."),
    }


def make_posting(
    job_id: str,
    title: str,
    company: str,
    tracks: list[tuple[str, str]],
    required_skills: list[dict[str, Any]],
    batch_years: list[int] | None = None,
) -> tuple[str, JobPosting]:
    reqs = {
        "required_skills": required_skills,
        "preferred_skills": [],
        "experience": [
            {
                "need": "required",
                "experience_type": "professional",
                "minimum_years": 0.0,
                "maximum_years": 1.0,
                "evidence": evidence("0 to 1 years of experience required."),
            }
        ],
        "experience_status": "known",
        "degrees": ["B.Tech", "BE"],
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
        "employment_type": "internship",
        "batch_years": [] if batch_years is None else batch_years,
        "degrees": ["B.Tech", "BE"],
        "locations": ["Bengaluru, India", "Remote"],
        "work_mode": "hybrid",
        "role_tracks": track_roles,
        "requirements": reqs,
        "skills_required": [s for item in required_skills for s in item.get("skills", [])],
        "skills_preferred": [],
        "apply_url": f"https://example.com/jobs/{job_id}",
    }
    return job_id, JobPosting.model_validate(data)


def seed_job(repo: Repo, job_id: str, posting: JobPosting) -> None:
    now = to_iso(utcnow())
    job = Job(
        id=job_id,
        canonical_url=posting.apply_url or f"https://example.com/jobs/{job_id}",
        role=posting.role,
        company=posting.company,
        status=JobStatus.NEW.value,
        score=82,
        score_reason="Evaluated job",
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


def main() -> None:
    sandbox_db = Path("data/sandbox/screenshots.db")
    sandbox_db.parent.mkdir(parents=True, exist_ok=True)
    if sandbox_db.exists():
        sandbox_db.unlink()

    engine = create_engine(f"sqlite:///{sandbox_db}")
    init_db(engine)
    repo = Repo(engine)

    # 1. Create candidate
    cand = repo.create_candidate(name="Sameer", email="sameer@example.com")
    draft = repo.save_profile_draft(
        cand.id,
        profile_data={
            "basics": {
                "name": "Sameer",
                "email": "sameer@example.com",
                "location": "Bengaluru, India",
            },
            "education": [
                {
                    "degree": "B.Tech",
                    "branch": "Electrical Engineering",
                    "institution": "National Institute of Technology",
                    "graduation_year": 2026,
                    "cgpa": "8.7",
                }
            ],
            "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "PyTorch", "Redis"],
            "experience": [
                {
                    "title": "Backend Engineering Intern",
                    "company": "Nexus Systems",
                    "bullets": ["Engineered high-throughput RESTful services using FastAPI."],
                }
            ],
            "projects": [
                {
                    "name": "JobRadar Engine",
                    "technologies": ["Python", "FastAPI", "SQLite"],
                    "description": "Multi-track AI matching platform.",
                }
            ],
            "target_roles": ["Backend Engineer", "AI Engineer"],
            "preferences": {
                "locations": ["Bengaluru, India", "Remote"],
                "roles": ["Backend Engineer"],
                "remote": True,
            },
        },
    )
    prof = repo.confirm_profile(draft.id)

    # 2. Add jobs: Confirmed ELIGIBLE, NEEDS_VERIFICATION, NOT_ELIGIBLE
    jobs = [
        make_posting(
            "job_bosch_ai",
            "Software Engineer - AI Platform",
            "Bosch Global",
            [("ai_backend", "strong"), ("sde", "strong")],
            [skill_req("Python", "FastAPI"), skill_req("Docker")],
            batch_years=[2025, 2026],  # Explicitly matches 2026
        ),
        make_posting(
            "job_nexus_backend",
            "Junior Backend Developer",
            "Nexus AI Labs",
            [("ai_backend", "strong"), ("full_stack", "possible")],
            [skill_req("Python", "PostgreSQL")],
            batch_years=[],  # Unspecified graduation batch -> NEEDS_VERIFICATION
        ),
        make_posting(
            "job_cloud_scale",
            "Platform Systems Engineer",
            "CloudScale Systems",
            [("sde", "strong"), ("ai_backend", "strong")],
            [skill_req("Python", "Redis")],
            batch_years=[],  # Unspecified graduation batch -> NEEDS_VERIFICATION
        ),
        make_posting(
            "job_legacy_batch",
            "Senior Infrastructure Associate",
            "Enterprise Dynamics",
            [("ai_backend", "strong")],
            [skill_req("Python")],
            batch_years=[2022, 2023, 2024],  # Excludes 2026 -> NOT_ELIGIBLE
        ),
    ]

    for jid, p in jobs:
        seed_job(repo, jid, p)

    service = CandidateMatchingService(repo)
    service.assess_candidate_jobs(cand.id, jobs, profile_version=prof.version)

    # 3. Spin up local server on ephemeral port
    st = settings()
    app = create_app(st, repo)
    token = repo.get_setting("web_token") or "test_web_token"
    session_token = sign_candidate_session(cand.id, token)

    port = 8489
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.5)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_cookies(
                [
                    {
                        "name": CANDIDATE_SESSION_COOKIE,
                        "value": session_token,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            page = context.new_page()

            # 1. Default Match Radar (Confirmed Matches + Pending Verification with banner)
            page.goto(f"http://127.0.0.1:{port}/candidate/dashboard")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SCREENSHOTS_DIR / "discovery_match_radar_default.png"))
            print("Captured: discovery_match_radar_default.png")

            # 2. Filtered by Career Track: AI / Backend
            page.goto(f"http://127.0.0.1:{port}/candidate/dashboard?track=ai_backend")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SCREENSHOTS_DIR / "discovery_match_radar_track_ai.png"))
            print("Captured: discovery_match_radar_track_ai.png")

            # 3. Ineligible Roles Transparency Log
            page.goto(f"http://127.0.0.1:{port}/candidate/dashboard?eligibility=not_eligible")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SCREENSHOTS_DIR / "discovery_match_radar_ineligible.png"))
            print("Captured: discovery_match_radar_ineligible.png")

            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)
        engine.dispose()
        if sandbox_db.exists():
            with contextlib.suppress(OSError):
                sandbox_db.unlink()


if __name__ == "__main__":
    main()
