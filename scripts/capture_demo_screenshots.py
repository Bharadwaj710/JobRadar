"""Capture fresh screenshots and verify application behaviour in the demo environment."""

from __future__ import annotations

import secrets
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from tests.test_digest import settings  # noqa: E402

from jobradar.db.repo import Repo, make_engine  # noqa: E402
from jobradar.web.app import create_app  # noqa: E402
from jobradar.web.session import CANDIDATE_SESSION_COOKIE, sign_candidate_session  # noqa: E402

SCREENSHOTS_DIR = PROJECT_ROOT / "tests" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
DEMO_DB_PATH = PROJECT_ROOT / "data" / "demo" / "jobradar_demo.db"


def run_verification_and_capture() -> dict[str, str]:
    if not DEMO_DB_PATH.exists():
        raise RuntimeError(f"Demo database not found at {DEMO_DB_PATH}. Run seeder first.")

    engine = make_engine(DEMO_DB_PATH)
    repo = Repo(engine)

    candidates = repo.list_candidates()
    if not candidates:
        raise RuntimeError("No candidate found in demo database.")
    candidate = candidates[0]
    token = repo.get_setting("web_token")
    if not token:
        token = secrets.token_urlsafe(24)
        repo.set_setting("web_token", token)
    session_cookie = sign_candidate_session(candidate.id, token)

    st = settings()
    app = create_app(st, repo)

    port = 8599
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    time.sleep(1.5)

    captured = {}
    base_url = f"http://127.0.0.1:{port}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            # 1. Desktop Context (1280x800)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            context.add_cookies(
                [
                    {
                        "name": CANDIDATE_SESSION_COOKIE,
                        "value": session_cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            page = context.new_page()

            # Verify Candidate Dashboard (Light)
            page.goto(f"{base_url}/candidate/dashboard")
            page.wait_for_load_state("networkidle")
            if "Alex Dev" not in page.content():
                raise AssertionError("Candidate name missing from dashboard")
            if "AI Systems Engineer" not in page.content():
                raise AssertionError("Stripe job missing")
            if "Opportunities Pending Verification" not in page.content():
                raise AssertionError("Discovery banner missing")

            path_dash_light = SCREENSHOTS_DIR / "demo_01_dashboard_light.png"
            page.screenshot(path=str(path_dash_light), full_page=True)
            captured["dashboard_light"] = str(path_dash_light)
            print(f"[Captured] {path_dash_light.name}")

            # Verify Candidate Dashboard (Dark)
            page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
            time.sleep(0.3)
            path_dash_dark = SCREENSHOTS_DIR / "demo_02_dashboard_dark.png"
            page.screenshot(path=str(path_dash_dark), full_page=True)
            captured["dashboard_dark"] = str(path_dash_dark)
            print(f"[Captured] {path_dash_dark.name}")

            # Revert to system/light
            page.evaluate("document.documentElement.removeAttribute('data-theme')")

            # Verify Track Filter (SDET)
            page.goto(f"{base_url}/candidate/dashboard?track=sdet")
            page.wait_for_load_state("networkidle")
            if "SDET / Automation Engineer" not in page.content():
                raise AssertionError("SDET job missing in track filter")

            path_track_sdet = SCREENSHOTS_DIR / "demo_03_track_sdet_filter.png"
            page.screenshot(path=str(path_track_sdet), full_page=True)
            captured["track_sdet"] = str(path_track_sdet)
            print(f"[Captured] {path_track_sdet.name}")

            # Verify Ineligible Transparency Drawer / Log
            page.goto(f"{base_url}/candidate/dashboard?eligibility=not_eligible")
            page.wait_for_load_state("networkidle")

            if "Lead Backend Architect" not in page.content():
                raise AssertionError("Ineligible job missing from transparency view")
            path_ineligible = SCREENSHOTS_DIR / "demo_04_ineligible_drawer.png"
            page.screenshot(path=str(path_ineligible), full_page=True)
            captured["ineligible_drawer"] = str(path_ineligible)
            print(f"[Captured] {path_ineligible.name}")

            # Verify Job Detail Page (Stripe AI Systems Engineer)
            page.goto(f"{base_url}/candidate/jobs/demo_job_01_stripe_ai")
            page.wait_for_load_state("networkidle")
            if "AI Systems Engineer" not in page.content():
                raise AssertionError("Role title missing from job detail")
            if "[Demo] Stripe Technologies" not in page.content():
                raise AssertionError("Company missing from job detail")
            if "ELIGIBLE" not in page.content():
                raise AssertionError("Eligible badge missing from job detail")

            path_job_detail = SCREENSHOTS_DIR / "demo_05_job_detail_stripe.png"
            page.screenshot(path=str(path_job_detail), full_page=True)
            captured["job_detail"] = str(path_job_detail)
            print(f"[Captured] {path_job_detail.name}")

            # Verify Applications Board
            page.goto(f"{base_url}/candidate/applications")
            page.wait_for_load_state("networkidle")
            if "Applications" not in page.content():
                raise AssertionError("Applications heading missing")
            if "AI Systems Engineer" not in page.content():
                raise AssertionError("Tracked job missing from applications board")

            path_apps_board = SCREENSHOTS_DIR / "demo_06_applications_board.png"
            page.screenshot(path=str(path_apps_board), full_page=True)
            captured["applications_board"] = str(path_apps_board)
            print(f"[Captured] {path_apps_board.name}")

            context.close()

            # 2. Mobile Viewport (375x812)
            mob_context = browser.new_context(
                viewport={"width": 375, "height": 812},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            )
            mob_context.add_cookies(
                [
                    {
                        "name": CANDIDATE_SESSION_COOKIE,
                        "value": session_cookie,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                ]
            )
            mob_page = mob_context.new_page()
            mob_page.goto(f"{base_url}/candidate/dashboard")
            mob_page.wait_for_load_state("networkidle")

            path_mobile = SCREENSHOTS_DIR / "demo_07_mobile_dashboard.png"
            mob_page.screenshot(path=str(path_mobile), full_page=True)
            captured["mobile_dashboard"] = str(path_mobile)
            print(f"[Captured] {path_mobile.name}")

            mob_context.close()
            browser.close()

    finally:
        server.should_exit = True
        thread.join(timeout=2.0)

    print("\n[Browser Verification Succeeded] All demo screens verified and captured.")
    return captured


if __name__ == "__main__":
    run_verification_and_capture()
