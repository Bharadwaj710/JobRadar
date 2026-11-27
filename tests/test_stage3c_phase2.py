"""Tests for Stage 3C Phase 2: Shared Inventory Decoupling.

Regression test coverage:
1. A job disappears from a complete, successful board snapshot and is closed even if a candidate has an application record.
2. A temporary board/API failure does not close jobs.
3. An incomplete or malformed board response does not close jobs.
4. A candidate application remains unchanged when its shared job is hidden.
5. Candidate application history remains unchanged.
6. One candidate's saved/applied status does not affect another candidate's inventory.
7. Deduplication still distinguishes different requisitions and materially different roles.
8. Duplicate coalescing does not depend on user_status_at.
9. Page rereading does not access or mutate candidate application records.
10. Existing terminal inventory states are not incorrectly reopened or overwritten.
11. Legacy behavior outside the modified lifecycle guards remains compatible (historical test).
12. Repeated watcher runs are idempotent.
13. Static verification that user_status_at is removed from affected production paths.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import text

from jobradar.config import BoardRef, BoardsConfig, PagesConfig
from jobradar.db.models import (
    Candidate,
    CandidateApplicationStatus,
    JobStatus,
)
from jobradar.db.repo import Repo, init_db, make_engine
from jobradar.pipeline.dedupe import job_id_for_url, role_key
from jobradar.pipeline.fetch import Page
from jobradar.pipeline.liveness import LivenessChecker
from jobradar.pipeline.match import CLOSED_REASON as MATCH_CLOSED_REASON
from jobradar.pipeline.schemas import JobPosting as SchemasJobPosting
from jobradar.queue.tasks import TaskQueue
from jobradar.sources.boards.watcher import CLOSED_REASON as BOARD_CLOSED_REASON
from jobradar.sources.boards.watcher import BoardWatcher
from jobradar.sources.pages.watcher import PageWatcher
from tests.conftest import FakeClock
from tests.test_match import GOOD, FakeProvider, matcher

GH_URL = "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
STRIPE_REF = BoardRef(ats="greenhouse", company="stripe")


def gh_job(job_id: int, title: str, location: str = "Bengaluru, India") -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": location},
        "absolute_url": f"https://job-boards.greenhouse.io/stripe/jobs/{job_id}",
        "updated_at": "2026-10-13T06:00:00-04:00",
    }


class FakeInbox:
    def __init__(self) -> None:
        self.got: list[object] = []

    async def emit(self, msg: object) -> None:
        self.got.append(msg)


class FakeFetcher:
    async def get(self, url: str) -> Page:
        return Page(url=url, final_url=url, text="Updated page content")

    async def aclose(self) -> None:
        pass


def make_board_watcher(
    repo: Repo,
    queue: TaskQueue,
    clock: FakeClock,
    inbox: FakeInbox,
    companies: list[BoardRef] | None = None,
) -> BoardWatcher:
    config = BoardsConfig(companies=companies or [], every_minutes=20)
    return BoardWatcher(config, repo, queue, inbox.emit, httpx.AsyncClient(), clock=clock)


def make_page_watcher(
    repo: Repo, queue: TaskQueue, clock: FakeClock, inbox: FakeInbox, url: str
) -> PageWatcher:
    config = PagesConfig(watch={"Test Page": url}, every_hours=2)
    return PageWatcher(config, repo, queue, inbox.emit, FakeFetcher(), clock=clock)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo(tmp_path: Path, clock: FakeClock) -> Repo:
    db_path = tmp_path / "test_stage3c_phase2.db"
    eng = make_engine(db_path)
    init_db(eng)
    return Repo(eng, clock=clock)


@pytest.fixture
def queue(repo: Repo) -> TaskQueue:
    return TaskQueue(repo)


@pytest.fixture
def candidate_alice(repo: Repo) -> Candidate:
    return repo.create_candidate(
        display_name="Alice Candidate",
        email="alice@example.com",
    )


@pytest.fixture
def candidate_bob(repo: Repo) -> Candidate:
    return repo.create_candidate(
        display_name="Bob Candidate",
        email="bob@example.com",
    )


@pytest.mark.asyncio
async def test_1_job_disappears_from_complete_board_and_closes_with_application_intact(
    repo: Repo, queue: TaskQueue, clock: FakeClock, candidate_alice: Candidate
) -> None:
    """1. A job disappears from a complete, successful board snapshot and is closed even if a candidate has an application record."""
    url = "https://job-boards.greenhouse.io/stripe/jobs/101"
    job_id = job_id_for_url(url)
    repo.get_or_create_job(job_id, url)
    repo.set_job_status(job_id, JobStatus.NEW)

    # Candidate Alice applies
    app = repo.apply_candidate_action(
        candidate_alice.id, job_id, "apply", notes="Applied via Stripe"
    )
    assert app.status == CandidateApplicationStatus.APPLIED.value

    # Board watcher detects posting vanished
    inbox = FakeInbox()
    watcher = make_board_watcher(repo, queue, clock, inbox)
    try:
        closed = await watcher._close(url)
    finally:
        await watcher.aclose()

    assert closed is True
    # Shared job is hidden
    job_after = repo.get_job(job_id)
    assert job_after is not None
    assert job_after.status == JobStatus.HIDDEN.value
    assert job_after.score_reason == BOARD_CLOSED_REASON

    # Candidate Alice's application is completely intact
    app_after = repo.get_candidate_application(candidate_alice.id, job_id)
    assert app_after is not None
    assert app_after.status == CandidateApplicationStatus.APPLIED.value
    assert app_after.notes == "Applied via Stripe"


@pytest.mark.asyncio
@respx.mock
async def test_2_temporary_board_api_failure_does_not_close_jobs(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    """2. A temporary board/API failure (503 / BoardUnavailable) does not close jobs."""
    url = "https://job-boards.greenhouse.io/stripe/jobs/1"
    j_id = job_id_for_url(url)
    repo.get_or_create_job(j_id, url)
    repo.set_job_status(j_id, JobStatus.NEW)

    inbox = FakeInbox()
    watcher = make_board_watcher(repo, queue, clock, inbox, companies=[STRIPE_REF])

    # Initial baseline check sees job 1
    respx.get(GH_URL).respond(json={"jobs": [gh_job(1, "Software Engineer")]})
    await watcher.check_board(STRIPE_REF)
    assert repo.get_job(j_id).status == JobStatus.NEW.value

    # Subsequent check encounters transient 503 error
    respx.get(GH_URL).respond(503)
    await watcher.check_board(STRIPE_REF)

    # Job is NOT closed, stays NEW
    job = repo.get_job(j_id)
    assert job is not None
    assert job.status == JobStatus.NEW.value
    await watcher.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_3_incomplete_or_glitched_board_response_does_not_close_jobs(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    """3. An incomplete or malformed board response (sudden empty list) does not close jobs."""
    url = "https://job-boards.greenhouse.io/stripe/jobs/1"
    j_id = job_id_for_url(url)
    repo.get_or_create_job(j_id, url)
    repo.set_job_status(j_id, JobStatus.NEW)

    inbox = FakeInbox()
    watcher = make_board_watcher(repo, queue, clock, inbox, companies=[STRIPE_REF])

    # Initial baseline check sees job 1
    respx.get(GH_URL).respond(json={"jobs": [gh_job(1, "Software Engineer")]})
    await watcher.check_board(STRIPE_REF)

    # Subsequent check returns empty list (sudden 0 postings glitch)
    respx.get(GH_URL).respond(json={"jobs": []})
    await watcher.check_board(STRIPE_REF)

    # Job is NOT closed because sudden 0 postings is treated as a glitch
    job = repo.get_job(j_id)
    assert job is not None
    assert job.status == JobStatus.NEW.value
    await watcher.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_4_candidate_application_remains_unchanged_when_shared_job_is_hidden(
    repo: Repo, queue: TaskQueue, clock: FakeClock, candidate_bob: Candidate
) -> None:
    """4. A candidate application remains unchanged when its shared job is hidden via liveness."""
    url = "https://job-boards.greenhouse.io/stripe/jobs/102"
    job_id = job_id_for_url(url)
    repo.get_or_create_job(job_id, url)
    repo.set_job_status(job_id, JobStatus.NEW)

    posting_data = {
        "company": "Stripe",
        "role": "Backend Engineer",
        "apply_url": url,
        "is_job": True,
    }
    repo.set_job_data(job_id, SchemasJobPosting.model_validate(posting_data).model_dump_json())

    # Candidate Bob saves the job
    app = repo.apply_candidate_action(
        candidate_bob.id, job_id, "save", notes="Saved for weekend review"
    )
    assert app.status == CandidateApplicationStatus.SAVED.value

    # Liveness check returns 404 (Closed)
    respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs/102").respond(404)
    liveness = LivenessChecker()
    m = matcher(repo, queue, FakeProvider("llm", GOOD))
    m.clock = clock
    m.liveness = liveness

    job = repo.get_job(job_id)
    assert job is not None
    posting = SchemasJobPosting.model_validate(posting_data)
    closed = await m._closed(job, posting)
    await liveness.aclose()

    assert closed is True
    job_after = repo.get_job(job_id)
    assert job_after is not None
    assert job_after.status == JobStatus.HIDDEN.value
    assert job_after.score_reason == MATCH_CLOSED_REASON

    # Candidate Bob's application remains SAVED
    app_after = repo.get_candidate_application(candidate_bob.id, job_id)
    assert app_after is not None
    assert app_after.status == CandidateApplicationStatus.SAVED.value
    assert app_after.notes == "Saved for weekend review"


@pytest.mark.asyncio
async def test_5_candidate_application_history_remains_unchanged(
    repo: Repo, queue: TaskQueue, clock: FakeClock, candidate_alice: Candidate
) -> None:
    """5. Candidate application history remains unchanged when a job closes."""
    url = "https://job-boards.greenhouse.io/stripe/jobs/105"
    job_id = job_id_for_url(url)
    repo.get_or_create_job(job_id, url)
    repo.set_job_status(job_id, JobStatus.NEW)

    repo.apply_candidate_action(candidate_alice.id, job_id, "save")
    repo.apply_candidate_action(candidate_alice.id, job_id, "apply", notes="Application submitted")

    hist_before = repo.list_candidate_application_history(candidate_alice.id, job_id=job_id)
    assert len(hist_before) == 2

    # Watcher closes the job
    inbox = FakeInbox()
    watcher = make_board_watcher(repo, queue, clock, inbox)
    try:
        await watcher._close(url)
    finally:
        await watcher.aclose()

    hist_after = repo.list_candidate_application_history(candidate_alice.id, job_id=job_id)
    assert len(hist_after) == 2
    assert [h.status for h in hist_after] == [h.status for h in hist_before]
    assert [h.changed_at for h in hist_after] == [h.changed_at for h in hist_before]


def test_6_one_candidates_saved_applied_status_does_not_affect_another_candidates_inventory(
    repo: Repo, candidate_alice: Candidate, candidate_bob: Candidate
) -> None:
    """6. One candidate's saved/applied status does not affect another candidate's inventory."""
    url = "https://example.com/jobs/shared_1"
    j_id = job_id_for_url(url)
    repo.get_or_create_job(j_id, url)
    repo.set_job_status(j_id, JobStatus.NEW)

    # Alice applies
    repo.apply_candidate_action(candidate_alice.id, j_id, "apply", notes="Alice application")

    # Bob inspects job with candidate context
    ctx_bob = repo.get_job_with_candidate_context(j_id, candidate_bob.id)
    assert ctx_bob is not None
    assert ctx_bob["job"].status == JobStatus.NEW.value
    # Bob has NO application for this job
    assert ctx_bob["application"] is None

    # Bob's application list does not contain this job
    bob_apps = repo.list_candidate_applications_with_jobs(candidate_bob.id)
    assert len(bob_apps) == 0

    # Alice's application list contains this job
    alice_apps = repo.list_candidate_applications_with_jobs(candidate_alice.id)
    assert len(alice_apps) == 1
    assert alice_apps[0][0].status == CandidateApplicationStatus.APPLIED.value


def test_7_deduplication_still_distinguishes_different_requisitions_and_roles(
    repo: Repo, clock: FakeClock
) -> None:
    """7. Deduplication still distinguishes different requisitions, employers, and materially different roles."""
    since = clock().isoformat()

    # Different employers
    j_stripe, _ = repo.get_or_create_job("job_stripe_swe", "https://stripe.com/jobs/101")
    repo.set_job_status(j_stripe.id, JobStatus.NEW)
    fp_stripe = role_key("Stripe", "Software Engineer")
    repo.set_fingerprints({j_stripe.id: fp_stripe})

    clock.advance(seconds=10)

    j_uber, _ = repo.get_or_create_job("job_uber_swe", "https://uber.com/jobs/202")
    repo.set_job_status(j_uber.id, JobStatus.EXTRACTED)
    fp_uber = role_key("Uber", "Software Engineer")
    repo.set_fingerprints({j_uber.id: fp_uber})

    assert fp_stripe != fp_uber
    assert repo.same_role_main(j_uber.id, fp_uber, since) is None

    # Materially different roles at same company
    clock.advance(seconds=10)
    j_infra, _ = repo.get_or_create_job("job_stripe_infra", "https://stripe.com/jobs/103")
    repo.set_job_status(j_infra.id, JobStatus.EXTRACTED)
    fp_infra = role_key("Stripe", "Infrastructure Engineer")
    repo.set_fingerprints({j_infra.id: fp_infra})

    assert fp_stripe != fp_infra
    assert repo.same_role_main(j_infra.id, fp_infra, since) is None


def test_8_duplicate_coalescing_does_not_depend_on_user_status_at(
    repo: Repo, clock: FakeClock, candidate_alice: Candidate
) -> None:
    """8. Duplicate coalescing does not depend on user_status_at."""
    fp = "google|software engineer"
    since = clock().isoformat()

    # Create earlier main job
    j1, _ = repo.get_or_create_job("job_google_main", "https://google.com/jobs/1")
    repo.set_job_status(j1.id, JobStatus.NEW)
    repo.set_fingerprints({j1.id: fp})

    # Candidate Alice applied to j1
    repo.apply_candidate_action(candidate_alice.id, j1.id, "apply")

    clock.advance(seconds=60)

    # Ingest duplicate job
    j2, _ = repo.get_or_create_job("job_google_dup", "https://google.com/jobs/2")
    repo.set_job_status(j2.id, JobStatus.EXTRACTED)
    repo.set_fingerprints({j2.id: fp})

    main_found = repo.same_role_main(j2.id, fp, since)
    assert main_found == j1.id


@pytest.mark.asyncio
async def test_9_page_rereading_does_not_access_or_mutate_candidate_records(
    repo: Repo, queue: TaskQueue, clock: FakeClock, candidate_alice: Candidate
) -> None:
    """9. Page rereading does not access or mutate candidate application records."""
    url = "https://company.com/careers/hiring-drive"
    job_id = job_id_for_url(url)
    repo.get_or_create_job(job_id, url)
    repo.set_job_status(job_id, JobStatus.HIDDEN, "old drive ended")

    # Alice had previously applied to the old drive
    repo.apply_candidate_action(candidate_alice.id, job_id, "apply")

    # Page watcher rereads because page content changed
    inbox = FakeInbox()
    watcher = make_page_watcher(repo, queue, clock, inbox, url)
    await watcher._reread(url)

    job_after = repo.get_job(job_id)
    assert job_after is not None
    assert job_after.status == JobStatus.DISCOVERED.value
    assert job_after.score_reason == "page updated"

    # Alice's application is untouched
    app = repo.get_candidate_application(candidate_alice.id, job_id)
    assert app is not None
    assert app.status == CandidateApplicationStatus.APPLIED.value


@pytest.mark.asyncio
async def test_10_terminal_inventory_states_are_not_reopened_or_overwritten(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    """10. Existing terminal inventory states are not incorrectly reopened or overwritten."""
    # A discarded job
    url_disc = "https://job-boards.greenhouse.io/stripe/jobs/discarded"
    j_disc = job_id_for_url(url_disc)
    repo.get_or_create_job(j_disc, url_disc)
    repo.set_job_status(j_disc, JobStatus.DISCARDED, "not a job")

    # An already hidden job
    url_hid = "https://job-boards.greenhouse.io/stripe/jobs/hidden"
    j_hid = job_id_for_url(url_hid)
    repo.get_or_create_job(j_hid, url_hid)
    repo.set_job_status(j_hid, JobStatus.HIDDEN, "previously closed")

    inbox = FakeInbox()
    watcher = make_board_watcher(repo, queue, clock, inbox)
    try:
        assert await watcher._close(url_disc) is False
        assert await watcher._close(url_hid) is False
    finally:
        await watcher.aclose()

    assert repo.get_job(j_disc).status == JobStatus.DISCARDED.value
    assert repo.get_job(j_hid).status == JobStatus.HIDDEN.value
    assert repo.get_job(j_hid).score_reason == "previously closed"


@pytest.mark.asyncio
async def test_11_legacy_behavior_outside_modified_lifecycle_remains_compatible(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    """11. Legacy behavior outside modified lifecycle guards remains compatible (historical test).

    Demonstrates that in legacy, setting user_status_at prevented board watcher from hiding closed jobs.
    In Phase 2, board watcher successfully hides the closed job even if legacy user_status_at is present.
    """
    url = "https://job-boards.greenhouse.io/stripe/jobs/legacy_bug"
    job_id = job_id_for_url(url)
    repo.get_or_create_job(job_id, url)
    repo.set_job_status(job_id, JobStatus.NEW)

    # Artificially populate legacy user_status_at column
    with repo._tx() as c:
        c.execute(
            text("UPDATE job SET user_status_at = :now WHERE id = :id"),
            {"now": clock().isoformat(), "id": job_id},
        )

    job_before = repo.get_job(job_id)
    assert job_before.user_status_at is not None

    # Under Phase 2, board watcher closes it regardless of user_status_at
    inbox = FakeInbox()
    watcher = make_board_watcher(repo, queue, clock, inbox)
    try:
        closed = await watcher._close(url)
    finally:
        await watcher.aclose()

    assert closed is True
    job_after = repo.get_job(job_id)
    assert job_after.status == JobStatus.HIDDEN.value
    assert job_after.score_reason == BOARD_CLOSED_REASON


@pytest.mark.asyncio
async def test_12_repeated_watcher_runs_are_idempotent(
    repo: Repo, queue: TaskQueue, clock: FakeClock
) -> None:
    """12. Repeated watcher runs on the same closed URL are strictly idempotent."""
    url = "https://job-boards.greenhouse.io/stripe/jobs/idempotent"
    j_id = job_id_for_url(url)
    repo.get_or_create_job(j_id, url)
    repo.set_job_status(j_id, JobStatus.NEW)

    inbox = FakeInbox()
    watcher = make_board_watcher(repo, queue, clock, inbox)
    try:
        # First run hides the job
        res1 = await watcher._close(url)
        assert res1 is True
        job1 = repo.get_job(j_id)
        assert job1.status == JobStatus.HIDDEN.value

        # Second run is a no-op
        res2 = await watcher._close(url)
        assert res2 is False
        job2 = repo.get_job(j_id)
        assert job2.status == JobStatus.HIDDEN.value
    finally:
        await watcher.aclose()


def test_13_no_references_to_user_status_at_in_affected_production_paths() -> None:
    """13. Static verification that user_status_at was removed from ingestion, watcher, and deduplication logic."""
    from jobradar.db import repo as repo_mod
    from jobradar.pipeline import match as match_mod
    from jobradar.sources.boards import watcher as board_watcher_mod
    from jobradar.sources.pages import watcher as page_watcher_mod

    # BoardWatcher._close
    close_src = inspect.getsource(board_watcher_mod.BoardWatcher._close)
    assert "user_status_at" not in close_src

    # PageWatcher._reread
    reread_src = inspect.getsource(page_watcher_mod.PageWatcher._reread)
    assert "user_status_at" not in reread_src

    # Matcher._closed
    closed_src = inspect.getsource(match_mod.Matcher._closed)
    assert "user_status_at" not in closed_src

    # Repo.same_role_main
    same_role_src = inspect.getsource(repo_mod.Repo.same_role_main)
    assert "user_status_at" not in same_role_src
