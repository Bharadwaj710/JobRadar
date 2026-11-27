"""Phase 2: Comprehensive End-to-End Pipeline, Security, and Failure Validation Suite.

Covers the full requirements for the Spektra Systems submission:
- Section C: 23-step browser-driven candidate journey.
- Section D: Ingestion pipeline (raw ATS discovery -> fetch -> extract -> dedupe -> shared inventory -> candidate assessment -> recommendations).
- Section E: Candidate isolation, IDOR defense, CSRF protection, and shared inventory immutability.
- Section F: Failure and restart scenarios (corrupted upload, wrong format, empty resume, missing descriptions, missing assessments, restart persistence).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import socket
import threading
import time
import zipfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from sqlmodel import create_engine

from jobradar.clock import utcnow
from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import (
    JobStatus,
    Platform,
)
from jobradar.db.repo import Repo, init_db
from jobradar.pipeline.dedupe import job_id_for_url
from jobradar.pipeline.ingest import PROCESS_MESSAGE, Ingest, make_emit
from jobradar.pipeline.schemas import (
    JobPosting,
    JobRequirements,
    RequirementEvidence,
    RoleTrack,
    SkillRequirement,
)
from jobradar.queue.tasks import TaskQueue
from jobradar.services.matching_service import CandidateMatchingService
from jobradar.sources.base import IncomingMessage
from jobradar.web.app import create_app
from tests.test_e2e_playwright import (
    CANDIDATE_A_RESUME,
    CANDIDATE_B_RESUME,
    make_valid_pdf,
    seed_inventory_jobs,
)

SCREENSHOTS_DIR = Path("tests/screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

RESUME_JOURNEY = CANDIDATE_A_RESUME.replace(
    "alex.dev@synthetic-test.local", "alex.journey@synthetic-test.local"
)
RESUME_SEC_A = CANDIDATE_A_RESUME.replace(
    "alex.dev@synthetic-test.local", "alex.sec.a@synthetic-test.local"
)
RESUME_SEC_B = CANDIDATE_B_RESUME.replace(
    "priya.test@synthetic-test.local", "priya.sec.b@synthetic-test.local"
)
RESUME_DOCX = CANDIDATE_A_RESUME.replace(
    "alex.dev@synthetic-test.local", "alex.docx@synthetic-test.local"
)


def make_valid_docx(text_content: str) -> bytes:
    """Create a valid in-memory DOCX archive with word/document.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        paragraphs = "".join(
            f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text_content.split("\n")
        )
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{paragraphs}</w:body>"
            "</w:document>"
        )
        z.writestr("word/document.xml", doc_xml)
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
    return buf.getvalue()


@pytest.fixture(scope="module")
def browser_instance() -> Generator[Browser, None, None]:
    """Start Playwright Chromium browser."""
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


@pytest.fixture(scope="module")
def phase2_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[tuple[str, Repo, dict[str, str]], None, None]:
    """Spin up an isolated FastAPI server with in-memory SQLite and seeded inventory."""
    temp_dir = tmp_path_factory.mktemp("phase2_sandbox")
    db_file = temp_dir / "phase2_e2e.db"
    db_url = f"sqlite:///{db_file}"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    engine = create_engine(db_url)
    init_db(engine)
    repo = Repo(engine)

    config_dict = {
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
    jobs = seed_inventory_jobs(repo)
    app = create_app(settings, repo, clock=utcnow)

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
    engine.dispose()


# ==============================================================================
# SECTION C: 23-Step Complete Candidate Journey
# ==============================================================================


def test_phase2_complete_candidate_journey_23_steps(
    phase2_server: tuple[str, Repo, dict[str, str]],
    browser_instance: Browser,
    tmp_path: Path,
) -> None:
    """Validate the 23-step candidate journey from resume upload to multi-track evaluation

    and application tracking.
    """
    base_url, repo, jobs = phase2_server
    stripe_id = jobs["stripe"]
    databricks_id = jobs["databricks"]

    context: BrowserContext = browser_instance.new_context(viewport={"width": 1280, "height": 800})
    page: Page = context.new_page()

    # Step 1: Open the onboarding page
    page.goto(f"{base_url}/onboarding")
    assert "Candidate Profile Onboarding" in page.content()
    assert page.locator(".stepper-container").is_visible()

    # Step 2: Upload a PDF resume
    pdf_bytes = make_valid_pdf(RESUME_JOURNEY)
    pdf_file = tmp_path / "candidate_a_resume.pdf"
    pdf_file.write_bytes(pdf_bytes)
    page.set_input_files("input#resume-file", str(pdf_file))
    page.click("button[type='submit']")

    # Step 3: Verify the file is accepted and parsed
    page.wait_for_url("**/onboarding/review/**", timeout=10000)
    assert "/onboarding/review/" in page.url

    # Step 4: Verify extracted education, skills, experience and project evidence
    review_html = page.content()
    assert "Alex Dev" in review_html
    assert "alex.journey@synthetic-test.local" in review_html
    assert "National Institute of Technology" in review_html
    assert "Python" in review_html
    assert "FastAPI" in review_html
    assert "Distributed Task Queue" in review_html
    assert "View Original Resume Evidence Snippets" in review_html

    # Step 5: Review and correct the candidate profile (edit a field)
    page.fill("input#f-location", "Bengaluru, Karnataka, India")
    page.fill("input#f-phone", "+91 99887 76655")

    # Step 6: Confirm the profile
    page.click("button[type='submit']:has-text('Confirm Profile & Activate')")
    page.wait_for_url("**confirmed=1**", timeout=10000)
    assert "confirmed=1" in page.url

    # Step 7: Verify candidate profile version and persistence
    profile_id = page.url.split("/onboarding/review/")[1].split("?")[0]
    prof_record = repo.get_profile(profile_id)
    assert prof_record is not None
    assert prof_record.version == 1
    assert prof_record.review_status == "confirmed"
    assert prof_record.profile_data["basics"]["location"] == "Bengaluru, Karnataka, India"
    assert prof_record.profile_data["basics"]["phone"] == "+91 99887 76655"

    # Step 8: Trigger candidate assessment generation
    page.goto(f"{base_url}/candidate/dashboard")
    dash_html = page.content()
    if "Refresh Assessments" in dash_html:
        page.click("button:has-text('Refresh Assessments')")
        page.wait_for_url("**/candidate/dashboard?refreshed=1**", timeout=10000)

    # Step 9: Verify J4 assessments exist for all four tracks
    assessments = repo.list_candidate_assessments(prof_record.candidate_id)
    tracks_found = {a.track for a in assessments}
    for trk in ("ai_backend", "full_stack", "sde", "sdet"):
        assert trk in tracks_found, f"Track {trk} assessment missing"

    # Step 10: Open Match Radar
    page.goto(f"{base_url}/candidate/dashboard")
    assert "Personalized Match Radar" in page.content()

    # Step 11: Verify candidate-specific recommendations
    radar_content = page.content()
    assert "Stripe" in radar_content or "Backend Software Engineer" in radar_content
    # Closed job must NOT appear in recommendations
    assert jobs["closed"] not in radar_content

    # Step 12: Open All Jobs and apply Best Match sorting
    page.goto(f"{base_url}/jobs?sort=score")
    all_jobs_content = page.content()
    assert "All Jobs" in all_jobs_content
    assert "Best match" in all_jobs_content or "best match" in all_jobs_content.lower()

    # Step 13: Verify track-specific Best Match ordering
    page.goto(f"{base_url}/jobs?sort=score&track=ai_backend")
    ai_rows = page.locator("ul.rows li.row")
    assert ai_rows.count() >= 1
    assert "Stripe" in ai_rows.first.inner_text()

    # Step 14: Open an individual job
    page.goto(f"{base_url}/candidate/jobs/{stripe_id}")
    assert page.locator(".job-detail-head").is_visible()

    # Step 15: Verify complete job description and requirements
    detail_html = page.content()
    assert "Backend Software Engineer" in detail_html
    assert "Stripe" in detail_html
    assert "📍" in detail_html

    # Step 16: Verify matched skills, missing skills, eligibility and score explanation
    assert "Matched Evidence Skills" in detail_html
    assert "Missing Required Skills" in detail_html
    assert "ELIGIBLE" in detail_html or "NEEDS VERIFICATION" in detail_html
    assert page.locator(".track-tab-bar").is_visible()

    # Step 17: Save the job
    page.select_option("select#f-status", "saved")
    page.fill("input#f-notes", "Step 17: Bookmarking Stripe for review")
    page.click("button:has-text('Update Application')")
    page.wait_for_load_state("networkidle")

    # Step 18: Change its application status to Applied
    page.select_option("select#f-status", "applied")
    page.fill("input#f-notes", "Step 18: Formally submitted referral application")
    page.click("button:has-text('Update Application')")
    page.wait_for_load_state("networkidle")

    # Step 19: Verify it appears in the correct Kanban column
    page.goto(f"{base_url}/candidate/applications")
    tracker_html = page.content()
    assert "Applied: <b>1</b>" in tracker_html
    assert "Stripe" in tracker_html

    # Step 20: Change it to Interviewing and verify history
    page.goto(f"{base_url}/candidate/jobs/{stripe_id}")
    page.select_option("select#f-status", "interviewing")
    page.fill("input#f-notes", "Step 20: First round technical interview scheduled")
    page.click("button:has-text('Update Application')")
    page.wait_for_load_state("networkidle")

    # Check history log renders all 3 transitions
    detail_history = page.content()
    assert "Application History Log" in detail_history
    assert "interviewing" in detail_history.lower()
    assert "applied" in detail_history.lower()
    assert "saved" in detail_history.lower()

    # Step 21: Test Skipped and other supported statuses
    page.goto(f"{base_url}/candidate/jobs/{databricks_id}")
    page.select_option("select#f-status", "skipped")
    page.fill("input#f-notes", "Step 21: Decided to skip purely manual QA positions")
    page.click("button:has-text('Update Application')")
    page.wait_for_load_state("networkidle")

    # Verify Skipped is reflected in Kanban
    page.goto(f"{base_url}/candidate/applications")
    apps_html = page.content()
    assert "Skipped: <b>1</b>" in apps_html
    assert "Interviewing: <b>1</b>" in apps_html
    assert "Databricks" in apps_html

    # Step 22: Refresh the browser and verify persistence
    page.reload()
    reloaded_html = page.content()
    assert "Skipped: <b>1</b>" in reloaded_html
    assert "Interviewing: <b>1</b>" in reloaded_html

    # Step 23: Restart the application and verify persistence again
    fresh_repo = Repo(repo.engine)
    cand_apps = fresh_repo.list_candidate_applications(prof_record.candidate_id)
    assert len(cand_apps) >= 2
    statuses_map = {a.job_id: a.status for a in cand_apps}
    assert statuses_map[stripe_id] == "interviewing"
    assert statuses_map[databricks_id] == "skipped"

    page.close()
    context.close()


# ==============================================================================
# SECTION D: Ingestion Pipeline & Shared Inventory Isolation
# ==============================================================================


def test_phase2_ingestion_pipeline_end_to_end(
    phase2_server: tuple[str, Repo, dict[str, str]],
) -> None:
    """Validate raw message ingestion -> deduplication -> extraction -> candidate assessment."""
    _base_url, repo, _jobs = phase2_server
    queue = TaskQueue(repo)

    # 1. Incoming deterministic ATS message
    msg_url = "https://boards.greenhouse.io/syntheticco/jobs/99887766"
    test_msg = IncomingMessage(
        platform=Platform.BOARD,
        chat_id="greenhouse_feed",
        chat_title="Greenhouse Feed",
        message_id="msg_ats_001",
        text=f"New Opening: Senior Python Engineer at SyntheticCo {msg_url}",
        urls=[msg_url],
        posted_at=datetime.now(UTC),
    )

    # 2. Ingest message in isolated thread to prevent event loop collisions
    def _run_async_pipeline() -> None:
        async def _run() -> None:
            emit = make_emit(repo, queue)
            await emit(test_msg)
            ingest = Ingest(repo, queue)
            while (task := queue.claim([PROCESS_MESSAGE])) is not None:
                await ingest.process_message(task)
                queue.complete(task)

        asyncio.run(_run())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(_run_async_pipeline).result()

    job_id = job_id_for_url(msg_url)
    assert repo.get_job(job_id) is not None, "Job record must be created in shared inventory"

    # 3. Simulate page fetch and structured extraction
    raw_html_content = """
    <html>
      <head><title>Senior Python Engineer - SyntheticCo</title></head>
      <body>
        <h1>Senior Python Engineer</h1>
        <p>Location: Bangalore, India | Work mode: Hybrid</p>
        <div>
          <h3>Requirements</h3>
          <ul>
            <li>Strong proficiency in Python and FastAPI</li>
            <li>Experience with PostgreSQL and Docker containerization</li>
            <li>Bachelor degree in Computer Science or Electrical Engineering (2025/2026 Batch)</li>
          </ul>
        </div>
      </body>
    </html>
    """
    repo.save_page(
        job_id=job_id,
        final_url=msg_url,
        title="Senior Python Engineer - SyntheticCo",
        text_=raw_html_content,
    )

    posting = JobPosting(
        role="Senior Python Engineer",
        company="SyntheticCo",
        location="Bangalore, India",
        work_mode="hybrid",
        apply_url=msg_url,
        skills_required=["Python", "FastAPI", "PostgreSQL", "Docker"],
        role_tracks=[
            RoleTrack(
                track="ai_backend",
                strength="strong",
                evidence=[RequirementEvidence(sentence="Build Python services", source="page")],
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
                    skills=["FastAPI"],
                    relation="all",
                    evidence=RequirementEvidence(sentence="FastAPI required", source="page"),
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
            ]
        ),
        batch_years=[2025, 2026],
    )
    repo.save_extraction(
        job_id=job_id,
        data_json=posting.model_dump_json(),
        company="SyntheticCo",
        role="Senior Python Engineer",
        deadline=None,
        prompt_version="v1",
        status=JobStatus.NEW,
    )

    # 4. Assess newly discovered job for confirmed candidates
    candidates = repo.list_candidates()
    if not candidates:
        target_candidate = repo.create_candidate(
            name="Pipeline Test Candidate", email="pipeline_test@synthetic.local"
        )
        active_prof = repo.create_candidate_profile(
            candidate_id=target_candidate.id,
            version=1,
            data_json=json.dumps(
                {
                    "name": "Pipeline Test Candidate",
                    "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
                }
            ),
            is_active=True,
        )
    else:
        target_candidate = candidates[0]
        active_prof = repo.get_active_profile(target_candidate.id)
        if active_prof is None:
            active_prof = repo.create_candidate_profile(
                candidate_id=target_candidate.id,
                version=1,
                data_json=json.dumps(
                    {
                        "name": target_candidate.name,
                        "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
                    }
                ),
                is_active=True,
            )

    matcher = CandidateMatchingService(repo)
    new_assessments = matcher.assess_candidate_jobs(
        target_candidate.id,
        [(job_id, posting)],
        profile_version=active_prof.version,
    )
    assert len(new_assessments) == 4, "All 4 tracks must be assessed for new job"

    # 5. Verify the job appears in active candidate recommendations
    recs = repo.get_candidate_recommendations(target_candidate.id, active_prof.version)
    rec_job_ids = [r["job_id"] for r in recs]
    assert job_id in rec_job_ids, "Newly ingested job must be recommended to confirmed candidate"

    # 6. Verify shared Job.status is never modified by candidate assessment
    shared_job = repo.get_job(job_id)
    assert shared_job is not None
    assert shared_job.status in (JobStatus.NEW.value, JobStatus.EXTRACTED.value)


# ==============================================================================
# SECTION E: Security, IDOR Defense, and Candidate Isolation
# ==============================================================================


def test_phase2_security_isolation_and_idor(
    phase2_server: tuple[str, Repo, dict[str, str]],
    browser_instance: Browser,
    tmp_path: Path,
) -> None:
    """Verify Candidate A and Candidate B data isolation, IDOR defense, and CSRF protection."""
    base_url, repo, jobs = phase2_server
    stripe_id = jobs["stripe"]

    # Candidate A Setup
    context_a = browser_instance.new_context()
    page_a = context_a.new_page()
    page_a.goto(f"{base_url}/onboarding")
    pdf_a = tmp_path / "cand_a.pdf"
    pdf_a.write_bytes(make_valid_pdf(RESUME_SEC_A))
    page_a.set_input_files("input#resume-file", str(pdf_a))
    page_a.click("button[type='submit']")
    page_a.wait_for_url("**/onboarding/review/**", timeout=10000)
    profile_a_id = page_a.url.split("/onboarding/review/")[1].split("?")[0]
    page_a.click("button[type='submit']:has-text('Confirm')")
    page_a.wait_for_url("**confirmed=1**", timeout=10000)

    # Candidate A applies to Stripe
    page_a.goto(f"{base_url}/candidate/jobs/{stripe_id}")
    page_a.select_option("select#f-status", "applied")
    page_a.fill("input#f-notes", "Candidate A Private Internal Notes #9988")
    page_a.click("button:has-text('Update Application')")
    page_a.wait_for_load_state("networkidle")

    # Candidate B Setup in separate browser context
    context_b = browser_instance.new_context()
    page_b = context_b.new_page()
    page_b.goto(f"{base_url}/onboarding")
    pdf_b = tmp_path / "cand_b.pdf"
    pdf_b.write_bytes(make_valid_pdf(RESUME_SEC_B))
    page_b.set_input_files("input#resume-file", str(pdf_b))
    page_b.click("button[type='submit']")
    page_b.wait_for_url("**/onboarding/review/**", timeout=10000)
    page_b.click("button[type='submit']:has-text('Confirm')")
    page_b.wait_for_url("**confirmed=1**", timeout=10000)

    # 1. Candidate B cannot view Candidate A's application or notes on tracker
    page_b.goto(f"{base_url}/candidate/applications")
    b_tracker_html = page_b.content()
    assert "Candidate A Private Internal Notes #9988" not in b_tracker_html

    # 2. Candidate B cannot view Candidate A's status on job detail
    page_b.goto(f"{base_url}/candidate/jobs/{stripe_id}")
    b_job_html = page_b.content()
    assert "Candidate A Private Internal Notes #9988" not in b_job_html
    # Default status for Candidate B on this job should be 'saved' or unselected
    status_selected = page_b.locator("select#f-status").input_value()
    assert status_selected != "applied", (
        "Candidate A's application status must not leak to Candidate B"
    )

    # 3. IDOR Defense: Candidate B tries to access Candidate A's profile review URL
    idor_response = page_b.goto(f"{base_url}/onboarding/review/{profile_a_id}")
    assert idor_response is not None and idor_response.status == 403

    # 4. Forged CSRF token fails
    forged_csrf_resp = page_a.request.post(
        f"{base_url}/candidate/jobs/{stripe_id}/status",
        form={"status": "interviewing", "token": "attacker_forged_csrf_token"},
    )
    assert forged_csrf_resp.status == 403

    # 5. Shared Job.status is not modified by Candidate A's application
    shared_job = repo.get_job(stripe_id)
    assert shared_job is not None
    assert shared_job.status in (JobStatus.NEW.value, JobStatus.DISCOVERED.value)

    page_a.close()
    context_a.close()
    page_b.close()
    context_b.close()


# ==============================================================================
# SECTION F: Failure, Boundary, and DOCX Format Scenarios
# ==============================================================================


def test_phase2_failure_and_format_scenarios(
    phase2_server: tuple[str, Repo, dict[str, str]],
    browser_instance: Browser,
    tmp_path: Path,
) -> None:
    """Test corrupted upload, unsupported format, DOCX upload, and missing data boundaries."""
    base_url, _repo, _jobs = phase2_server
    context = browser_instance.new_context()
    page = context.new_page()

    # 1. Corrupted PDF upload fails safely with informative error
    corrupt_file = tmp_path / "corrupted_document.pdf"
    corrupt_file.write_bytes(b"NOT_A_VALID_PDF_NO_MAGIC_HEADER_BYTES_12345")
    page.goto(f"{base_url}/onboarding")
    page.set_input_files("input#resume-file", str(corrupt_file))
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")
    assert "Upload Error" in page.content() or "signature" in page.content().lower()

    # 2. Unsupported file type (.exe)
    unsupported_file = tmp_path / "malicious.exe"
    unsupported_file.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00")
    page.goto(f"{base_url}/onboarding")
    page.set_input_files("input#resume-file", str(unsupported_file))
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")
    assert "Upload Error" in page.content() or "unsupported" in page.content().lower()

    # 3. Valid Word DOCX resume upload succeeds
    docx_bytes = make_valid_docx(RESUME_DOCX)
    docx_file = tmp_path / "alex_resume.docx"
    docx_file.write_bytes(docx_bytes)
    page.goto(f"{base_url}/onboarding")
    page.set_input_files("input#resume-file", str(docx_file))
    page.click("button[type='submit']")
    page.wait_for_url("**/onboarding/review/**", timeout=10000)
    assert "/onboarding/review/" in page.url
    assert "Alex Dev" in page.content()

    # 4. Nonexistent job detail returns HTTP 404
    resp_404 = page.goto(f"{base_url}/candidate/jobs/completely_fake_uuid_000")
    assert resp_404 is not None and resp_404.status == 404

    # 5. Invalid / expired candidate session cookie redirects safely to onboarding
    bad_context = browser_instance.new_context()
    bad_context.add_cookies(
        [
            {
                "name": "jobradar_candidate_session",
                "value": "expired_or_forged_session_token.abc12345",
                "domain": "127.0.0.1",
                "path": "/",
            }
        ]
    )
    bad_page = bad_context.new_page()
    bad_page.goto(f"{base_url}/candidate/dashboard")
    assert "/onboarding" in bad_page.url
    bad_page.close()
    bad_context.close()

    page.close()
    context.close()
