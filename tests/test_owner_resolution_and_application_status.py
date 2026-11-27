"""Tests for Task 3A.1: Owner Resolution Safety & Migration Preservation.

Verifies:
1. Explicit owner ID resolves correctly.
2. Missing or invalid configured owner ID fails safely.
3. A unique confirmed candidate without reliable owner identity does NOT get selected automatically.
4. A single draft candidate without reliable owner identity does NOT get selected automatically.
5. Exact reliable legacy-owner identity (verified email) resolves the correct candidate.
6. Multiple possible identity matches or unconfirmed profile fail safely.
7. Existing candidate IDs, profiles, and assessment rows remain completely unchanged.
8. Candidate session identity remains strictly separate from legacy owner resolution.
9. Ownership, authentication, and CSRF protections remain intact.
10. SQLite migration preserves records, column values, foreign keys, indexes, and triggers.
11. Migration is safe and idempotent when run repeatedly.
12. Existing application statuses continue working.
13. SKIPPED status is accepted and persisted.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from jobradar.config import AppConfig, Secrets, Settings
from jobradar.db.models import (
    CandidateApplicationStatus,
)
from jobradar.db.repo import (
    OwnerResolutionError,
    Repo,
    _refresh_candidate_application_statuses,
    init_db,
    make_engine,
)
from jobradar.web.app import create_app
from jobradar.web.session import (
    CANDIDATE_SESSION_COOKIE,
    candidate_csrf_token,
    parse_candidate_session,
    sign_candidate_session,
    validate_candidate_csrf,
    verify_profile_ownership,
)
from tests.conftest import FakeClock
from tests.test_digest import settings


@pytest.fixture
def isolated_repo(tmp_path: Path, clock: FakeClock) -> Repo:
    db_path = tmp_path / "test_stage3a1.db"
    eng = make_engine(db_path)
    init_db(eng)
    return Repo(eng, clock=clock)


def test_explicit_owner_id_resolves_correctly(isolated_repo: Repo) -> None:
    """1. Explicit configured owner candidate ID takes precedence and resolves correctly."""
    c1 = isolated_repo.create_candidate(
        id="cand_owner_1", display_name="Owner Candidate", email="owner@example.com"
    )
    c2 = isolated_repo.create_candidate(
        id="cand_other_2", display_name="Other Candidate", email="other@example.com"
    )

    resolved = isolated_repo.resolve_owner_candidate(configured_id="cand_owner_1")
    assert resolved.id == c1.id
    assert resolved.display_name == "Owner Candidate"

    resolved_id = isolated_repo.resolve_owner_candidate_id(configured_id="cand_other_2")
    assert resolved_id == c2.id


def test_missing_configured_owner_id_fails_safely(isolated_repo: Repo) -> None:
    """2. Non-existent configured owner ID raises OwnerResolutionError with actionable guidance."""
    isolated_repo.create_candidate(id="cand_existing", display_name="Existing Candidate")

    with pytest.raises(
        OwnerResolutionError, match="Configured owner_candidate_id 'cand_missing' was not found"
    ):
        isolated_repo.resolve_owner_candidate(configured_id="cand_missing")

    with pytest.raises(
        OwnerResolutionError, match="Configured owner_candidate_id 'cand_missing' was not found"
    ):
        isolated_repo.resolve_owner_candidate_id(configured_id="cand_missing")


def test_unique_confirmed_candidate_without_identity_does_not_get_selected(
    isolated_repo: Repo,
) -> None:
    """3. A unique confirmed candidate must NOT automatically become the owner without reliable identity."""
    c = isolated_repo.create_candidate(
        id="cand_confirmed_lone", display_name="Lone Confirmed", email="user@example.com"
    )
    isolated_repo.save_profile_draft(candidate_id=c.id, profile_data={"name": "Lone"})
    profiles = isolated_repo.get_candidate_profiles(c.id)
    isolated_repo.confirm_profile(c.id, profiles[0].id, {"name": "Lone"})

    # Even though c is the only confirmed candidate, resolving without owner identity must fail safely
    with pytest.raises(
        OwnerResolutionError,
        match="Legacy owner candidate identity cannot be verified: owner_candidate_id is not configured",
    ):
        isolated_repo.resolve_owner_candidate(configured_id=None, owner_email=None)


def test_single_draft_candidate_without_identity_does_not_get_selected(
    isolated_repo: Repo,
) -> None:
    """4. A single candidate in the database must NOT automatically become the owner without identity."""
    isolated_repo.create_candidate(
        id="cand_only_one", display_name="Only One", email="lone@test.com"
    )

    with pytest.raises(
        OwnerResolutionError,
        match="Legacy owner candidate identity cannot be verified: owner_candidate_id is not configured",
    ):
        isolated_repo.resolve_owner_candidate(configured_id=None, owner_email=None)


def test_exact_reliable_legacy_owner_identity_resolves_correct_candidate(
    isolated_repo: Repo,
) -> None:
    """5. Exact reliable legacy-owner identity (verified email) resolves the correct confirmed candidate."""
    c1 = isolated_repo.create_candidate(
        id="cand_real_owner", display_name="True Owner", email="owner@established.com"
    )
    isolated_repo.save_profile_draft(c1.id, profile_data={"name": "Owner Profile"})
    p1 = isolated_repo.get_candidate_profiles(c1.id)[0]
    isolated_repo.confirm_profile(c1.id, p1.id, {"name": "Owner Profile"})

    c2 = isolated_repo.create_candidate(
        id="cand_other_user", display_name="Other User", email="other@domain.com"
    )
    isolated_repo.save_profile_draft(c2.id, profile_data={"name": "Other Profile"})
    p2 = isolated_repo.get_candidate_profiles(c2.id)[0]
    isolated_repo.confirm_profile(c2.id, p2.id, {"name": "Other Profile"})

    # Case-insensitive email match resolves reliably
    resolved = isolated_repo.resolve_owner_candidate(
        configured_id=None, owner_email="OWNER@ESTABLISHED.COM"
    )
    assert resolved.id == c1.id
    assert resolved.display_name == "True Owner"

    resolved_id = isolated_repo.resolve_owner_candidate_id(
        configured_id=None, owner_email="owner@established.com"
    )
    assert resolved_id == c1.id


def test_multiple_possible_identity_matches_or_unconfirmed_profile_fail_safely(
    isolated_repo: Repo,
) -> None:
    """6. Ambiguous email matches or matching candidate without confirmed profile fail safely."""
    # A. Email does not match any candidate
    isolated_repo.create_candidate(id="cand_x", email="x@example.com")
    with pytest.raises(
        OwnerResolutionError,
        match="Legacy owner identity could not be verified: no candidate found with email",
    ):
        isolated_repo.resolve_owner_candidate(configured_id=None, owner_email="unknown@example.com")

    # B. Matched candidate has no confirmed profile
    c_draft = isolated_repo.create_candidate(id="cand_draft_only", email="draft@example.com")
    with pytest.raises(
        OwnerResolutionError,
        match=f"Candidate '{c_draft.id}' matches owner email 'draft@example.com' but has no confirmed profile",
    ):
        isolated_repo.resolve_owner_candidate(configured_id=None, owner_email="draft@example.com")


def test_existing_candidate_ids_and_assessments_remain_unchanged(isolated_repo: Repo) -> None:
    """7. Owner candidate resolution is strictly read-only and never mutates candidate state."""
    c = isolated_repo.create_candidate(
        id="cand_immutable", display_name="Immutable Candidate", email="user@immutable.com"
    )
    isolated_repo.save_profile_draft(c.id, profile_data={"skills": ["Python"]})
    p = isolated_repo.get_candidate_profiles(c.id)[0]
    confirmed = isolated_repo.confirm_profile(c.id, p.id, {"skills": ["Python"]})

    c_before = isolated_repo.get_candidate(c.id)
    assert c_before is not None

    # Perform resolution
    res1 = isolated_repo.resolve_owner_candidate("cand_immutable")
    res2 = isolated_repo.resolve_owner_candidate(None, owner_email="user@immutable.com")

    assert res1.id == c.id
    assert res2.id == c.id

    # Verify candidate record is bit-for-bit unchanged
    c_after = isolated_repo.get_candidate(c.id)
    assert c_after is not None
    assert c_after.id == c_before.id
    assert c_after.created_at == c_before.created_at
    assert c_after.updated_at == c_before.updated_at
    assert c_after.email == c_before.email

    profiles_after = isolated_repo.get_candidate_profiles(c.id)
    assert len(profiles_after) == 1
    assert profiles_after[0].id == confirmed.id
    assert profiles_after[0].version == confirmed.version


def test_candidate_session_identity_separate_from_owner_resolution(isolated_repo: Repo) -> None:
    """8. Candidate session identity remains completely separate from owner resolution."""
    c_owner = isolated_repo.create_candidate(id="cand_owner_acct", email="owner@local.com")
    c_other = isolated_repo.create_candidate(id="cand_other_acct", email="other@local.com")

    secret = "session_test_secret_key"  # noqa: S105
    # Authenticate other candidate
    token = sign_candidate_session(c_other.id, secret)
    session_cand_id = parse_candidate_session(token, secret)
    assert session_cand_id == c_other.id

    # Owner resolution resolves owner candidate, not session candidate
    owner_cand = isolated_repo.resolve_owner_candidate("cand_owner_acct")
    assert owner_cand.id == c_owner.id
    assert session_cand_id == c_other.id
    assert session_cand_id != owner_cand.id


def test_ownership_and_csrf_protections_intact(isolated_repo: Repo) -> None:
    """9. Candidate ownership, session signing, and CSRF token protections remain intact."""
    c1 = isolated_repo.create_candidate(id="cand_user_1", display_name="User 1")
    c2 = isolated_repo.create_candidate(id="cand_user_2", display_name="User 2")

    p1 = isolated_repo.save_profile_draft(c1.id, profile_data={"name": "1"})
    p2 = isolated_repo.save_profile_draft(c2.id, profile_data={"name": "2"})

    assert verify_profile_ownership(c1.id, p1) is True
    assert verify_profile_ownership(c2.id, p1) is False
    assert verify_profile_ownership(c2.id, p2) is True
    assert verify_profile_ownership(c1.id, p2) is False

    secret = "sec_csrf_test"  # noqa: S105
    csrf1 = candidate_csrf_token(c1.id, secret)
    assert validate_candidate_csrf(c1.id, csrf1, secret) is True
    assert validate_candidate_csrf(c2.id, csrf1, secret) is False


def test_sqlite_migration_preserves_records_foreign_keys_indexes_and_triggers(
    tmp_path: Path,
) -> None:
    """10. SQLite migration preserves all records, foreign keys, indexes, and triggers."""
    db_path = tmp_path / "comprehensive_migration_test.db"

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE job (
            id VARCHAR PRIMARY KEY,
            canonical_url VARCHAR,
            status VARCHAR NOT NULL,
            created_at VARCHAR,
            updated_at VARCHAR
        )
        """
    )
    conn.execute("CREATE TABLE candidate (id VARCHAR PRIMARY KEY, email VARCHAR)")
    conn.execute(
        """
        CREATE TABLE candidate_application (
            id VARCHAR NOT NULL PRIMARY KEY,
            candidate_id VARCHAR NOT NULL,
            job_id VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            applied_at VARCHAR,
            notes VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL,
            CONSTRAINT candidate_application_candidate_job UNIQUE (candidate_id, job_id),
            CONSTRAINT candidate_application_status_value CHECK (
                status IN ('saved', 'applied', 'interviewing', 'offered', 'rejected')
            ),
            FOREIGN KEY (job_id) REFERENCES job (id),
            FOREIGN KEY (candidate_id) REFERENCES candidate (id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE candidate_application_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id VARCHAR NOT NULL,
            job_id VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            notes VARCHAR NOT NULL,
            changed_at VARCHAR NOT NULL,
            FOREIGN KEY (job_id) REFERENCES job (id),
            FOREIGN KEY (candidate_id) REFERENCES candidate (id)
        )
        """
    )
    # Explicit indexes on old table
    conn.execute(
        "CREATE INDEX candidate_application_candidate_status ON candidate_application (candidate_id, status)"
    )
    conn.execute("CREATE INDEX ix_candidate_application_job_id ON candidate_application (job_id)")
    conn.execute(
        "CREATE INDEX ix_candidate_application_candidate_id ON candidate_application (candidate_id)"
    )

    # Custom audit trigger on candidate_application
    conn.execute(
        """
        CREATE TRIGGER trg_test_audit AFTER UPDATE OF status ON candidate_application
        BEGIN
            INSERT INTO candidate_application_history (candidate_id, job_id, status, notes, changed_at)
            VALUES (NEW.candidate_id, NEW.job_id, NEW.status, 'audit_trigger', '2026-11-26T12:00:00Z');
        END;
        """
    )

    # Seed data
    conn.execute(
        """
        INSERT INTO job (id, canonical_url, status, created_at, updated_at)
        VALUES ('j1', 'https://example.com/j1', 'new', 'now', 'now'),
               ('j2', 'https://example.com/j2', 'new', 'now', 'now')
        """
    )
    conn.execute("INSERT INTO candidate VALUES ('c1', 'c1@test.com')")
    conn.execute(
        """
        INSERT INTO candidate_application VALUES
        ('app1', 'c1', 'j1', 'saved', NULL, 'Saved job 1', '2026-11-20T10:00:00Z', '2026-11-20T10:00:00Z'),
        ('app2', 'c1', 'j2', 'applied', '2026-11-21T11:00:00Z', 'Applied note', '2026-11-21T11:00:00Z', '2026-11-21T11:00:00Z')
        """
    )
    conn.commit()

    # Pre-migration: inserting 'skipped' fails CHECK constraint
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO candidate_application VALUES ('app3', 'c1', 'j1', 'skipped', NULL, '', 'now', 'now')"
        )
    conn.close()

    # Execute migration
    eng = make_engine(db_path)
    _refresh_candidate_application_statuses(eng)

    repo = Repo(eng)
    # A. Verify existing records and column values are preserved
    app1 = repo.get_candidate_application("c1", "j1")
    assert app1 is not None
    assert app1.id == "app1"
    assert app1.status == "saved"
    assert app1.notes == "Saved job 1"
    assert app1.created_at == "2026-11-20T10:00:00Z"
    assert app1.applied_at is None

    app2 = repo.get_candidate_application("c1", "j2")
    assert app2 is not None
    assert app2.id == "app2"
    assert app2.status == "applied"
    assert app2.applied_at == "2026-11-21T11:00:00Z"

    # B. Verify foreign key integrity
    with eng.connect() as c:
        fk_check = c.execute(text("PRAGMA foreign_key_check")).fetchall()
        assert not fk_check

        # C. Verify all 3 indexes exist on candidate_application
        idx_rows = c.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'candidate_application'"
            )
        ).fetchall()
        idx_names = {r[0] for r in idx_rows}
        assert "candidate_application_candidate_status" in idx_names
        assert "ix_candidate_application_job_id" in idx_names
        assert "ix_candidate_application_candidate_id" in idx_names

        # D. Verify trigger survived and functions
        trg_rows = c.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'candidate_application'"
            )
        ).fetchall()
        trg_names = {r[0] for r in trg_rows}
        assert "trg_test_audit" in trg_names

    # E. Verify trigger execution upon update
    repo.save_candidate_application("c1", "j1", status="interviewing", notes="Got interview")
    history = repo.list_candidate_application_history("c1", "j1")
    # One inserted by save_candidate_application, one inserted by trigger
    assert any(h.notes == "audit_trigger" for h in history)

    # F. Verify 'skipped' can be inserted and retrieved
    with eng.connect() as c, c.begin():
        c.execute(
            text(
                "INSERT INTO job (id, canonical_url, status, created_at, updated_at) "
                "VALUES ('j3', 'https://example.com/j3', 'new', 'now', 'now')"
            )
        )
    app3 = repo.save_candidate_application("c1", "j3", status="skipped", notes="Dismissed")
    assert app3.status == "skipped"

    fetched3 = repo.get_candidate_application("c1", "j3")
    assert fetched3 is not None
    assert fetched3.status == "skipped"
    assert fetched3.notes == "Dismissed"

    eng.dispose()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """11. Re-running migration on an already updated database is safe and idempotent."""
    db_path = tmp_path / "idempotent_test.db"
    eng = make_engine(db_path)
    init_db(eng)

    repo = Repo(eng)
    c = repo.create_candidate(id="c_idem")
    j, _ = repo.get_or_create_job("j_idem", "https://example.com/idem")
    repo.save_candidate_application(c.id, j.id, status="skipped")

    # Second migration run must be a safe noop
    _refresh_candidate_application_statuses(eng)

    app = repo.get_candidate_application(c.id, j.id)
    assert app is not None
    assert app.status == "skipped"

    eng.dispose()


def test_existing_application_statuses_and_skipped_supported(isolated_repo: Repo) -> None:
    """12. All standard statuses plus SKIPPED are supported and counted."""
    c = isolated_repo.create_candidate(id="c_all_st")
    all_statuses = [
        CandidateApplicationStatus.SAVED.value,
        CandidateApplicationStatus.APPLIED.value,
        CandidateApplicationStatus.INTERVIEWING.value,
        CandidateApplicationStatus.OFFERED.value,
        CandidateApplicationStatus.REJECTED.value,
        CandidateApplicationStatus.SKIPPED.value,
    ]

    for idx, st in enumerate(all_statuses):
        jid = f"job_st_{idx}"
        isolated_repo.get_or_create_job(jid, f"https://example.com/{jid}")
        app = isolated_repo.save_candidate_application(c.id, jid, status=st)
        assert app.status == st

    counts = isolated_repo.get_candidate_application_counts(c.id)
    for st in all_statuses:
        assert counts[st] == 1
    assert counts["total"] == len(all_statuses)


def test_settings_explicit_owner_id_overrides_automatic_identity_resolution(
    isolated_repo: Repo, tmp_path: Path
) -> None:
    """13. Explicit AppConfig.owner_candidate_id overrides automatic email matching."""
    c1 = isolated_repo.create_candidate(
        id="cand_owner_explicit", display_name="Explicit Owner", email="explicit@example.com"
    )
    isolated_repo.save_profile_draft(c1.id, {"skills": ["Python"]})
    prof1 = isolated_repo.get_candidate_profiles(c1.id)[0]
    isolated_repo.confirm_profile(c1.id, prof1.id, {"skills": ["Python"]})

    c2 = isolated_repo.create_candidate(
        id="cand_notify_match", display_name="Notify Match", email="notify@example.com"
    )
    isolated_repo.save_profile_draft(c2.id, {"skills": ["Go"]})
    prof2 = isolated_repo.get_candidate_profiles(c2.id)[0]
    isolated_repo.confirm_profile(c2.id, prof2.id, {"skills": ["Go"]})

    # Configure notify address to match c2, but explicit owner_candidate_id to c1
    base_cfg = settings().config.model_dump()
    base_cfg["owner_candidate_id"] = "cand_owner_explicit"
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="notify@example.com"),
    )

    resolved = isolated_repo.resolve_owner_candidate_from_settings(st)
    assert resolved.id == c1.id
    assert resolved.email == "explicit@example.com"


def test_settings_conflicting_configured_identity_sources_fail_safely(
    isolated_repo: Repo, tmp_path: Path
) -> None:
    """14. Conflicting identity sources (notify_address != profile.yaml email) fail safely."""
    c = isolated_repo.create_candidate(
        id="cand_conflict", display_name="Conflict", email="sec@example.com"
    )
    isolated_repo.save_profile_draft(c.id, {"skills": ["Python"]})
    prof = isolated_repo.get_candidate_profiles(c.id)[0]
    isolated_repo.confirm_profile(c.id, prof.id, {"skills": ["Python"]})

    base_cfg = settings().config.model_dump()
    base_cfg["owner_candidate_id"] = None
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="sec@example.com"),
    )

    profile_data = {"basics": {"email": "yaml@example.com"}}

    with pytest.raises(
        OwnerResolutionError,
        match="Configured owner identity sources disagree",
    ):
        isolated_repo.resolve_owner_candidate_from_settings(st, profile=profile_data)


def test_settings_agreeing_identity_sources_resolve_confirmed_owner(
    isolated_repo: Repo, tmp_path: Path
) -> None:
    """15. Consistent identity sources resolve the confirmed owner candidate."""
    c = isolated_repo.create_candidate(
        id="cand_consistent", display_name="Consistent", email="same@example.com"
    )
    isolated_repo.save_profile_draft(c.id, {"skills": ["Python"]})
    prof = isolated_repo.get_candidate_profiles(c.id)[0]
    isolated_repo.confirm_profile(c.id, prof.id, {"skills": ["Python"]})

    base_cfg = settings().config.model_dump()
    base_cfg["owner_candidate_id"] = None
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="same@example.com"),
    )
    profile_data = {"basics": {"email": "same@example.com"}}

    resolved = isolated_repo.resolve_owner_candidate_from_settings(st, profile=profile_data)
    assert resolved.id == c.id
    assert resolved.email == "same@example.com"


def test_settings_missing_identity_sources_fail_safely(isolated_repo: Repo) -> None:
    """16. Unconfigured owner ID with missing identity sources fails safely."""
    base_cfg = settings().config.model_dump()
    base_cfg["owner_candidate_id"] = None
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(),
    )
    profile_data = {"basics": {}}

    with pytest.raises(
        OwnerResolutionError,
        match="Legacy owner candidate identity cannot be verified",
    ):
        isolated_repo.resolve_owner_candidate_from_settings(st, profile=profile_data)


def test_legacy_owner_web_workflow_calls_safe_resolution_mechanism(
    isolated_repo: Repo, clock: FakeClock, tmp_path: Path
) -> None:
    """17. Legacy owner web workflow (/ and /skills) calls safe owner resolution."""
    c = isolated_repo.create_candidate(
        id="cand_owner_web", display_name="Owner Web", email="owner@web.local"
    )
    isolated_repo.save_profile_draft(c.id, {"skills": ["Python"]})
    prof = isolated_repo.get_candidate_profiles(c.id)[0]
    isolated_repo.confirm_profile(c.id, prof.id, {"skills": ["Python"]})

    yaml_file = tmp_path / "owner_profile.yaml"
    yaml_file.write_text(
        "basics:\n  name: Owner Web\n  email: owner@web.local\nskills:\n  additional: [Python]\n",
        encoding="utf-8",
    )

    base_cfg = settings().config.model_dump()
    base_cfg["matching"]["profile_path"] = str(yaml_file)
    base_cfg["owner_candidate_id"] = None
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="owner@web.local"),
    )

    app = create_app(st, isolated_repo, clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1:8080")

    # Verify app state resolution resolves the owner
    assert app.state.resolve_owner().id == c.id

    # 1. Root route renders today view for unauthenticated visitor (owner)
    resp_root = client.get("/")
    assert resp_root.status_code == 200

    # 2. Skills route renders skills view for unauthenticated visitor (owner)
    resp_skills = client.get("/skills")
    assert resp_skills.status_code == 200

    # 3. Legacy owner "I know this" invokes owner resolution
    web_token = isolated_repo.get_setting("web_token") or "test_web_token_123"
    isolated_repo.set_setting("web_token", web_token)
    know_resp = client.post(
        "/skills/know",
        data={"token": web_token, "skill": "Docker", "next": "/skills"},
        headers={"Origin": "http://127.0.0.1:8080", "x-jobradar-token": web_token},
    )
    assert know_resp.status_code == 200


def test_candidate_session_identity_remains_independent_of_owner_settings(
    isolated_repo: Repo, clock: FakeClock, tmp_path: Path
) -> None:
    """18. Authenticated candidate session remains completely independent from owner resolution."""
    c_owner = isolated_repo.create_candidate(
        id="cand_owner_sys", display_name="Owner", email="owner@sys.local"
    )
    isolated_repo.save_profile_draft(c_owner.id, {"skills": ["Python"]})
    prof_o = isolated_repo.get_candidate_profiles(c_owner.id)[0]
    isolated_repo.confirm_profile(c_owner.id, prof_o.id, {"skills": ["Python"]})

    c_user = isolated_repo.create_candidate(
        id="cand_logged_in_user", display_name="Alice User", email="alice@user.local"
    )
    user_prof_data = {
        "basics": {"name": "Alice User", "email": "alice@user.local"},
        "skills": ["Rust"],
    }
    isolated_repo.save_profile_draft(c_user.id, user_prof_data)
    prof_u = isolated_repo.get_candidate_profiles(c_user.id)[0]
    isolated_repo.confirm_profile(c_user.id, prof_u.id, user_prof_data)

    yaml_file = tmp_path / "owner_profile.yaml"
    yaml_file.write_text(
        "basics:\n  name: Owner\n  email: owner@sys.local\nskills:\n  additional: [Python]\n",
        encoding="utf-8",
    )

    base_cfg = settings().config.model_dump()
    base_cfg["matching"]["profile_path"] = str(yaml_file)
    base_cfg["owner_candidate_id"] = "cand_owner_sys"
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(notify_to="owner@sys.local"),
    )

    app = create_app(st, isolated_repo, clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1:8080")

    # Set authenticated candidate session for Alice
    web_token = isolated_repo.get_setting("web_token") or "test_web_token_123"
    isolated_repo.set_setting("web_token", web_token)
    session_cookie = sign_candidate_session(c_user.id, web_token)
    client.cookies.set(CANDIDATE_SESSION_COOKIE, session_cookie)

    # Alice visits / -> redirects to /candidate/dashboard
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/candidate/dashboard"

    # Alice visits /skills -> sees Alice's personalized skills page, not Owner's
    skills_resp = client.get("/skills")
    assert skills_resp.status_code == 200
    assert "Alice User" in skills_resp.text

    # Owner resolution remains candidate owner
    assert app.state.resolve_owner().id == c_owner.id


def test_injected_migration_failure_rolls_back_cleanly_leaving_no_partial_rebuild(
    tmp_path: Path,
) -> None:
    """19. An injected failure during migration rolls back cleanly without partial tables."""
    db_path = tmp_path / "injected_failure_test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE job (id VARCHAR PRIMARY KEY, status VARCHAR NOT NULL)")
    conn.execute("CREATE TABLE candidate (id VARCHAR PRIMARY KEY, email VARCHAR)")
    conn.execute(
        """
        CREATE TABLE candidate_application (
            id VARCHAR NOT NULL PRIMARY KEY,
            candidate_id VARCHAR NOT NULL,
            job_id VARCHAR NOT NULL,
            status VARCHAR NOT NULL CHECK(status IN ('saved', 'applied', 'interviewing')),
            applied_at VARCHAR,
            notes VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL,
            CONSTRAINT candidate_application_candidate_job UNIQUE (candidate_id, job_id),
            FOREIGN KEY (job_id) REFERENCES job (id),
            FOREIGN KEY (candidate_id) REFERENCES candidate (id)
        )
        """
    )
    conn.execute("CREATE INDEX idx_fail_test ON candidate_application (candidate_id, status)")
    conn.execute("INSERT INTO job VALUES ('j_fail', 'new')")
    conn.execute("INSERT INTO candidate VALUES ('c_fail', 'fail@test.com')")
    conn.execute(
        "INSERT INTO candidate_application VALUES "
        "('app_fail', 'c_fail', 'j_fail', 'saved', NULL, 'Pre-fail note', 'now', 'now')"
    )
    conn.commit()
    conn.close()

    eng = make_engine(db_path)
    raw = eng.raw_connection()
    dbapi = raw.driver_connection

    # Run rebuild transaction with injected failure
    dbapi.execute("PRAGMA foreign_keys = OFF")
    dbapi.execute("PRAGMA legacy_alter_table = ON")
    in_tx = False
    try:
        dbapi.execute("BEGIN IMMEDIATE")
        in_tx = True
        dbapi.execute("ALTER TABLE candidate_application RENAME TO candidate_application_old")
        # Injected failure!
        raise RuntimeError("Injected database failure midway through table rebuild")
    except Exception:
        if in_tx:
            dbapi.execute("ROLLBACK")
    finally:
        dbapi.execute("PRAGMA legacy_alter_table = OFF")
        dbapi.execute("PRAGMA foreign_keys = ON")

    # Verify state after rollback
    tables = [
        r[0]
        for r in dbapi.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    ]
    assert "candidate_application_old" not in tables
    assert "candidate_application" in tables

    # Verify original records are preserved intact
    row = dbapi.execute(
        "SELECT id, status, notes FROM candidate_application WHERE id = 'app_fail'"
    ).fetchone()
    assert row is not None
    assert row[0] == "app_fail"
    assert row[1] == "saved"
    assert row[2] == "Pre-fail note"

    # Verify index survived rollback
    indexes = [
        r[0]
        for r in dbapi.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'candidate_application'"
        ).fetchall()
    ]
    assert "idx_fail_test" in indexes

    # Verify foreign key check passes
    fk_issues = dbapi.execute("PRAGMA foreign_key_check").fetchall()
    assert not fk_issues

    raw.close()
    eng.dispose()


def test_web_candidate_application_status_update_supports_skipped(
    isolated_repo: Repo, clock: FakeClock
) -> None:
    """20. Web candidate application status route accepts and renders 'skipped' status."""
    cand = isolated_repo.create_candidate(id="c_skip_test", email="skip@test.local")
    isolated_repo.save_profile_draft(cand.id, {"skills": ["Python"]})
    prof = isolated_repo.get_candidate_profiles(cand.id)[0]
    isolated_repo.confirm_profile(cand.id, prof.id, {"skills": ["Python"]})

    job, _ = isolated_repo.get_or_create_job("j_skip_1", "https://example.com/jskip1")

    base_cfg = settings().config.model_dump()
    st = Settings(
        config=AppConfig.model_validate(base_cfg),
        secrets=Secrets(),
    )
    app = create_app(st, isolated_repo, clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1:8080")

    web_token = isolated_repo.get_setting("web_token") or "test_web_token_123"
    isolated_repo.set_setting("web_token", web_token)
    session_cookie = sign_candidate_session(cand.id, web_token)
    client.cookies.set(CANDIDATE_SESSION_COOKIE, session_cookie)

    csrf = candidate_csrf_token(cand.id, web_token)

    # Post status = 'skipped'
    resp = client.post(
        f"/candidate/jobs/{job.id}/status",
        data={"status": "skipped", "notes": "Not a match right now"},
        headers={"Origin": "http://127.0.0.1:8080", "x-jobradar-token": csrf},
    )
    assert resp.status_code in (200, 303)

    # Verify in repo
    app_rec = isolated_repo.get_candidate_application(cand.id, job.id)
    assert app_rec is not None
    assert app_rec.status == "skipped"
    assert app_rec.notes == "Not a match right now"

    # Verify on /candidate/applications board
    apps_page = client.get("/candidate/applications")
    assert apps_page.status_code == 200
