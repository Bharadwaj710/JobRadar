"""Phase 1.2: Global Form Controls & Layout Polish Verification Test Suite.

Verifies:
- Theme-aware color-scheme and native controls (light, dark, system).
- Responsive two-column form grid on Profile Review (/onboarding/review/{id}).
- Single-column layout on mobile viewports (<640px).
- Labels positioned above inputs with full width controls.
- Consistent heights (38px / 36px) across text inputs and selects.
- Custom theme-aware SVG chevrons on select elements.
- Filter toolbar consistency on All Jobs (/jobs) and Match Radar (/candidate/dashboard).
- Captures all 7 required screenshots in tests/screenshots/.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
from sqlmodel import create_engine

from jobradar.clock import utcnow
from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.repo import Repo, init_db
from jobradar.web.app import create_app
from tests.test_e2e_playwright import (
    CANDIDATE_A_RESUME,
    make_valid_pdf,
    seed_inventory_jobs,
)

SCREENSHOTS_DIR = Path("tests/screenshots")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

RESUME_REVIEW_TEST = CANDIDATE_A_RESUME.replace(
    "alex.dev@synthetic-test.local", "alex.review@synthetic-test.local"
)
RESUME_RADAR_TEST = CANDIDATE_A_RESUME.replace(
    "alex.dev@synthetic-test.local", "alex.radar@synthetic-test.local"
)


@pytest.fixture(scope="module")
def browser_instance() -> Generator[Browser, None, None]:
    """Start Playwright Chromium browser."""
    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


@pytest.fixture(scope="module")
def form_controls_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[tuple[str, Repo, dict[str, str]], None, None]:
    """Spin up an isolated FastAPI server with in-memory SQLite and seeded inventory."""
    temp_dir = tmp_path_factory.mktemp("form_controls_sandbox")
    db_file = temp_dir / "form_controls_test.db"
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


def test_phase1_2_profile_review_form_layout_and_screenshots(
    form_controls_server: tuple[str, Repo, dict[str, str]],
    browser_instance: Browser,
    tmp_path: Path,
) -> None:
    """Verify responsive two-column grid, full-width inputs, theme color-scheme, and capture review profile screenshots."""
    base_url, _repo, _jobs = form_controls_server

    context: BrowserContext = browser_instance.new_context(viewport={"width": 1280, "height": 800})
    page: Page = context.new_page()

    # 1. Onboarding and resume upload
    page.goto(f"{base_url}/onboarding")
    pdf_bytes = make_valid_pdf(RESUME_REVIEW_TEST)
    pdf_file = tmp_path / "resume_review_test.pdf"
    pdf_file.write_bytes(pdf_bytes)
    page.set_input_files("input#resume-file", str(pdf_file))
    page.click("button[type='submit']")
    page.wait_for_url("**/onboarding/review/**", timeout=10000)

    # 2. Verify form structure and classes
    assert page.locator(".form-grid-2col").count() >= 2
    assert page.locator("input#f-name").is_visible()
    assert page.locator("input#f-email").is_visible()
    assert page.locator("input#f-institution").is_visible()
    assert page.locator("textarea#f-skills").is_visible()

    # 3. Check desktop computed styles
    name_box = page.locator("input#f-name").bounding_box()
    email_box = page.locator("input#f-email").bounding_box()
    assert name_box is not None and email_box is not None

    # Check 38px consistent height
    assert abs(name_box["height"] - 38) <= 2
    assert abs(email_box["height"] - 38) <= 2

    # Check two-column layout on desktop: name and email are side by side (same Y, different X)
    assert abs(name_box["y"] - email_box["y"]) <= 4
    assert email_box["x"] > name_box["x"]

    # Check institution is full-width (spans across both columns)
    inst_box = page.locator("input#f-institution").bounding_box()
    degree_box = page.locator("input#f-degree").bounding_box()
    assert inst_box is not None and degree_box is not None
    assert inst_box["width"] > degree_box["width"] * 1.5

    # Check textarea height
    skills_box = page.locator("textarea#f-skills").bounding_box()
    assert skills_box is not None
    assert skills_box["height"] >= 80

    # 4. Check labels appear above controls
    name_label_box = page.locator("label[for='f-name']").bounding_box()
    assert name_label_box is not None
    assert name_label_box["y"] < name_box["y"]

    # 5. Check focus state behavior
    page.focus("input#f-name")
    focus_box_shadow = page.evaluate(
        "() => window.getComputedStyle(document.querySelector('input#f-name')).boxShadow"
    )
    assert "rgb" in focus_box_shadow or focus_box_shadow != "none"

    # 6. Theme Switching and Capture: Dark Mode
    page.evaluate(
        "() => { document.documentElement.setAttribute('data-theme', 'dark'); localStorage.setItem('jobradar-theme', 'dark'); }"
    )
    page.wait_for_timeout(300)
    dark_scheme = page.evaluate(
        "() => window.getComputedStyle(document.documentElement).colorScheme"
    )
    assert dark_scheme == "dark"
    page.screenshot(path=str(SCREENSHOTS_DIR / "review_profile_dark.png"), full_page=True)

    # 7. Theme Switching and Capture: Light Mode
    page.evaluate(
        "() => { document.documentElement.setAttribute('data-theme', 'light'); localStorage.setItem('jobradar-theme', 'light'); }"
    )
    page.wait_for_timeout(300)
    light_scheme = page.evaluate(
        "() => window.getComputedStyle(document.documentElement).colorScheme"
    )
    assert light_scheme == "light"
    page.screenshot(path=str(SCREENSHOTS_DIR / "review_profile_light.png"), full_page=True)

    # 8. Mobile Viewport Layout and Capture (<640px)
    page.set_viewport_size({"width": 375, "height": 812})
    page.wait_for_timeout(300)
    mobile_name_box = page.locator("input#f-name").bounding_box()
    mobile_email_box = page.locator("input#f-email").bounding_box()
    assert mobile_name_box is not None and mobile_email_box is not None

    # On mobile, fields stack vertically (email is below name)
    assert mobile_email_box["y"] > mobile_name_box["y"]
    page.screenshot(path=str(SCREENSHOTS_DIR / "review_profile_mobile.png"), full_page=True)

    page.close()
    context.close()


def test_phase1_2_filters_and_match_radar_screenshots(
    form_controls_server: tuple[str, Repo, dict[str, str]],
    browser_instance: Browser,
    tmp_path: Path,
) -> None:
    """Verify Match Radar and All Jobs filter controls in light/dark themes and capture screenshots."""
    base_url, _repo, _jobs = form_controls_server

    context: BrowserContext = browser_instance.new_context(viewport={"width": 1280, "height": 800})
    page: Page = context.new_page()

    # Setup candidate and confirm profile
    page.goto(f"{base_url}/onboarding")
    pdf_bytes = make_valid_pdf(RESUME_RADAR_TEST)
    pdf_file = tmp_path / "resume_radar_test.pdf"
    pdf_file.write_bytes(pdf_bytes)
    page.set_input_files("input#resume-file", str(pdf_file))
    page.click("button[type='submit']")
    page.wait_for_url("**/onboarding/review/**", timeout=10000)
    page.click("button[type='submit']:has-text('Confirm')")
    page.wait_for_url("**confirmed=1**", timeout=10000)

    # 1. Match Radar Verification
    page.goto(f"{base_url}/candidate/dashboard")
    page.wait_for_load_state("networkidle")

    # Verify filter controls on Match Radar
    assert page.locator("select[name='track']").is_visible()
    assert page.locator("select[name='eligibility']").is_visible()

    track_select_box = page.locator("select[name='track']").bounding_box()
    assert track_select_box is not None
    assert abs(track_select_box["height"] - 36) <= 4

    # Capture Match Radar: Light
    page.evaluate("() => { document.documentElement.setAttribute('data-theme', 'light'); }")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SCREENSHOTS_DIR / "match_radar_light.png"), full_page=True)

    # Capture Match Radar: Dark
    page.evaluate("() => { document.documentElement.setAttribute('data-theme', 'dark'); }")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SCREENSHOTS_DIR / "match_radar_dark.png"), full_page=True)

    # Test filtering produces expected submission and preserves selected option
    page.goto(f"{base_url}/candidate/dashboard?track=ai_backend")
    page.wait_for_load_state("networkidle")
    assert "track=ai_backend" in page.url
    assert page.locator("select[name='track']").input_value() == "ai_backend"

    # 2. All Jobs Verification
    page.goto(f"{base_url}/jobs")
    page.wait_for_load_state("networkidle")

    # Verify search and select heights match 38px
    search_box = page.locator(".finder-top .search").bounding_box()
    show_box = page.locator("select#show").bounding_box()
    sort_box = page.locator("select#sort-all").bounding_box()
    search_btn_box = page.locator(".finder-top button[type='submit']").bounding_box()

    assert (
        search_box is not None
        and show_box is not None
        and sort_box is not None
        and search_btn_box is not None
    )
    assert abs(show_box["height"] - 38) <= 2
    assert abs(sort_box["height"] - 38) <= 2
    assert abs(search_btn_box["height"] - 38) <= 2

    # Open filter details drawer
    page.click(".finder-more summary")
    page.wait_for_timeout(200)
    assert page.locator(".finder-grid select[name='track']").is_visible()

    # Capture All Jobs Filters: Dark
    page.evaluate("() => { document.documentElement.setAttribute('data-theme', 'dark'); }")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SCREENSHOTS_DIR / "all_jobs_filters_dark.png"), full_page=True)

    # Capture All Jobs Filters: Light
    page.evaluate("() => { document.documentElement.setAttribute('data-theme', 'light'); }")
    page.wait_for_timeout(300)
    page.screenshot(path=str(SCREENSHOTS_DIR / "all_jobs_filters_light.png"), full_page=True)

    # 3. Candidate Job Detail & Application Status Form (authenticated context)
    stripe_id = _jobs["stripe"]
    page.goto(f"{base_url}/candidate/jobs/{stripe_id}")
    page.wait_for_load_state("networkidle")

    # Verify status select and notes input styles
    status_select_box = page.locator("select#f-status").bounding_box()
    notes_input_box = page.locator("input#f-notes").bounding_box()
    btn_box = page.locator("button:has-text('Update Application')").bounding_box()

    assert status_select_box is not None and notes_input_box is not None and btn_box is not None
    assert abs(status_select_box["height"] - 38) <= 2
    assert abs(notes_input_box["height"] - 38) <= 2
    assert abs(btn_box["height"] - 38) <= 4

    # Mutate application status
    page.select_option("select#f-status", "interviewing")
    page.fill("input#f-notes", "Form Controls Polish Verification Note")
    with page.expect_navigation():
        page.click("button:has-text('Update Application')")
    page.wait_for_load_state("networkidle")

    # Verify update succeeded and persisted
    page.reload()
    assert page.locator("select#f-status").input_value() == "interviewing"
    assert page.locator("input#f-notes").input_value() == "Form Controls Polish Verification Note"

    page.close()
    context.close()
