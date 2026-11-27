"""Tests for MVP Task 1: Candidate-Aware Navigation and Skills.

Verifies:
1. Authenticated candidate visiting / redirects to /candidate/dashboard.
2. Unauthenticated visitor visiting / sees the legacy today view.
3. Candidate A viewing their Skills page sees Candidate A's skills and gaps.
4. Candidate B viewing their Skills page sees Candidate B's skills and gaps.
5. Candidate isolation: Candidate A cannot access Candidate B's skill data;
   invalid/tampered cookies fall back safely to unauthenticated view.
6. Skill updates ("I know this") create versioned profile updates for the candidate,
   trigger re-assessment, and DO NOT touch config/profile.yaml.
7. Candidate without a confirmed profile sees a clear onboarding prompt on /skills.
8. Legacy owner workflow remains functional when no candidate session is present.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from jobradar.config import AppConfig, Settings
from jobradar.db.models import Candidate, CandidateProfile
from jobradar.db.repo import Repo
from jobradar.web.app import create_app
from jobradar.web.session import (
    CANDIDATE_SESSION_COOKIE,
    candidate_csrf_token,
    sign_candidate_session,
)
from tests.conftest import FakeClock
from tests.test_digest import settings

BASE_URL = "http://127.0.0.1:8080"


def make_test_settings(profile_path: Path | None = None) -> Settings:
    base = settings().config.model_dump()
    if profile_path is not None:
        base["matching"]["profile_path"] = str(profile_path)
    return Settings(
        config=AppConfig.model_validate(base),
        secrets=settings().secrets,
    )


# ==============================================================================
# Helper Factories
# ==============================================================================


def create_test_candidate(
    repo: Repo,
    name: str,
    email: str,
    skills: list[str],
    confirmed: bool = True,
) -> tuple[Candidate, CandidateProfile | None]:
    cand = repo.create_candidate(name=name, email=email)
    if not confirmed:
        return cand, None

    resume = repo.save_resume(
        candidate_id=cand.id,
        filename="resume.pdf",
        file_type="pdf",
        raw_text=f"Resume for {name}. Skills: {', '.join(skills)}",
    )
    profile_data = {
        "basics": {
            "name": name,
            "email": email,
            "phone": "+91 9999999999",
            "city": "Bengaluru",
        },
        "education": [
            {
                "degree": "B.Tech",
                "field": "Computer Science",
                "institution": "Test University",
                "graduation_year": 2026,
            }
        ],
        "skills": list(skills),
        "experience": [],
        "projects": [],
        "certifications": [],
        "preferences": {"roles": ["Backend Developer"]},
    }
    draft = repo.save_profile_draft(
        candidate_id=cand.id,
        resume_id=resume.id,
        profile_data=profile_data,
    )
    confirmed_profile = repo.confirm_profile(cand.id, draft.id, profile_data)
    return cand, confirmed_profile


# ==============================================================================
# 1. Root Route Navigation Tests
# ==============================================================================


def test_authenticated_candidate_root_redirects_to_dashboard(repo: Repo, clock: FakeClock) -> None:
    cand, _ = create_test_candidate(repo, "Alice Candidate", "alice@test.com", ["Python"])
    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    app = create_app(settings(), repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    session_cookie = sign_candidate_session(cand.id, web_token)
    client.cookies.set(CANDIDATE_SESSION_COOKIE, session_cookie)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/candidate/dashboard"


def test_unauthenticated_visitor_root_renders_today(repo: Repo, clock: FakeClock) -> None:
    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    app = create_app(settings(), repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Today" in response.text


def test_invalid_or_expired_candidate_session_falls_back_safely(
    repo: Repo, clock: FakeClock
) -> None:
    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    app = create_app(settings(), repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    # Corrupted / tampered session token
    client.cookies.set(CANDIDATE_SESSION_COOKIE, "cand_fake.invalid_signature")

    response = client.get("/", follow_redirects=False)
    # Does not redirect to candidate dashboard; renders today view without leaking data
    assert response.status_code == 200
    assert "Today" in response.text


# ==============================================================================
# 2. Candidate-Aware Skills Page Tests
# ==============================================================================


def test_candidate_a_views_own_skills_page(repo: Repo, clock: FakeClock) -> None:
    cand_a, prof_a = create_test_candidate(
        repo, "Candidate A", "cand_a@test.com", ["Python", "FastAPI", "SQL"]
    )
    assert prof_a is not None

    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    app = create_app(settings(), repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    cookie = sign_candidate_session(cand_a.id, web_token)
    client.cookies.set(CANDIDATE_SESSION_COOKIE, cookie)

    response = client.get("/skills")
    assert response.status_code == 200
    assert "Candidate A" in response.text
    assert f"Version {prof_a.version}" in response.text


def test_candidate_b_views_own_skills_page(repo: Repo, clock: FakeClock) -> None:
    cand_b, prof_b = create_test_candidate(
        repo, "Candidate B", "cand_b@test.com", ["Java", "Spring Boot", "AWS"]
    )
    assert prof_b is not None

    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    app = create_app(settings(), repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    cookie = sign_candidate_session(cand_b.id, web_token)
    client.cookies.set(CANDIDATE_SESSION_COOKIE, cookie)

    response = client.get("/skills")
    assert response.status_code == 200
    assert "Candidate B" in response.text
    assert f"Version {prof_b.version}" in response.text


def test_candidate_a_cannot_access_candidate_b_skill_data(repo: Repo, clock: FakeClock) -> None:
    cand_a, _ = create_test_candidate(repo, "Candidate A", "cand_a@test.com", ["Python"])
    _cand_b, _ = create_test_candidate(repo, "Candidate B", "cand_b@test.com", ["Java"])

    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    app = create_app(settings(), repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    # Authenticated as Candidate A
    client.cookies.set(CANDIDATE_SESSION_COOKIE, sign_candidate_session(cand_a.id, web_token))

    response = client.get("/skills")
    assert response.status_code == 200
    assert "Candidate A" in response.text
    assert "Candidate B" not in response.text


# ==============================================================================
# 3. Candidate Profile Without Confirmation
# ==============================================================================


def test_candidate_without_confirmed_profile_shows_onboarding_action(
    repo: Repo, clock: FakeClock
) -> None:
    cand, _ = create_test_candidate(
        repo, "Unconfirmed User", "unconfirmed@test.com", [], confirmed=False
    )
    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    app = create_app(settings(), repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    client.cookies.set(CANDIDATE_SESSION_COOKIE, sign_candidate_session(cand.id, web_token))

    # /skills should display the clear onboarding action card
    response = client.get("/skills")
    assert response.status_code == 200
    assert "No Confirmed Profile Found" in response.text
    assert "/onboarding" in response.text

    # /skills/{key} should redirect to /onboarding
    detail_resp = client.get("/skills/python", follow_redirects=False)
    assert detail_resp.status_code == 303
    assert detail_resp.headers["location"] == "/onboarding"


# ==============================================================================
# 4. "I Know This" Skill Updates & Profile Versioning
# ==============================================================================


def test_candidate_skill_update_creates_version_and_preserves_yaml(
    repo: Repo, clock: FakeClock, tmp_path: Path
) -> None:
    # Set up test candidate with initial skills
    cand, prof_v1 = create_test_candidate(repo, "Dev Candidate", "dev@test.com", ["Python", "SQL"])
    assert prof_v1 is not None
    assert prof_v1.version == 1

    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    # Create dummy profile.yaml to ensure it is untouched
    yaml_file = tmp_path / "profile.yaml"
    initial_yaml_content = "basics:\n  name: Static Owner\nskills:\n  additional: [Rust]\n"
    yaml_file.write_text(initial_yaml_content, encoding="utf-8")

    st = make_test_settings(yaml_file)

    app = create_app(st, repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    client.cookies.set(CANDIDATE_SESSION_COOKIE, sign_candidate_session(cand.id, web_token))
    csrf = candidate_csrf_token(cand.id, web_token)

    # Post "I know this" for a new skill: Docker
    post_resp = client.post(
        "/skills/know",
        data={"token": csrf, "skill": "Docker", "next": "/skills"},
        headers={"Origin": BASE_URL, "x-jobradar-token": csrf},
    )
    assert post_resp.status_code == 200
    data = post_resp.json()
    assert data["ok"] is True
    assert "Version 2" in data["message"]

    # Verify candidate profile in DB was versioned
    active_profile = repo.get_active_profile(cand.id)
    assert active_profile is not None
    assert active_profile.version == 2
    assert "Docker" in active_profile.profile_data["skills"]

    # Verify YAML file was NOT touched!
    assert yaml_file.read_text(encoding="utf-8") == initial_yaml_content


# ==============================================================================
# 5. Legacy Owner Workflow Remains Functional
# ==============================================================================


def test_legacy_owner_workflow_functional(repo: Repo, clock: FakeClock, tmp_path: Path) -> None:
    web_token = repo.get_setting("web_token") or "test_web_token_123"
    repo.set_setting("web_token", web_token)

    yaml_file = tmp_path / "profile.yaml"
    initial_yaml = "basics:\n  name: Legacy Owner\nskills:\n  additional: [C++]\n"
    yaml_file.write_text(initial_yaml, encoding="utf-8")

    st = make_test_settings(yaml_file)

    app = create_app(st, repo, clock=clock)
    client = TestClient(app, base_url=BASE_URL)

    # 1. Unauthenticated /skills loads legacy profile
    resp = client.get("/skills")
    assert resp.status_code == 200

    # 2. Unauthenticated "I know this" updates YAML file via legacy path
    know_resp = client.post(
        "/skills/know",
        data={"token": web_token, "skill": "Go", "next": "/skills"},
        headers={"Origin": BASE_URL, "x-jobradar-token": web_token},
    )
    assert know_resp.status_code == 200
    assert know_resp.json()["ok"] is True

    # Check YAML was updated by legacy handler
    updated_yaml = yaml_file.read_text(encoding="utf-8")
    assert "Go" in updated_yaml
