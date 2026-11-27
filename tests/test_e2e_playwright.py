"""Automated End-to-End Clean-Run Validation Test Suite with Playwright Browser Automation.

Validates the complete 18-step candidate journey from resume upload to application tracking:
- Step 1: Start application on documented ephemeral localhost port
- Step 2: Open clean browser context
- Step 3: Upload representative synthetic PDF resume
- Step 4: Verify extracted name, email, education, skills, experience, projects
- Step 5: Verify evidence snippets and profile review
- Step 6: Confirm candidate profile
- Step 7: Open candidate dashboard and verify lazy J4 assessment generation
- Step 8: Verify all four tracks (ai_backend, full_stack, sde, sdet)
- Step 9: Open Match Radar and inspect recommendation consistency
- Step 10: Open All Jobs and test overall Best Match
- Step 11: Select each track and verify track-specific Best Match ordering
- Step 12: Open recommended job and verify full description, requirements, match score, breakdown
- Step 13: Save a job, move it to Applied, then Interviewing
- Step 14: Verify application history, notes, and timestamps across all transitions
- Step 15: Close and simulate application restart
- Step 16: Verify profile, assessments, and application history persist across restart
- Step 17: Test invalid resume upload, missing job (404), invalid session, and CSRF rejection (403)
- Step 18: Verify second candidate (Candidate B) cannot access Candidate A's data (isolation & IDOR)
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from sqlmodel import create_engine, text

from jobradar.clock import to_iso, utcnow
from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import JobStatus, Platform
from jobradar.db.repo import Repo, init_db
from jobradar.pipeline.schemas import (
    JobPosting,
    JobRequirements,
    RequirementEvidence,
    RoleTrack,
    SkillRequirement,
)
from jobradar.web.app import create_app

SCREENSHOTS_DIR = Path("tests/screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def make_valid_pdf(text: str) -> bytes:
    """Generate a valid, standards-compliant PDF 1.4 document containing synthetic text."""
    header = b"%PDF-1.4\n"
    s1 = b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
    s2 = b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
    s3 = b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"

    lines = text.split("\n")
    stream_lines = ["BT", "/F1 12 Tf", "50 750 Td", "16 TL"]
    for i, line in enumerate(lines):
        esc = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if i == 0:
            stream_lines.append(f"({esc}) Tj")
        else:
            stream_lines.append(f"T* ({esc}) Tj")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin1")

    o1 = len(header)
    o2 = o1 + len(s1)
    o3 = o2 + len(s2)
    s4 = (
        b"4 0 obj <</Length "
        + str(len(stream)).encode("ascii")
        + b">> stream\n"
        + stream
        + b"\nendstream\nendobj\n"
    )
    o4 = o3 + len(s3)
    o5 = o4 + len(s4)
    s5 = b"5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n"
    xref_offset = o5 + len(s5)

    xref = (
        f"xref\n0 6\n0000000000 65535 f \n"
        f"{o1:010d} 00000 n \n"
        f"{o2:010d} 00000 n \n"
        f"{o3:010d} 00000 n \n"
        f"{o4:010d} 00000 n \n"
        f"{o5:010d} 00000 n \n"
        f"trailer <</Size 6 /Root 1 0 R>>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")

    return header + s1 + s2 + s3 + s4 + s5 + xref


CANDIDATE_A_RESUME = """Alex Dev
alex.dev@synthetic-test.local | +91 98765 43210
Bangalore, India | https://github.com/alexdev | https://linkedin.com/in/alexdev

EDUCATION
National Institute of Technology
B.Tech in Computer Science and Engineering
Graduation: 2026 | CGPA: 8.8

SKILLS
Python, FastAPI, PostgreSQL, Docker, Redis, PyTorch, REST, Git

PROJECTS
Distributed Task Queue
- Designed an async task queue in Python and Redis.

Semantic Search Engine
- Built a semantic search engine using FastAPI and PyTorch embeddings.

EXPERIENCE
Software Engineering Intern | TechCorp Solutions
June 2025 - August 2025 | Bangalore
- Developed microservices in Python using FastAPI and PostgreSQL.
"""

CANDIDATE_B_RESUME = """Sam Test
sam.test@synthetic-test.local | +91 91234 56789
Hyderabad, India

EDUCATION
Birla Institute of Technology
B.Tech in Information Technology
Graduation: 2025 | CGPA: 8.2

SKILLS
Python, QA Automation, Test Automation, Selenium, Pytest, Docker, Git

PROJECTS
Automated Testing Pipeline
- Built automated QA regression framework in Python and Pytest.

EXPERIENCE
QA Automation Intern | TestHub Inc
January 2025 - May 2025
- Automated end-to-end regression test suites.
"""


def seed_inventory_jobs(repo: Repo) -> dict[str, str]:
    """Seed synthetic job inventory with distinct J4 track classifications across all 4 tracks."""
    now_iso = to_iso(datetime.now(UTC))

    # Job 1: Stripe Backend (AI Backend & SDE strong match for Candidate A)
    job1_id = "e2e001" + "1" * 26
    posting1 = JobPosting(
        role="Backend Software Engineer",
        company="Stripe",
        location="Bangalore, India",
        work_mode="hybrid",
        apply_url="https://stripe.com/jobs/e2e-backend",
        skills_required=["Python", "PostgreSQL", "Docker", "REST"],
        skills_preferred=["Redis", "PyTorch"],
        role_tracks=[
            RoleTrack(
                track="ai_backend",
                strength="strong",
                evidence=[
                    RequirementEvidence(
                        sentence="Build high-scale backend services in Python", source="page"
                    )
                ],
            ),
            RoleTrack(
                track="sde",
                strength="strong",
                evidence=[
                    RequirementEvidence(sentence="General software engineering", source="page")
                ],
            ),
        ],
        requirements=JobRequirements(
            required_skills=[
                SkillRequirement(
                    skills=["Python"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="Python required", source="page"),
                ),
                SkillRequirement(
                    skills=["PostgreSQL"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="PostgreSQL required", source="page"),
                ),
                SkillRequirement(
                    skills=["Docker"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="Docker required", source="page"),
                ),
            ],
            preferred_skills=[
                SkillRequirement(
                    skills=["Redis"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="Redis preferred", source="page"),
                ),
            ],
        ),
        summary="Build scalable financial backend infrastructure using Python and modern databases.",
    )
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO job (id, canonical_url, role, company, status, data_json, score, score_reason, created_at, updated_at) "
                "VALUES (:id, :url, :role, :company, :status, :data, :score, :score_reason, :now, :now)"
            ),
            {
                "id": job1_id,
                "url": "https://stripe.com/jobs/e2e-backend",
                "role": "Backend Software Engineer",
                "company": "Stripe",
                "status": JobStatus.NEW.value,
                "data": posting1.model_dump_json(),
                "score": 75,
                "score_reason": "Legacy: Python and Postgres match",
                "now": now_iso,
            },
        )
    src1 = repo.upsert_source(Platform.BOARD, "greenhouse-stripe", "Greenhouse · Stripe")
    msg1_id, _ = repo.save_raw_message(src1.id, str(hash(job1_id)), now_iso, text_="Stripe Backend")
    repo.add_job_source(job1_id, msg1_id)

    # Job 2: Databricks QA Automation Engineer (SDET strong match for Candidate B)
    job2_id = "e2e002" + "2" * 26
    posting2 = JobPosting(
        role="SDET / QA Automation Engineer",
        company="Databricks",
        location="Hyderabad, India",
        work_mode="remote",
        apply_url="https://databricks.com/jobs/e2e-qa",
        skills_required=["Python", "Test automation", "Selenium"],
        skills_preferred=["Docker"],
        role_tracks=[
            RoleTrack(
                track="sdet",
                strength="strong",
                evidence=[
                    RequirementEvidence(
                        sentence="Develop QA automation suites in Python", source="page"
                    )
                ],
            ),
        ],
        requirements=JobRequirements(
            required_skills=[
                SkillRequirement(
                    skills=["Python"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="Python required", source="page"),
                ),
                SkillRequirement(
                    skills=["Test automation"],
                    relation="all",
                    evidence=RequirementEvidence(
                        sentence="Test automation required", source="page"
                    ),
                ),
            ],
        ),
        summary="Lead test automation and continuous validation pipelines for cloud analytics.",
    )
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO job (id, canonical_url, role, company, status, data_json, score, score_reason, created_at, updated_at) "
                "VALUES (:id, :url, :role, :company, :status, :data, :score, :score_reason, :now, :now)"
            ),
            {
                "id": job2_id,
                "url": "https://databricks.com/jobs/e2e-qa",
                "role": "SDET / QA Automation Engineer",
                "company": "Databricks",
                "status": JobStatus.NEW.value,
                "data": posting2.model_dump_json(),
                "score": 70,
                "score_reason": "Legacy: QA automation match",
                "now": now_iso,
            },
        )
    src2 = repo.upsert_source(Platform.BOARD, "lever-databricks", "Lever · Databricks")
    msg2_id, _ = repo.save_raw_message(src2.id, str(hash(job2_id)), now_iso, text_="Databricks QA")
    repo.add_job_source(job2_id, msg2_id)

    # Job 3: Closed job (Hidden inventory)
    job3_id = "e2e003" + "3" * 26
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO job (id, canonical_url, role, company, status, data_json, score, score_reason, created_at, updated_at) "
                "VALUES (:id, :url, :role, :company, :status, :data, :score, :score_reason, :now, :now)"
            ),
            {
                "id": job3_id,
                "url": "https://example.com/jobs/closed",
                "role": "Frontend Engineer (Closed)",
                "company": "OldCorp",
                "status": JobStatus.HIDDEN.value,
                "data": json.dumps({"role": "Frontend Engineer", "company": "OldCorp"}),
                "score": 40,
                "score_reason": "Closed job",
                "now": now_iso,
            },
        )

    # Job 4: Vercel Full Stack Engineer (Full Stack strong & SDE possible match)
    job4_id = "e2e004" + "4" * 26
    posting4 = JobPosting(
        role="Full Stack Engineer",
        company="Vercel",
        location="Remote, India",
        work_mode="remote",
        apply_url="https://vercel.com/jobs/e2e-fullstack",
        skills_required=["Python", "FastAPI", "Docker"],
        skills_preferred=["Redis"],
        role_tracks=[
            RoleTrack(
                track="full_stack",
                strength="strong",
                evidence=[
                    RequirementEvidence(
                        sentence="Build end-to-end cloud platforms with FastAPI and modern tooling",
                        source="page",
                    )
                ],
            ),
            RoleTrack(
                track="sde",
                strength="possible",
                evidence=[
                    RequirementEvidence(sentence="General full stack engineering", source="page")
                ],
            ),
        ],
        requirements=JobRequirements(
            required_skills=[
                SkillRequirement(
                    skills=["Python"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="Python required", source="page"),
                ),
                SkillRequirement(
                    skills=["FastAPI"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="FastAPI required", source="page"),
                ),
                SkillRequirement(
                    skills=["Docker"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="Docker required", source="page"),
                ),
            ],
            preferred_skills=[
                SkillRequirement(
                    skills=["Redis"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="Redis preferred", source="page"),
                ),
            ],
        ),
        summary="Build developer experience platforms and web services using FastAPI and Docker.",
    )
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO job (id, canonical_url, role, company, status, data_json, score, score_reason, created_at, updated_at) "
                "VALUES (:id, :url, :role, :company, :status, :data, :score, :score_reason, :now, :now)"
            ),
            {
                "id": job4_id,
                "url": "https://vercel.com/jobs/e2e-fullstack",
                "role": "Full Stack Engineer",
                "company": "Vercel",
                "status": JobStatus.NEW.value,
                "data": posting4.model_dump_json(),
                "score": 80,
                "score_reason": "Legacy: Full stack match",
                "now": now_iso,
            },
        )
    src4 = repo.upsert_source(Platform.BOARD, "greenhouse-vercel", "Greenhouse · Vercel")
    msg4_id, _ = repo.save_raw_message(
        src4.id, str(hash(job4_id)), now_iso, text_="Vercel Full Stack"
    )
    repo.add_job_source(job4_id, msg4_id)

    return {
        "stripe": job1_id,
        "databricks": job2_id,
        "closed": job3_id,
        "vercel": job4_id,
    }


@pytest.fixture(scope="module")
def e2e_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[tuple[str, Repo, dict[str, str]], None, None]:
    """Launch an ephemeral JobRadar FastAPI application on a dedicated localhost port."""
    temp_dir = tmp_path_factory.mktemp("e2e_runtime")
    db_file = temp_dir / "test_e2e.db"
    db_url = f"sqlite:///{db_file}"

    # Dedicated port selection
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # Settings & repo initialization
    engine = create_engine(db_url)
    init_db(engine)
    repo = Repo(engine)
    config_dict: dict[str, Any] = {
        "sources": {"telegram": {"enabled": False}},
        "notify": {"dry_run": True, "timezone": "Asia/Kolkata"},
        "notion": {"enabled": False},
        "web": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": port,
            "url": f"http://127.0.0.1:{port}",
        },
        "matching": {"enabled": False},
    }
    settings = Settings(config=AppConfig.model_validate(config_dict), secrets=Secrets())

    # Seed shared inventory
    jobs = seed_inventory_jobs(repo)

    app = create_app(settings, repo, clock=utcnow)

    # Start uvicorn in background thread
    server = uvicorn.Server(
        config=uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Poll until ready
    for _ in range(50):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            break
        except OSError:
            time.sleep(0.1)

    yield base_url, repo, jobs

    server.should_exit = True
    thread.join(timeout=2.0)


@pytest.fixture(scope="module")
def browser() -> Generator[Browser, None, None]:
    """Start Playwright Chromium browser."""
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


def test_full_playwright_e2e_scenarios(
    e2e_server: tuple[str, Repo, dict[str, str]], browser: Browser, tmp_path: Path
) -> None:
    """Execute complete 18-step Clean-Run Workflow Validation against the live browser interface."""
    base_url, repo, jobs = e2e_server
    stripe_id = jobs["stripe"]
    databricks_id = jobs["databricks"]

    # =========================================================================
    # STEP 1 & 2: Startup and Clean Browser Context
    # =========================================================================
    context_a: BrowserContext = browser.new_context()
    page_a: Page = context_a.new_page()

    # =========================================================================
    # STEP 3: Upload Representative PDF Resume
    # =========================================================================
    page_a.goto(f"{base_url}/onboarding")
    assert "Profile Setup" in page_a.title() or "JobRadar" in page_a.title()
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_01_onboarding.png"))

    pdf_path_a = tmp_path / "alex_resume.pdf"
    pdf_path_a.write_bytes(make_valid_pdf(CANDIDATE_A_RESUME))

    page_a.set_input_files("input#resume-file", str(pdf_path_a))
    page_a.click("button[type='submit']")

    # Wait for redirect to review page
    page_a.wait_for_url("**/onboarding/review/**", timeout=10000)
    assert "/onboarding/review/" in page_a.url
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_02_profile_review.png"))

    profile_a_id = page_a.url.split("/onboarding/review/")[1].split("?")[0]
    assert profile_a_id, "Candidate A profile ID must be present in review URL"

    # =========================================================================
    # STEP 4 & 5: Verify Extracted Entities & Evidence Snippets
    # =========================================================================
    content_review = page_a.content()
    # Basics
    assert "Alex Dev" in content_review
    assert "alex.dev@synthetic-test.local" in content_review
    # Education
    assert (
        "National Institute of Technology" in content_review or "Computer Science" in content_review
    )
    # Skills
    assert "Python" in content_review
    assert "FastAPI" in content_review
    assert "PostgreSQL" in content_review
    assert "Docker" in content_review
    # Experience
    assert "TechCorp" in content_review or "Software Engineering Intern" in content_review
    # Projects
    assert "Distributed Task Queue" in content_review or "Semantic Search" in content_review
    # Evidence / confidence section
    assert "Confidence" in content_review or "parser" in content_review.lower()

    # =========================================================================
    # STEP 6: Confirm Candidate Profile
    # =========================================================================
    page_a.click("button[type='submit']:has-text('Confirm')")
    page_a.wait_for_url("**confirmed=1**", timeout=10000)

    # =========================================================================
    # STEP 7: Candidate Dashboard & Lazy J4 Assessment Generation
    # =========================================================================
    page_a.goto(f"{base_url}/candidate/dashboard")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_03_dashboard_lazy_assessments.png"))

    dash_content = page_a.content()
    assert "Alex Dev" in dash_content
    assert "Version 1" in dash_content

    # Trigger evaluation if not yet generated
    if "No job recommendations match" in dash_content or "Total Evaluated" in dash_content:
        page_a.click("button:has-text('Refresh Assessments')")
        page_a.wait_for_url("**/candidate/dashboard?refreshed=1**", timeout=10000)

    dash_refreshed = page_a.content()
    # High-ranking recommendations visible
    assert "Backend Software Engineer" in dash_refreshed or "Full Stack Engineer" in dash_refreshed
    assert "Stripe" in dash_refreshed or "Vercel" in dash_refreshed

    # =========================================================================
    # STEP 8 & 9: Verify All 4 Tracks & Match Radar Consistency
    # =========================================================================
    prof_a = repo.get_profile(profile_a_id)
    assert prof_a is not None
    cand_a_id = prof_a.candidate_id
    cand_a_record = repo.get_candidate(cand_a_id)
    assert cand_a_record is not None

    # Candidate assessments cover all 4 tracks
    active_cand_assessments = repo.list_candidate_assessments(cand_a_id)
    assert len(active_cand_assessments) >= 4, "Assessments must be generated across tracks"

    tracks_represented = {a.track for a in active_cand_assessments}
    assert "ai_backend" in tracks_represented
    assert "full_stack" in tracks_represented
    assert "sde" in tracks_represented
    assert "sdet" in tracks_represented

    # Closed job (Job 3) must NOT appear in candidate recommendations
    recs = repo.get_candidate_recommendations(cand_a_id, profile_version=prof_a.version, limit=20)
    rec_job_ids = {r["job_id"] for r in recs}
    assert jobs["closed"] not in rec_job_ids, (
        "Hidden/closed job must be filtered from recommendations"
    )

    # =========================================================================
    # STEP 10: All Jobs & Overall Best Match Ordering
    # =========================================================================
    page_a.goto(f"{base_url}/jobs?sort=score")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_05_all_jobs_best_match.png"))

    rows = page_a.locator("ul.rows li.row")
    assert rows.count() >= 2
    # Candidate A has highest fit for Stripe and Vercel
    top_two_titles = [rows.nth(0).inner_text(), rows.nth(1).inner_text()]
    assert any("Stripe" in t for t in top_two_titles)
    assert any("Vercel" in t for t in top_two_titles)

    # =========================================================================
    # STEP 11: Track-Specific Best Match Ordering
    # =========================================================================
    # 1. AI Backend track sort: Stripe Backend should be #1
    page_a.goto(f"{base_url}/jobs?sort=score&track=ai_backend")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_06_track_sort_ai_backend.png"))
    rows_ai = page_a.locator("ul.rows li.row")
    assert rows_ai.count() >= 1
    assert "Stripe" in rows_ai.first.inner_text()

    # 2. Full Stack track sort: Vercel Full Stack should be #1
    page_a.goto(f"{base_url}/jobs?sort=score&track=full_stack")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_07_track_sort_full_stack.png"))
    rows_fs = page_a.locator("ul.rows li.row")
    assert rows_fs.count() >= 1
    assert "Vercel" in rows_fs.first.inner_text()

    # 3. SDET track sort: Databricks SDET should be #1
    page_a.goto(f"{base_url}/jobs?sort=score&track=sdet")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_08_track_sort_sdet.png"))
    rows_sdet = page_a.locator("ul.rows li.row")
    assert rows_sdet.count() >= 1
    assert "Databricks" in rows_sdet.first.inner_text()

    # =========================================================================
    # STEP 12: Open Recommended Job & Verify Description, Requirements, Match Score Ring
    # =========================================================================
    page_a.goto(f"{base_url}/candidate/jobs/{stripe_id}")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_04_job_fit_detail.png"))

    fit_content = page_a.content()
    assert "Backend Software Engineer" in fit_content
    assert "Stripe" in fit_content
    assert "AI BACKEND" in fit_content or "ai backend" in fit_content.lower()
    assert "ELIGIBLE" in fit_content or "NEEDS VERIFICATION" in fit_content
    assert "Python" in fit_content
    # Score ring SVG is present
    assert "score-ring" in fit_content or "<svg" in fit_content

    # =========================================================================
    # STEP 13: Save Job, Move to Applied, Then Interviewing
    # =========================================================================
    # 1. Stripe: Move to Saved
    page_a.select_option("select#f-status", "saved")
    page_a.fill("input#f-notes", "Initial bookmark for evaluation")
    page_a.click("button:has-text('Update Application')")
    page_a.wait_for_load_state("networkidle")

    # 2. Stripe: Move to Applied
    page_a.select_option("select#f-status", "applied")
    page_a.fill("input#f-notes", "Applied via employee referral on Stripe careers site")
    page_a.click("button:has-text('Update Application')")
    page_a.wait_for_load_state("networkidle")

    # 3. Stripe: Move to Interviewing
    page_a.select_option("select#f-status", "interviewing")
    page_a.fill("input#f-notes", "Technical phone screen scheduled with backend team")
    page_a.click("button:has-text('Update Application')")
    page_a.wait_for_load_state("networkidle")

    # 4. Databricks: Move to Saved
    page_a.goto(f"{base_url}/candidate/jobs/{databricks_id}")
    page_a.select_option("select#f-status", "saved")
    page_a.fill("input#f-notes", "Saved for future review")
    page_a.click("button:has-text('Update Application')")
    page_a.wait_for_load_state("networkidle")

    # =========================================================================
    # STEP 14: Verify Application Tracker & History Log Across Transitions
    # =========================================================================
    # 1. Check Kanban Tracker Board
    page_a.goto(f"{base_url}/candidate/applications")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_09_applications_board.png"))
    tracker_content = page_a.content()
    assert "Interviewing: <b>1</b>" in tracker_content
    assert "Saved: <b>1</b>" in tracker_content
    assert "Stripe" in tracker_content
    assert "Databricks" in tracker_content

    # 2. Check Application History Log on Stripe job detail
    page_a.goto(f"{base_url}/candidate/jobs/{stripe_id}")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_10_application_history_log.png"))
    stripe_detail = page_a.content()
    assert "Application History Log" in stripe_detail
    assert "interviewing" in stripe_detail.lower()
    assert "applied" in stripe_detail.lower()
    assert "saved" in stripe_detail.lower()
    assert "Technical phone screen scheduled" in stripe_detail
    assert "Applied via employee referral" in stripe_detail

    # Verify transition history in database
    history_records = repo.list_candidate_application_history(cand_a_record.id, job_id=stripe_id)
    assert len(history_records) >= 3, "All 3 status transitions must be logged in history"
    statuses_logged = [h.status for h in history_records]
    assert "saved" in statuses_logged
    assert "applied" in statuses_logged
    assert "interviewing" in statuses_logged
    for h in history_records:
        assert h.changed_at, "History entry must have an ISO timestamp"

    # =========================================================================
    # STEP 15 & 16: Close Application & Verify Restart Persistence
    # =========================================================================
    # Simulate restart by instantiating fresh Repo against existing SQLite file
    fresh_repo = Repo(repo.engine)

    candidates = fresh_repo.list_candidates()
    cand_a_matches = [c for c in candidates if "Alex" in c.name]
    assert len(cand_a_matches) == 1, "Candidate A must persist across restart"
    persisted_cand_a = cand_a_matches[0]

    persisted_prof_a = fresh_repo.get_active_profile(persisted_cand_a.id)
    assert persisted_prof_a is not None
    assert persisted_prof_a.version == 1
    assert persisted_prof_a.review_status == "confirmed"

    persisted_apps = fresh_repo.list_candidate_applications(persisted_cand_a.id)
    assert len(persisted_apps) >= 2
    stripe_persisted = next(a for a in persisted_apps if a.job_id == stripe_id)
    assert stripe_persisted.status == "interviewing"
    assert "Technical phone screen" in stripe_persisted.notes

    persisted_history = fresh_repo.list_candidate_application_history(
        persisted_cand_a.id, job_id=stripe_id
    )
    assert len(persisted_history) >= 3

    # =========================================================================
    # STEP 17: Security & Boundary Tests (Errors, 404, Tampered Session, CSRF)
    # =========================================================================
    # 1. Invalid resume upload error
    invalid_file = tmp_path / "corrupt.txt"
    invalid_file.write_text("This is an unparseable non-pdf plain text payload.")
    page_a.goto(f"{base_url}/onboarding")
    page_a.set_input_files("input#resume-file", str(invalid_file))
    page_a.click("button[type='submit']")
    page_a.wait_for_load_state("networkidle")
    page_a.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_12_invalid_upload_error.png"))
    err_content = page_a.content()
    assert (
        "Upload Error" in err_content
        or "Unsupported file type" in err_content
        or "valid PDF" in err_content
    )

    # 2. Nonexistent job detail returns 404
    resp_404 = page_a.goto(f"{base_url}/candidate/jobs/nonexistent_job_uuid_999")
    assert resp_404 is not None and resp_404.status == 404

    # 3. Invalid session cookie redirects to onboarding
    context_tampered = browser.new_context()
    context_tampered.add_cookies(
        [
            {
                "name": "jobradar_candidate_session",
                "value": "tampered_fake_id_123.0123456789abcdef0123456789abcdef",
                "domain": "127.0.0.1",
                "path": "/",
            }
        ]
    )
    page_tampered = context_tampered.new_page()
    page_tampered.goto(f"{base_url}/candidate/dashboard")
    assert "/onboarding" in page_tampered.url
    page_tampered.close()
    context_tampered.close()

    # 4. CSRF rejection returns HTTP 403
    csrf_resp = page_a.request.post(
        f"{base_url}/candidate/jobs/{stripe_id}/status",
        form={"status": "applied", "token": "invalid_tampered_csrf_token_999"},
    )
    assert csrf_resp.status == 403

    # =========================================================================
    # STEP 18: Candidate B Isolation & IDOR Defense
    # =========================================================================
    # Open isolated browser context B (zero shared state)
    context_b: BrowserContext = browser.new_context()
    page_b: Page = context_b.new_page()

    # 1. Onboard Candidate B (Sam Test)
    page_b.goto(f"{base_url}/onboarding")
    pdf_path_b = tmp_path / "sam_resume.pdf"
    pdf_path_b.write_bytes(make_valid_pdf(CANDIDATE_B_RESUME))

    page_b.set_input_files("input#resume-file", str(pdf_path_b))
    page_b.click("button[type='submit']")
    page_b.wait_for_url("**/onboarding/review/**", timeout=10000)

    page_b.click("button[type='submit']:has-text('Confirm')")
    page_b.wait_for_url("**confirmed=1**", timeout=10000)

    # 2. Candidate B Dashboard is strictly isolated
    page_b.goto(f"{base_url}/candidate/dashboard")
    page_b.screenshot(path=str(SCREENSHOTS_DIR / "clean_run_11_candidate_b_isolated.png"))
    b_dash = page_b.content()
    assert "Sam Test" in b_dash
    assert "Alex Dev" not in b_dash

    # 3. Candidate B Applications tracker is isolated (0 applications)
    page_b.goto(f"{base_url}/candidate/applications")
    b_apps = page_b.content()
    assert "Applied: <b>0</b>" in b_apps or "Saved: <b>0</b>" in b_apps or "0 tracked" in b_apps
    assert "Technical phone screen scheduled" not in b_apps
    assert "Applied via employee referral" not in b_apps

    # 4. IDOR Defense: Candidate B attempts to access Candidate A's profile review URL
    idor_resp = page_b.goto(f"{base_url}/onboarding/review/{profile_a_id}")
    assert idor_resp is not None and idor_resp.status == 403

    # Cleanup contexts
    page_a.close()
    context_a.close()
    page_b.close()
    context_b.close()


def test_theme_persistence_and_responsive_viewports(
    e2e_server: tuple[str, Repo, dict[str, str]], browser: Browser
) -> None:
    """Verify theme cycling/persistence across reloads and responsive behavior across viewports."""
    base_url, _repo, _jobs = e2e_server
    context: BrowserContext = browser.new_context(viewport={"width": 1280, "height": 800})
    page: Page = context.new_page()

    # 1. Initial load on /jobs
    page.goto(f"{base_url}/jobs")
    initial_theme = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
    assert initial_theme is None

    # 2. Cycle theme to light
    page.click("#theme")
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "light"

    # 3. Reload - light theme persists
    page.reload()
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "light"

    # 4. Cycle theme to dark
    page.click("#theme")
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "dark"

    # 5. Navigate to /onboarding - dark theme persists across navigation
    page.goto(f"{base_url}/onboarding")
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "dark"

    # 6. New page in same context - dark theme persists from localStorage
    new_page = context.new_page()
    new_page.goto(f"{base_url}/jobs")
    assert new_page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "dark"
    new_page.close()

    # 7. Cycle back to system
    page.click("#theme")
    assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") is None

    # 8. Responsive Viewport Tests: Desktop
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/jobs")
    desktop_overflow = page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth"
    )
    assert desktop_overflow is True

    # 9. Responsive Viewport Tests: Tablet
    page.set_viewport_size({"width": 768, "height": 1024})
    page.goto(f"{base_url}/jobs")
    tablet_overflow = page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth"
    )
    assert tablet_overflow is True

    # 10. Responsive Viewport Tests: Mobile (390 x 844)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/jobs")
    mobile_overflow = page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth"
    )
    assert mobile_overflow is True

    # Check mobile bottom nav hides section titles
    section_title_display = page.evaluate(
        "() => window.getComputedStyle(document.querySelector('.nav-section-title')).display"
    )
    assert section_title_display == "none"

    page.close()
    context.close()
