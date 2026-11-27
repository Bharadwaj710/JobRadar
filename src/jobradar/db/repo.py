"""All database queries (swap SQLite/Postgres here).

Every method opens its own short transaction. Task-queue state changes are single SQL
statements, so they stay atomic even if the process dies between calls.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, bindparam, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import Session, SQLModel, col, create_engine, func, select

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.db import models  # also registers every table on SQLModel.metadata
from jobradar.db.models import (
    Artifact,
    Candidate,
    CandidateApplication,
    CandidateApplicationHistory,
    CandidateApplicationStatus,
    CandidateHistoricalAssessment,
    CandidateJobAssessment,
    CandidateProfile,
    FeedBoard,
    FetchedPage,
    Job,
    JobLead,
    JobStatus,
    JobTrackMatch,
    Platform,
    RawMessage,
    Resume,
    Source,
    TaskStatus,
    WatchedPage,
)

if TYPE_CHECKING:
    from jobradar.config import Settings
    from jobradar.pipeline.eligibility import EligibilityResult
    from jobradar.pipeline.leads import LeadEnrichment
    from jobradar.pipeline.technical_match import FourTrackAssessment, TrackAssessment


def get_default_db_path() -> Path:
    env_path = os.environ.get("JOBRADAR_DB_PATH")
    if env_path:
        return Path(env_path)
    return Path("data/jobradar.db")


DEFAULT_DB_PATH = get_default_db_path()


def make_engine(path: Path | None = None) -> Engine:
    target_path = path or get_default_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{target_path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


class OwnerResolutionError(RuntimeError):
    """Raised when the owner candidate cannot be safely or unambiguously resolved."""


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
    _add_new_columns(engine)
    _refresh_source_platforms(engine)
    _refresh_candidate_application_statuses(engine)


def backup_database(engine: Engine, destination: Path) -> Path:
    """Create and integrity-check a consistent SQLite online backup.

    SQLite's backup API includes committed WAL contents while readers such as the dashboard are
    connected.  Refusing to overwrite makes the backup safe to use as a migration checkpoint.
    """

    if destination.exists():
        raise FileExistsError(f"backup already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    raw = engine.raw_connection()
    target = sqlite3.connect(destination)
    try:
        dbapi = raw.driver_connection
        if dbapi is None:
            raise RuntimeError("no database connection available for backup")
        dbapi.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise RuntimeError(f"database backup failed integrity check: {check}")
    finally:
        target.close()
        raw.close()
    return destination


def _refresh_source_platforms(engine: Engine) -> None:
    """SQLite can't change a CHECK constraint in place, so when a new source platform appears
    (e.g. "board"), `source` is rebuilt with the new list. A backup of the database file is
    written next to it first."""
    with engine.connect() as c:
        sql = c.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'source'")
        ).scalar()
    if sql is None:
        return
    new = [p.value for p in Platform if f"'{p.value}'" not in sql]
    if not new:
        return
    raw = engine.raw_connection()
    try:
        dbapi = raw.driver_connection
        if dbapi is None:
            raise RuntimeError("no database connection to rebuild `source` with")
        if path := engine.url.database:
            # Named after what's new, so an earlier backup is never overwritten.
            backup = sqlite3.connect(f"{path}.before-{'-'.join(new)}-sources.bak")
            dbapi.backup(backup)
            backup.close()
        columns = ", ".join(c.name for c in models.Source.__table__.columns)  # type: ignore[attr-defined]
        create = str(CreateTable(models.Source.__table__).compile(engine))  # type: ignore[attr-defined]
        dbapi.execute("PRAGMA foreign_keys = OFF")
        dbapi.execute("PRAGMA legacy_alter_table = ON")  # keep raw_message pointing at "source"
        try:
            dbapi.execute("BEGIN")
            dbapi.execute("ALTER TABLE source RENAME TO source_old")
            dbapi.execute(create)
            dbapi.execute(f"INSERT INTO source ({columns}) SELECT {columns} FROM source_old")  # noqa: S608 (column names from the model)
            dbapi.execute("DROP TABLE source_old")
            problems = dbapi.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"foreign key problems after rebuilding source: {problems[:3]}")
            dbapi.execute("COMMIT")
        except Exception:
            dbapi.execute("ROLLBACK")
            raise
        finally:
            dbapi.execute("PRAGMA legacy_alter_table = OFF")
            dbapi.execute("PRAGMA foreign_keys = ON")
    finally:
        raw.close()


def _refresh_candidate_application_statuses(engine: Engine) -> None:
    """SQLite can't change a CHECK constraint in place, so when a new application status
    appears (e.g. 'skipped'), candidate_application is rebuilt with the new constraint."""
    with engine.connect() as c:
        sql = c.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'candidate_application'"
            )
        ).scalar()
    if sql is None:
        return
    if "'skipped'" in sql:
        return
    raw = engine.raw_connection()
    try:
        dbapi = raw.driver_connection
        if dbapi is None:
            raise RuntimeError("no database connection to rebuild `candidate_application` with")
        if path := engine.url.database:
            backup = sqlite3.connect(f"{path}.before-skipped-application-status.bak")
            dbapi.backup(backup)
            backup.close()
        # Capture existing triggers before rename
        triggers = dbapi.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'candidate_application'"
        ).fetchall()
        columns = ", ".join(c.name for c in models.CandidateApplication.__table__.columns)  # type: ignore[attr-defined]
        create = str(CreateTable(models.CandidateApplication.__table__).compile(engine))  # type: ignore[attr-defined]

        # SQLite does not allow PRAGMA foreign_keys inside an active transaction.
        if getattr(dbapi, "in_transaction", False):
            dbapi.commit()
        dbapi.execute("PRAGMA foreign_keys = OFF")
        fk_status = dbapi.execute("PRAGMA foreign_keys").fetchone()
        if fk_status and fk_status[0] != 0:
            raise RuntimeError(
                "Failed to disable foreign keys prior to candidate_application rebuild"
            )
        dbapi.execute("PRAGMA legacy_alter_table = ON")
        in_tx = False
        try:
            dbapi.execute("BEGIN IMMEDIATE")
            in_tx = True
            dbapi.execute("ALTER TABLE candidate_application RENAME TO candidate_application_old")
            dbapi.execute(create)
            dbapi.execute(
                f"INSERT INTO candidate_application ({columns}) "  # noqa: S608
                f"SELECT {columns} FROM candidate_application_old"
            )
            dbapi.execute("DROP TABLE candidate_application_old")
            # Recreate all model indexes
            for idx in models.CandidateApplication.__table__.indexes:  # type: ignore[attr-defined]
                dbapi.execute(str(CreateIndex(idx).compile(engine)))
            # Recreate all preserved triggers
            for _trg_name, trg_sql in triggers:
                if trg_sql:
                    dbapi.execute(trg_sql)
            problems = dbapi.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(
                    f"foreign key problems after rebuilding candidate_application: {problems[:3]}"
                )
            dbapi.execute("COMMIT")
            in_tx = False
        except Exception:
            if in_tx:
                dbapi.execute("ROLLBACK")
            raise
        finally:
            dbapi.execute("PRAGMA legacy_alter_table = OFF")
            dbapi.execute("PRAGMA foreign_keys = ON")
    finally:
        raw.close()


def _add_new_columns(engine: Engine) -> None:
    """create_all makes missing tables but not columns added to existing ones. Adds them;
    only nullable columns may be added to an existing table."""
    with engine.begin() as c:
        for table in SQLModel.metadata.sorted_tables:
            have = {row[1] for row in c.execute(text(f"PRAGMA table_info('{table.name}')"))}
            for column in table.columns:
                if column.name not in have:
                    if not column.nullable:
                        raise RuntimeError(f"cannot add required column {table.name}.{column.name}")
                    kind = column.type.compile(engine.dialect)
                    c.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}'))


@dataclass(frozen=True)
class Sighting:
    """One message that posted a job, with where it came from."""

    platform: str
    chat_id: str
    chat_title: str | None
    message_id: str
    text: str | None
    posted_at: str


@dataclass(frozen=True)
class TaskRecord:
    id: int
    type: str
    key: str
    payload: dict[str, Any]
    attempts: int  # including the current attempt


@dataclass(frozen=True)
class TrackDashboardCounts:
    total_jobs: int
    assessed_jobs: int
    stale_jobs: int
    eligibility: dict[str, int]


class Repo:
    def __init__(self, engine: Engine, clock: Clock = utcnow) -> None:
        self.engine = engine
        self._clock = clock

    def _now(self) -> str:
        return to_iso(self._clock())

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    # ---- sources and messages ---------------------------------------------------------------

    def upsert_source(self, platform: Platform, chat_id: str, title: str | None = None) -> Source:
        with Session(self.engine) as s:
            src = s.exec(
                select(Source).where(Source.platform == platform.value, Source.chat_id == chat_id)
            ).first()
            if src is None:
                src = Source(platform=platform.value, chat_id=chat_id, title=title)
            elif title and src.title != title:
                src.title = title
            s.add(src)
            s.commit()
            s.refresh(src)
            return src

    def set_last_msg_id(self, source_id: int, last_msg_id: int) -> None:
        """Advance the catch-up point; never moves it backwards."""
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE source SET last_msg_id = :m "
                    "WHERE id = :id AND (last_msg_id IS NULL OR last_msg_id < :m)"
                ),
                {"id": source_id, "m": last_msg_id},
            )

    def save_raw_message(
        self,
        source_id: int,
        message_id: str,
        posted_at: str,
        text_: str | None = None,
        urls: list[str] | None = None,
        media_path: str | None = None,
    ) -> tuple[int, bool]:
        """Store a message before any processing. Returns (id, created); duplicates are no-ops."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "INSERT INTO raw_message "
                    "(source_id, message_id, text, urls_json, media_path, posted_at, received_at,"
                    " processed) "
                    "VALUES (:sid, :mid, :text, :urls, :media, :posted, :now, 0) "
                    "ON CONFLICT (source_id, message_id) DO NOTHING RETURNING id"
                ),
                {
                    "sid": source_id,
                    "mid": message_id,
                    "text": text_,
                    "urls": json.dumps(urls or []),
                    "media": media_path,
                    "posted": posted_at,
                    "now": self._now(),
                },
            ).first()
            if row is not None:
                return int(row[0]), True
            existing = c.execute(
                text("SELECT id FROM raw_message WHERE source_id = :sid AND message_id = :mid"),
                {"sid": source_id, "mid": message_id},
            ).one()
            return int(existing[0]), False

    def get_raw_message(self, raw_id: int) -> RawMessage | None:
        with Session(self.engine) as s:
            return s.get(RawMessage, raw_id)

    def mark_processed(self, raw_id: int) -> None:
        with self._tx() as c:
            c.execute(text("UPDATE raw_message SET processed = 1 WHERE id = :id"), {"id": raw_id})

    # ---- jobs ---------------------------------------------------------------------------------

    def get_or_create_job(self, job_id: str, canonical_url: str | None) -> tuple[Job, bool]:
        """Returns (job, created). Safe to call repeatedly for the same id."""
        now = self._now()
        with self._tx() as c:
            created = (
                c.execute(
                    text(
                        "INSERT INTO job (id, canonical_url, status, created_at, updated_at) "
                        "VALUES (:id, :url, :status, :now, :now) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {"id": job_id, "url": canonical_url, "status": JobStatus.DISCOVERED.value,
                     "now": now},
                ).first()
                is not None
            )  # fmt: skip
        job = self.get_job(job_id)
        if job is None:  # canonical_url belonged to another id; cannot happen with sha1 ids
            raise RuntimeError(f"job {job_id} conflicts on canonical_url {canonical_url}")
        return job, created

    def get_job(self, job_id: str) -> Job | None:
        with Session(self.engine) as s:
            return s.get(Job, job_id)

    def add_job_source(self, job_id: str, raw_message_id: int) -> bool:
        """Record that a message posted this job. True if this pairing is new."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "INSERT INTO job_source (job_id, raw_message_id) VALUES (:job, :raw) "
                    "ON CONFLICT DO NOTHING RETURNING job_id"
                ),
                {"job": job_id, "raw": raw_message_id},
            ).first()
        if row is not None:
            self.touch_job(job_id)
        return row is not None

    def touch_job(self, job_id: str) -> None:
        with self._tx() as c:
            c.execute(
                text("UPDATE job SET updated_at = :now WHERE id = :id"),
                {"id": job_id, "now": self._now()},
            )

    def job_sightings(self, job_id: str) -> list[Sighting]:
        """Every message that posted this job, oldest first."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT s.platform, s.chat_id, s.title, m.message_id, m.text, m.posted_at "
                    "FROM job_source js JOIN raw_message m ON m.id = js.raw_message_id "
                    "JOIN source s ON s.id = m.source_id WHERE js.job_id = :job "
                    "ORDER BY m.posted_at, m.id"
                ),
                {"job": job_id},
            ).all()
        return [Sighting(*r) for r in rows]

    def job_post_contexts(self, job_id: str, limit: int = 30) -> list[tuple[str | None, list[str]]]:
        """For each post carrying this job's link: (post text, ids of the other jobs in it)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT m.id, m.text, group_concat(other.job_id) FROM job_source js
                    JOIN raw_message m ON m.id = js.raw_message_id
                    LEFT JOIN job_source other
                        ON other.raw_message_id = m.id AND other.job_id != js.job_id
                    WHERE js.job_id = :job
                    GROUP BY m.id ORDER BY m.posted_at, m.id LIMIT :limit
                    """
                ),
                {"job": job_id, "limit": limit},
            ).all()
        return [(r[1], r[2].split(",") if r[2] else []) for r in rows]

    def job_urls(self, status: JobStatus) -> list[tuple[str, str]]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT id, canonical_url FROM job "
                    "WHERE status = :status AND canonical_url IS NOT NULL"
                ),
                {"status": status.value},
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    def jobs_with_sightings(self, at_least: int, status: JobStatus) -> list[str]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT j.id FROM job j JOIN job_source js ON js.job_id = j.id "
                    "WHERE j.status = :status GROUP BY j.id HAVING count(*) >= :n"
                ),
                {"status": status.value, "n": at_least},
            ).all()
        return [str(r[0]) for r in rows]

    def set_job_status(self, job_id: str, status: JobStatus, reason: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    """
                    UPDATE job SET status = :status,
                        score_reason = coalesce(:reason, score_reason), updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "status": status.value, "reason": reason, "now": self._now()},
            )

    def latest_artifact(self, job_id: str, kind: str = "resume") -> Artifact | None:
        with Session(self.engine) as s:
            query = (
                select(Artifact)
                .where(Artifact.job_id == job_id, Artifact.kind == kind)
                .order_by(col(Artifact.version).desc())
            )
            return s.exec(query).first()

    def next_version(self, job_id: str, kind: str = "resume") -> int:
        latest = self.latest_artifact(job_id, kind)
        return latest.version + 1 if latest else 1

    def save_artifact(
        self, job_id: str, kind: str, version: int, local_path: str, content_json: str
    ) -> Artifact:
        """One version of this job's resume (or kit)."""
        artifact = Artifact(
            job_id=job_id, kind=kind, version=version, local_path=local_path,
            content_json=content_json, created_at=self._now(),
        )  # fmt: skip
        with Session(self.engine) as s:
            s.add(artifact)
            s.commit()
            s.refresh(artifact)
        return artifact

    def artifacts_since(self, since: str, kind: str = "resume") -> int:
        with self._tx() as c:
            row = c.execute(
                text("SELECT COUNT(*) FROM artifact WHERE kind = :k AND created_at >= :since"),
                {"k": kind, "since": since},
            ).first()
        return int(row[0]) if row else 0

    def jobs_with_prefix(self, prefix: str) -> list[str]:
        """Ids of jobs starting with this (at most a few)."""
        if not re.fullmatch(r"[0-9a-f]{4,40}", prefix):
            return []
        with self._tx() as c:
            rows = c.execute(
                text("SELECT id FROM job WHERE id LIKE :p LIMIT 5"), {"p": prefix + "%"}
            ).all()
        return [str(r[0]) for r in rows]

    def jobs_in_notion(self) -> list[str]:
        """Ids of jobs that have a Notion page."""
        with self._tx() as c:
            rows = c.execute(text("SELECT id FROM job WHERE notion_page_id IS NOT NULL")).all()
        return [str(r[0]) for r in rows]

    def set_notion_page(self, job_id: str, page_id: str | None) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE job SET notion_page_id = :page,"
                    " notion_synced_at = CASE WHEN :page IS NULL THEN NULL ELSE :now END "
                    "WHERE id = :id"
                ),
                {"id": job_id, "page": page_id, "now": self._now()},
            )

    # ---- task queue ---------------------------------------------------------------------------

    def enqueue(
        self,
        type_: str,
        key: str,
        payload: dict[str, Any] | None = None,
        run_after: str = "",
        priority: int = 0,
    ) -> None:
        """Insert a task, or merge into the existing (type, key) row:

        - pending: payload replaced, keeps the earlier run_after
        - running: payload replaced and the task runs once more after the current attempt
        - done / failed: reset to pending with a fresh attempt count
        """
        now = self._now()
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO task (type, key, payload, status, attempts, run_after, rerun,"
                    " priority) "
                    "VALUES (:type, :key, :payload, 'pending', 0, :run_after, 0, :priority) "
                    "ON CONFLICT (type, key) DO UPDATE SET "
                    " payload = excluded.payload,"
                    " priority = max(coalesce(priority, 0), excluded.priority),"
                    " run_after = CASE status"
                    "   WHEN 'pending' THEN min(run_after, excluded.run_after)"
                    "   WHEN 'running' THEN run_after"
                    "   ELSE excluded.run_after END,"
                    " attempts = CASE WHEN status IN ('done', 'failed') THEN 0 ELSE attempts END,"
                    " last_error = CASE WHEN status IN ('done', 'failed') THEN NULL"
                    "   ELSE last_error END,"
                    " rerun = CASE WHEN status = 'running' THEN 1 ELSE rerun END,"
                    " status = CASE WHEN status IN ('done', 'failed') THEN 'pending'"
                    "   ELSE status END"
                ),
                {
                    "type": type_,
                    "key": key,
                    "payload": json.dumps(payload or {}),
                    "run_after": run_after or now,
                    "priority": priority,
                },
            )

    def claim(self, types: list[str] | None = None) -> TaskRecord | None:
        """Atomically take the next ready task (optionally of the given types): highest
        priority first, then the oldest."""
        type_filter = ""
        params: dict[str, Any] = {"now": self._now()}
        if types:
            names = [f":t{i}" for i in range(len(types))]
            type_filter = f" AND type IN ({', '.join(names)})"
            params |= {f"t{i}": t for i, t in enumerate(types)}
        with self._tx() as c:
            # type_filter only adds generated placeholder names; values stay bound parameters.
            sql = (
                "UPDATE task SET status = 'running', attempts = attempts + 1,"  # noqa: S608
                " started_at = :now WHERE id = (SELECT id FROM task"
                " WHERE status = 'pending' AND run_after <= :now"
                f"{type_filter} ORDER BY coalesce(priority, 0) DESC, run_after, id LIMIT 1) "
                "RETURNING id, type, key, payload, attempts"
            )
            row = c.execute(
                text(sql),
                params,
            ).first()
        if row is None:
            return None
        return TaskRecord(
            id=row.id,
            type=row.type,
            key=row.key,
            payload=json.loads(row.payload),
            attempts=row.attempts,
        )

    def complete(self, task_id: int) -> None:
        now = self._now()
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE task SET"
                    " status = CASE WHEN rerun THEN 'pending' ELSE 'done' END,"
                    " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                    " run_after = CASE WHEN rerun THEN :now ELSE run_after END,"
                    " rerun = 0, started_at = NULL, last_error = NULL "
                    "WHERE id = :id AND status = 'running'"
                ),
                {"id": task_id, "now": now},
            )

    def fail(self, task_id: int, error: str, retry_at: str | None) -> None:
        """Record a failed attempt: back to pending at `retry_at`, or `failed` when None.

        A task re-enqueued while it was running always gets a fresh run instead.
        """
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE task SET"
                    " status = CASE WHEN rerun THEN 'pending'"
                    "   WHEN :retry_at IS NULL THEN 'failed' ELSE 'pending' END,"
                    " run_after = CASE WHEN rerun THEN :now"
                    "   ELSE coalesce(:retry_at, run_after) END,"
                    " attempts = CASE WHEN rerun THEN 0 ELSE attempts END,"
                    " rerun = 0, started_at = NULL, last_error = :error "
                    "WHERE id = :id AND status = 'running'"
                ),
                {"id": task_id, "error": error[:2000], "retry_at": retry_at, "now": self._now()},
            )

    def defer(self, task_id: int, until: str) -> None:
        """Put a running task back without counting the attempt (e.g. Telegram FloodWait)."""
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE task SET status = 'pending', run_after = :until,"
                    " attempts = max(attempts - 1, 0), rerun = 0, started_at = NULL "
                    "WHERE id = :id AND status = 'running'"
                ),
                {"id": task_id, "until": until},
            )

    def reclaim_stuck(self, started_before: str) -> int:
        """Return tasks left `running` by a crash to `pending`. Returns how many."""
        with self._tx() as c:
            result = c.execute(
                text(
                    "UPDATE task SET status = 'pending', run_after = :now, started_at = NULL,"
                    " rerun = 0 WHERE status = 'running' AND started_at < :cutoff"
                ),
                {"now": self._now(), "cutoff": started_before},
            )
            return result.rowcount

    def requeue_failed(self) -> int:
        with self._tx() as c:
            result = c.execute(
                text(
                    "UPDATE task SET status = 'pending', attempts = 0, run_after = :now "
                    "WHERE status = 'failed'"
                ),
                {"now": self._now()},
            )
            return result.rowcount

    def has_ready_tasks(self, types: list[str]) -> bool:
        """True while any task of these types is running or due to run now."""
        names = [f":t{i}" for i in range(len(types))]
        params: dict[str, Any] = {"now": self._now()} | {f"t{i}": t for i, t in enumerate(types)}
        sql = (
            "SELECT 1 FROM task WHERE (status = 'running'"  # noqa: S608  (placeholders only)
            " OR (status = 'pending' AND run_after <= :now))"
            f" AND type IN ({', '.join(names)}) LIMIT 1"
        )
        with self._tx() as c:
            return c.execute(text(sql), params).first() is not None

    # ---- fetch and extraction ---------------------------------------------------------------

    def save_page(
        self,
        job_id: str,
        final_url: str | None,
        title: str | None,
        text_: str | None,
        note: str | None = None,
        structured: dict[str, Any] | None = None,
    ) -> None:
        with Session(self.engine) as s:
            page = s.get(FetchedPage, job_id) or FetchedPage(job_id=job_id, fetched_at="")
            page.final_url, page.title, page.text, page.note = final_url, title, text_, note
            page.structured_json = json.dumps(structured) if structured else None
            page.fetched_at = self._now()
            s.add(page)
            s.commit()

    def get_page(self, job_id: str) -> FetchedPage | None:
        with Session(self.engine) as s:
            return s.get(FetchedPage, job_id)

    def save_extraction(
        self, job_id: str, data_json: str, company: str | None, role: str | None,
        deadline: str | None, prompt_version: str, status: JobStatus,
        fingerprint: str | None = None,
    ) -> None:  # fmt: skip
        with self._tx() as c:
            c.execute(
                text(
                    """
                    UPDATE job SET data_json = :data, company = :company, role = :role,
                        deadline = :deadline, prompt_version = :version, status = :status,
                        fingerprint = :fingerprint, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"id": job_id, "data": data_json, "company": company, "role": role,
                 "deadline": deadline, "version": prompt_version, "status": status.value,
                 "fingerprint": fingerprint, "now": self._now()},
            )  # fmt: skip

    def jobs_without_fingerprint(self) -> list[tuple[str, str | None, str | None]]:
        """(id, company, role) of extracted jobs with no role key yet."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT id, company, role FROM job WHERE fingerprint IS NULL "
                    "AND data_json IS NOT NULL AND status != 'discarded'"
                )
            ).all()
        return [(str(r[0]), r[1], r[2]) for r in rows]

    def set_fingerprints(self, keys: dict[str, str]) -> None:
        with self._tx() as c:
            for job_id, key in keys.items():
                c.execute(
                    text("UPDATE job SET fingerprint = :fp WHERE id = :id"),
                    {"fp": key, "id": job_id},
                )

    def same_role_main(self, job_id: str, fingerprint: str, since: str) -> str | None:
        """The oldest open job with this company and role, first seen since `since` and
        before this one; None if there is none."""
        with self._tx() as c:
            row = c.execute(
                text(
                    """
                    SELECT id FROM job
                    WHERE fingerprint = :fp AND id != :id
                      AND status IN ('extracted', 'new') AND created_at >= :since
                      AND created_at < (SELECT created_at FROM job WHERE id = :id)
                    ORDER BY created_at, id LIMIT 1
                    """
                ),
                {"fp": fingerprint, "id": job_id, "since": since},
            ).first()
        return str(row[0]) if row else None

    def copy_sightings(self, from_job: str, to_job: str) -> int:
        """Add every message that posted `from_job` to `to_job` too. Returns how many."""
        with self._tx() as c:
            result = c.execute(
                text(
                    "INSERT INTO job_source (job_id, raw_message_id) "
                    "SELECT :to, raw_message_id FROM job_source WHERE job_id = :from "
                    "ON CONFLICT DO NOTHING"
                ),
                {"to": to_job, "from": from_job},
            )
        if result.rowcount:
            self.touch_job(to_job)
        return int(result.rowcount or 0)

    def set_job_data(self, job_id: str, data_json: str, prompt_version: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "UPDATE job SET data_json = :data, "
                    "prompt_version = coalesce(:version, prompt_version), updated_at = :now "
                    "WHERE id = :id"
                ),
                {
                    "id": job_id,
                    "data": data_json,
                    "version": prompt_version,
                    "now": self._now(),
                },
            )

    # ---- matching -----------------------------------------------------------------------------

    def save_match(self, job_id: str, version: str, provider: str, answers_json: str) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    """
                    INSERT INTO match_result (job_id, profile_version, provider, answers_json,
                        created_at)
                    VALUES (:job, :version, :provider, :answers, :now)
                    ON CONFLICT (job_id, profile_version) DO UPDATE SET
                        provider = excluded.provider, answers_json = excluded.answers_json,
                        created_at = excluded.created_at
                    """
                ),
                {"job": job_id, "version": version, "provider": provider,
                 "answers": answers_json, "now": self._now()},
            )  # fmt: skip

    @staticmethod
    def _upsert_track_match(
        c: Connection,
        job_id: str,
        version: str,
        assessment: TrackAssessment,
        eligibility: EligibilityResult,
        now: str,
    ) -> None:
        c.execute(
            text(
                """
                INSERT INTO job_track_match (
                    job_id, profile_version, track, technical_score, evidence_coverage,
                    eligibility_status, recommended_resume, explanation, assessment_json,
                    eligibility_json, created_at, updated_at
                ) VALUES (
                    :job, :version, :track, :score, :coverage, :eligibility,
                    :resume, :explanation, :assessment_json, :eligibility_json, :now, :now
                )
                ON CONFLICT (job_id, profile_version, track) DO UPDATE SET
                    technical_score = excluded.technical_score,
                    evidence_coverage = excluded.evidence_coverage,
                    eligibility_status = excluded.eligibility_status,
                    recommended_resume = excluded.recommended_resume,
                    explanation = excluded.explanation,
                    assessment_json = excluded.assessment_json,
                    eligibility_json = excluded.eligibility_json,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "job": job_id,
                "version": version,
                "track": assessment.track,
                "score": assessment.score,
                "coverage": assessment.evidence_coverage,
                "eligibility": eligibility.state.value,
                "resume": assessment.recommended_resume,
                "explanation": assessment.explanation,
                "assessment_json": assessment.model_dump_json(),
                "eligibility_json": eligibility.model_dump_json(),
                "now": now,
            },
        )

    def save_track_assessment(
        self,
        job_id: str,
        version: str,
        assessment: TrackAssessment,
        eligibility: EligibilityResult,
    ) -> None:
        """Upsert one track result without changing the job's legacy score or match answers."""

        with self._tx() as c:
            self._upsert_track_match(c, job_id, version, assessment, eligibility, self._now())

    def save_track_assessments(self, job_id: str, version: str, result: FourTrackAssessment) -> int:
        """Upsert every available track atomically; return the number supplied."""

        assessments = list(result.tracks.values())
        now = self._now()
        with self._tx() as c:
            for assessment in assessments:
                self._upsert_track_match(c, job_id, version, assessment, result.eligibility, now)
        return len(assessments)

    def get_track_match(self, job_id: str, version: str, track: str) -> JobTrackMatch | None:
        with Session(self.engine) as s:
            return s.get(JobTrackMatch, (job_id, version, track))

    def track_matches_for_job(self, job_id: str, version: str | None = None) -> list[JobTrackMatch]:
        statement = select(JobTrackMatch).where(col(JobTrackMatch.job_id) == job_id)
        if version is not None:
            statement = statement.where(col(JobTrackMatch.profile_version) == version)
        statement = statement.order_by(col(JobTrackMatch.profile_version), col(JobTrackMatch.track))
        with Session(self.engine) as s:
            return list(s.exec(statement).all())

    def track_matches_for_jobs(
        self, job_ids: list[str], version: str | None = None
    ) -> list[JobTrackMatch]:
        """Track rows for a page of jobs in one query; never mixes versions unless requested."""

        if not job_ids:
            return []
        statement = select(JobTrackMatch).where(col(JobTrackMatch.job_id).in_(job_ids))
        if version is not None:
            statement = statement.where(col(JobTrackMatch.profile_version) == version)
        statement = statement.order_by(
            col(JobTrackMatch.job_id), col(JobTrackMatch.profile_version), col(JobTrackMatch.track)
        )
        with Session(self.engine) as s:
            return list(s.exec(statement).all())

    def track_matches_for_track(
        self, track: str, version: str | None = None
    ) -> list[JobTrackMatch]:
        statement = select(JobTrackMatch).where(col(JobTrackMatch.track) == track)
        if version is not None:
            statement = statement.where(col(JobTrackMatch.profile_version) == version)
        statement = statement.order_by(
            col(JobTrackMatch.job_id), col(JobTrackMatch.profile_version)
        )
        with Session(self.engine) as s:
            return list(s.exec(statement).all())

    def track_matches_for_version(self, version: str) -> list[JobTrackMatch]:
        statement = (
            select(JobTrackMatch)
            .where(col(JobTrackMatch.profile_version) == version)
            .order_by(col(JobTrackMatch.job_id), col(JobTrackMatch.track))
        )
        with Session(self.engine) as s:
            return list(s.exec(statement).all())

    def save_job_lead(self, job_id: str, profile_version: str, enrichment: LeadEnrichment) -> None:
        """Upsert the current verified lead without changing job or match records."""

        now = self._now()
        with self._tx() as c:
            c.execute(
                text(
                    """
                    INSERT INTO job_lead (
                        job_id, profile_version, primary_track, vacancy_status,
                        eligibility_status, application_priority, official_application_url,
                        last_verified_at, enrichment_json, created_at, updated_at
                    ) VALUES (
                        :job, :version, :track, :vacancy, :eligibility, :priority,
                        :application_url, :verified, :enrichment, :now, :now
                    )
                    ON CONFLICT (job_id) DO UPDATE SET
                        profile_version = excluded.profile_version,
                        primary_track = excluded.primary_track,
                        vacancy_status = excluded.vacancy_status,
                        eligibility_status = excluded.eligibility_status,
                        application_priority = excluded.application_priority,
                        official_application_url = excluded.official_application_url,
                        last_verified_at = excluded.last_verified_at,
                        enrichment_json = excluded.enrichment_json,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "job": job_id,
                    "version": profile_version,
                    "track": enrichment.primary_track,
                    "vacancy": enrichment.vacancy_status,
                    "eligibility": enrichment.eligibility_status,
                    "priority": enrichment.application_priority,
                    "application_url": enrichment.official_application_url,
                    "verified": enrichment.last_verified_at.isoformat(),
                    "enrichment": enrichment.model_dump_json(),
                    "now": now,
                },
            )

    def get_job_lead(self, job_id: str) -> JobLead | None:
        with Session(self.engine) as s:
            return s.get(JobLead, job_id)

    def leads_for_jobs(self, job_ids: list[str]) -> list[JobLead]:
        """Lead rows for a page of jobs in one query."""

        if not job_ids:
            return []
        statement = (
            select(JobLead)
            .where(col(JobLead.job_id).in_(job_ids))
            .order_by(col(JobLead.application_priority), col(JobLead.last_verified_at).desc())
        )
        with Session(self.engine) as s:
            return list(s.exec(statement).all())

    def leads_by_priority(self, priority: str, profile_version: str) -> list[JobLead]:
        statement = (
            select(JobLead)
            .where(col(JobLead.application_priority) == priority)
            .where(col(JobLead.profile_version) == profile_version)
            .order_by(col(JobLead.last_verified_at).desc())
        )
        with Session(self.engine) as s:
            return list(s.exec(statement).all())

    def track_dashboard_counts(self, version: str | None) -> TrackDashboardCounts:
        """Distinct-job counts for the active track profile, plus older-only stale jobs."""

        with self._tx() as c:
            total = int(c.execute(text("SELECT count(*) FROM job")).scalar_one())
            stale = int(
                c.execute(
                    text(
                        "SELECT count(DISTINCT old.job_id) FROM job_track_match old "
                        "WHERE :version IS NULL OR NOT EXISTS (SELECT 1 FROM job_track_match cur "
                        "WHERE cur.job_id = old.job_id AND cur.profile_version = :version)"
                    ),
                    {"version": version},
                ).scalar_one()
            )
            if version is None:
                return TrackDashboardCounts(total, 0, stale, {})
            assessed = int(
                c.execute(
                    text(
                        "SELECT count(DISTINCT job_id) FROM job_track_match "
                        "WHERE profile_version = :version"
                    ),
                    {"version": version},
                ).scalar_one()
            )
            rows = c.execute(
                text(
                    "SELECT eligibility_status, count(DISTINCT job_id) FROM job_track_match "
                    "WHERE profile_version = :version GROUP BY eligibility_status"
                ),
                {"version": version},
            ).all()
        return TrackDashboardCounts(
            total,
            assessed,
            stale,
            {str(status): int(count) for status, count in rows},
        )

    def ever_matched(self, job_id: str) -> bool:
        """True if this job has answers for any profile version."""
        with self._tx() as c:
            row = c.execute(
                text("SELECT 1 FROM match_result WHERE job_id = :job LIMIT 1"), {"job": job_id}
            ).first()
        return row is not None

    def set_match_score(self, job_id: str, score: int | None, reason: str) -> bool:
        """Store the score; an extracted job becomes `new` (in the Inbox). True if changed."""
        with self._tx() as c:
            result = c.execute(
                text(
                    """
                    UPDATE job SET score = :score, score_reason = :reason, updated_at = :now,
                        status = CASE WHEN status = 'extracted' THEN 'new' ELSE status END
                    WHERE id = :id AND (score IS NOT :score OR score_reason IS NOT :reason
                        OR status = 'extracted')
                    """
                ),
                {"id": job_id, "score": score, "reason": reason, "now": self._now()},
            )
        return bool(result.rowcount)

    def jobs_to_match(self, version: str) -> list[str]:
        """Open jobs without answers for this profile version."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT id FROM job WHERE status IN ('extracted', 'new')
                        AND NOT EXISTS (SELECT 1 FROM match_result m
                            WHERE m.job_id = job.id AND m.profile_version = :version)
                    ORDER BY created_at
                    """
                ),
                {"version": version},
            ).all()
        return [str(r[0]) for r in rows]

    def jobs_to_track_match(self, version: str, expected_tracks: int = 4) -> list[str]:
        """Open jobs without a complete set of track rows for this profile version."""

        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT id FROM job WHERE status IN ('extracted', 'new')
                        AND (SELECT count(*) FROM job_track_match tm
                            WHERE tm.job_id = job.id AND tm.profile_version = :version)
                            < :expected
                    ORDER BY created_at
                    """
                ),
                {"version": version, "expected": expected_tracks},
            ).all()
        return [str(row[0]) for row in rows]

    def match_answers(self, version: str) -> list[tuple[str, str]]:
        """(job_id, answers_json) for open jobs matched against this profile version."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT m.job_id, m.answers_json FROM match_result m
                    JOIN job ON job.id = m.job_id
                    WHERE m.profile_version = :version AND job.status IN ('extracted', 'new')
                    """
                ),
                {"version": version},
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    def set_scam_risk(self, job_id: str, risk: str) -> None:
        with self._tx() as c:
            c.execute(
                text("UPDATE job SET scam_risk = :risk WHERE id = :id"),
                {"id": job_id, "risk": risk},
            )

    # ---- your statuses (Notion read path) ----------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._tx() as c:
            row = c.execute(text("SELECT value FROM setting WHERE key = :k"), {"k": key}).first()
        return None if row is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO setting (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                ),
                {"k": key, "v": value},
            )

    def set_user_status(self, job_id: str, status: JobStatus) -> bool:
        """Record a Status you set in Notion. False if the job is unknown or unchanged."""
        now = self._now()
        with self._tx() as c:
            result = c.execute(
                text(
                    """
                    UPDATE job SET status = :status, user_status_at = :now, updated_at = :now,
                        score_reason = CASE WHEN :status = 'hidden' THEN 'hidden by you'
                                            ELSE score_reason END
                    WHERE id = :id AND (status != :status OR user_status_at IS NULL)
                    """
                ),
                {"id": job_id, "status": status.value, "now": now},
            )
            if not result.rowcount:
                return False
            c.execute(
                text(
                    "INSERT INTO status_change (job_id, status, changed_at) "
                    "VALUES (:id, :status, :now)"
                ),
                {"id": job_id, "status": status.value, "now": now},
            )
        return True

    def status_counts(self) -> dict[str, int]:
        """How many jobs are in each status right now."""
        with self._tx() as c:
            rows = c.execute(text("SELECT status, count(*) FROM job GROUP BY status")).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def status_changes_since(self, since: str, until: str | None = None) -> dict[str, int]:
        """How many jobs you moved into each status since this time (and before `until`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT status, count(DISTINCT job_id) FROM status_change "
                    "WHERE changed_at >= :since AND (:until IS NULL OR changed_at < :until) "
                    "GROUP BY status"
                ),
                {"since": since, "until": until},
            ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def candidate_status_changes_since(
        self, candidate_id: str, since: str, until: str | None = None
    ) -> dict[str, int]:
        """How many jobs this candidate moved into each status since `since`."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT status, count(DISTINCT job_id) FROM candidate_application_history "
                    "WHERE candidate_id = :cand_id AND changed_at >= :since "
                    "AND (:until IS NULL OR changed_at < :until) "
                    "GROUP BY status"
                ),
                {"cand_id": candidate_id, "since": since, "until": until},
            ).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def scores_by_status(self) -> dict[str, list[int]]:
        """Match Scores of the jobs in each status (jobs without a score are left out)."""
        with self._tx() as c:
            rows = c.execute(text("SELECT status, score FROM job WHERE score IS NOT NULL")).all()
        scores: dict[str, list[int]] = {}
        for status, score in rows:
            scores.setdefault(str(status), []).append(int(score))
        return scores

    # ---- company job boards -------------------------------------------------------------------

    def get_board(self, key: str) -> FeedBoard:
        with Session(self.engine) as s:
            return s.get(FeedBoard, key) or FeedBoard(key=key)

    def save_board(self, board: FeedBoard) -> None:
        with Session(self.engine) as s:
            s.merge(board)
            s.commit()

    # ---- watched pages ------------------------------------------------------------------------

    def get_watched_page(self, url: str, name: str) -> WatchedPage:
        with Session(self.engine) as s:
            return s.get(WatchedPage, url) or WatchedPage(url=url, name=name)

    def save_watched_page(self, page: WatchedPage) -> None:
        with Session(self.engine) as s:
            s.merge(page)
            s.commit()

    # ---- alerts -------------------------------------------------------------------------------

    def queue_alert(self, job_id: str) -> bool:
        """Record that this job deserves an alert. False if it ever had one."""
        with self._tx() as c:
            result = c.execute(
                text(
                    "INSERT INTO alert (job_id, queued_at) VALUES (:job, :now) "
                    "ON CONFLICT (job_id) DO NOTHING"
                ),
                {"job": job_id, "now": self._now()},
            )
        return bool(result.rowcount)

    def pending_alerts(self) -> list[str]:
        with self._tx() as c:
            rows = c.execute(
                text("SELECT job_id FROM alert WHERE outcome IS NULL ORDER BY queued_at")
            ).all()
        return [str(r[0]) for r in rows]

    def alerts_sent_since(self, since: str, until: str | None = None) -> int:
        """Alerts sent (or previewed in a dry run) since this time (and before `until`)."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT count(*) FROM alert WHERE outcome IN ('sent', 'dry_run') "
                    "AND sent_at >= :since AND (:until IS NULL OR sent_at < :until)"
                ),
                {"since": since, "until": until},
            ).one()
        return int(row[0])

    def last_alert_sent_at(self) -> str | None:
        """When the latest alert email went out (or was previewed in a dry run)."""
        with self._tx() as c:
            row = c.execute(
                text("SELECT max(sent_at) FROM alert WHERE outcome IN ('sent', 'dry_run')")
            ).one()
        return None if row[0] is None else str(row[0])

    def finish_alerts(self, job_ids: list[str], outcome: str) -> None:
        with self._tx() as c:
            for job_id in job_ids:
                c.execute(
                    text("UPDATE alert SET outcome = :outcome, sent_at = :now WHERE job_id = :job"),
                    {"job": job_id, "outcome": outcome, "now": self._now()},
                )

    # ---- digest -------------------------------------------------------------------------------

    def jobs_created_since(self, since: str, until: str | None = None) -> list[Job]:
        with Session(self.engine) as s:
            query = select(Job).where(col(Job.created_at) >= since)
            if until is not None:
                query = query.where(col(Job.created_at) < until)
            return list(s.exec(query))

    def jobs_you_moved_to(self, statuses: list[JobStatus]) -> list[Job]:
        """Jobs whose Status you set to one of these, most recent change first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status).in_([st.value for st in statuses]),
                    col(Job.user_status_at).is_not(None),
                )
                .order_by(col(Job.user_status_at).desc())
            )
            return list(s.exec(query))

    def jobs_applied(self) -> list[Job]:
        """Jobs whose Status you set to Applied (and nothing since), oldest first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status) == JobStatus.APPLIED.value,
                    col(Job.user_status_at).is_not(None),
                )
                .order_by(col(Job.user_status_at))
            )
            return list(s.exec(query))

    # ---- the dashboard's reads ------------------------------------------------------------

    def good_new_jobs(self, min_score: int, since: str) -> list[Job]:
        """New jobs scoring at least `min_score`, first seen since `since`, best first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status) == JobStatus.NEW.value,
                    col(Job.score) >= min_score,
                    col(Job.created_at) >= since,
                )
                .order_by(col(Job.score).desc(), col(Job.created_at).desc())
            )
            return list(s.exec(query))

    def jobs_in(self, statuses: list[JobStatus]) -> list[Job]:
        """Jobs in these statuses, the most recently changed first."""
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(col(Job.status).in_([st.value for st in statuses]))
                .order_by(func.coalesce(Job.user_status_at, Job.updated_at).desc(), col(Job.id))
            )
            return list(s.exec(query))

    def search_jobs(
        self, statuses: list[str] | None, words: str, limit: int, offset: int, order: str = "new"
    ) -> tuple[list[Job], int]:
        """A page of jobs in these statuses (None: any) whose company or role contains `words`,
        and how many match in all. `order`: "new" (newest first), "old" or "score"."""
        where: list[Any] = []
        if statuses is not None:
            where.append(col(Job.status).in_(statuses))
        if words := words.strip():
            like = "%" + words.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                col(Job.company).ilike(like, escape="\\") | col(Job.role).ilike(like, escape="\\")
            )
        with Session(self.engine) as s:
            total = s.exec(select(func.count()).select_from(Job).where(*where)).one()
            by: list[Any] = [col(Job.created_at).desc(), col(Job.id)]  # newest first
            if order == "old":
                by = [col(Job.created_at), col(Job.id)]
            elif order == "score":
                by = [col(Job.score).desc().nulls_last(), col(Job.created_at).desc()]
            query = select(Job).where(*where).order_by(*by).limit(limit).offset(offset)
            return list(s.exec(query)), int(total)

    # ---- full-text search (the dashboard's All jobs) ------------------------------------

    _FTS = (
        "CREATE VIRTUAL TABLE IF NOT EXISTS job_fts USING fts5(job_id UNINDEXED, role, company, "
        "places, skills, body, tokenize = 'unicode61 remove_diacritics 2')"
    )

    def jobs_to_index(self, since: str | None) -> list[str]:
        """Jobs whose searchable text may have changed since `since` (all when None): the job
        itself, its page, or a new post about it."""
        with self._tx() as c:
            c.execute(text(self._FTS))
            if since is None:
                rows = c.execute(text("SELECT id FROM job")).all()
            else:
                rows = c.execute(
                    text(
                        "SELECT id FROM job WHERE updated_at > :since "
                        "UNION SELECT job_id FROM fetched_page WHERE fetched_at > :since "
                        "UNION SELECT js.job_id FROM job_source js "
                        "JOIN raw_message m ON m.id = js.raw_message_id "
                        "WHERE m.received_at > :since"
                    ),
                    {"since": since},
                ).all()
        return [str(r[0]) for r in rows]

    def index_jobs(self, rows: list[tuple[str, str, str, str, str, str]]) -> None:
        """(job id, role, company, places, skills, body) into the search index, replacing."""
        if not rows:
            return
        with self._tx() as c:
            c.execute(text(self._FTS))
            for chunk in range(0, len(rows), 200):
                part = rows[chunk : chunk + 200]
                c.execute(
                    text("DELETE FROM job_fts WHERE job_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": [r[0] for r in part]},
                )
                c.execute(
                    text(
                        "INSERT INTO job_fts (job_id, role, company, places, skills, body) "
                        "VALUES (:id, :role, :company, :places, :skills, :body)"
                    ),
                    [
                        {
                            "id": r[0],
                            "role": r[1],
                            "company": r[2],
                            "places": r[3],
                            "skills": r[4],
                            "body": r[5],
                        }
                        for r in part
                    ],
                )

    def find_jobs(
        self,
        statuses: list[str] | None,
        match: str | None,
        min_score: int | None,
        since: str | None,
        source_id: int | None,
        track_version: str | None,
        track: str | None,
        eligibility: str | None,
        technical_min: int | None,
        work_mode: str | None,
        published_since: str | None,
        priority: str | None,
        order: str,
        limit: int,
        offset: int,
        candidate_id: str | None = None,
    ) -> tuple[list[tuple[Job, str | None]], int]:
        """A page of jobs for All jobs, with a text snippet around what `match` (an FTS5
        query) found, and how many match in all. `order`: new, old, score, or relevance."""
        where: list[str] = ["1 = 1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        join = ""
        if match:
            join = "JOIN job_fts f ON f.job_id = j.id"
            where.append("job_fts MATCH :match")
            params["match"] = match
        if statuses is not None:
            marks = ", ".join(f":st{i}" for i in range(len(statuses)))
            where.append(f"j.status IN ({marks})" if statuses else "0 = 1")
            params |= {f"st{i}": st for i, st in enumerate(statuses)}
        if min_score is not None:
            if candidate_id:
                where.append(
                    "EXISTS (SELECT 1 FROM candidate_job_assessment cja "
                    "WHERE cja.job_id = j.id AND cja.candidate_id = :cand_id "
                    "AND cja.technical_score >= :min)"
                )
                params["cand_id"] = candidate_id
                params["min"] = min_score
            else:
                where.append("j.score >= :min")
                params["min"] = min_score
        if since is not None:
            where.append("j.created_at >= :since")
            params["since"] = since
        if source_id is not None:
            where.append(
                "EXISTS (SELECT 1 FROM job_source js JOIN raw_message m ON m.id = js.raw_message_id"
                " WHERE js.job_id = j.id AND m.source_id = :src)"
            )
            params["src"] = source_id
        track_filters: list[str] = []
        if track is not None:
            track_filters.append("tm.track = :track")
            track_filters.append(
                "coalesce(json_extract(tm.assessment_json, "
                "'$.components.role_alignment.value'), 0) > 0"
            )
            params["track"] = track
        if eligibility is not None:
            if eligibility.lower() == "eligible":
                track_filters.append("tm.eligibility_status IN ('ELIGIBLE', 'LIKELY_ELIGIBLE')")
            else:
                track_filters.append("tm.eligibility_status = :eligibility")
                params["eligibility"] = eligibility.upper()
        if technical_min is not None:
            track_filters.append("tm.technical_score >= :technical_min")
            params["technical_min"] = technical_min
        if candidate_id and (
            track is not None or eligibility is not None or technical_min is not None
        ):
            cand_track_filters = ["cja.candidate_id = :cand_id"]
            params["cand_id"] = candidate_id
            if track is not None:
                cand_track_filters.append("cja.track = :cand_track")
                params["cand_track"] = track
            if eligibility is not None:
                if eligibility.lower() == "eligible":
                    cand_track_filters.append(
                        "cja.eligibility_status IN ('ELIGIBLE', 'LIKELY_ELIGIBLE')"
                    )
                else:
                    cand_track_filters.append("cja.eligibility_status = :cand_eligibility")
                    params["cand_eligibility"] = eligibility.upper()
            if technical_min is not None:
                cand_track_filters.append("cja.technical_score >= :cand_tech_min")
                params["cand_tech_min"] = technical_min
            where.append(
                "EXISTS (SELECT 1 FROM candidate_job_assessment cja WHERE cja.job_id = j.id AND "  # noqa: S608
                + " AND ".join(cand_track_filters)
                + ")"
            )
        elif track_filters:
            if track_version is None:
                where.append("0 = 1")
            else:
                params["track_version"] = track_version
                track_filters.insert(0, "tm.profile_version = :track_version")
                # Only fixed clauses are joined here; every external value remains bound.
                where.append(
                    "EXISTS (SELECT 1 FROM job_track_match tm WHERE tm.job_id = j.id AND "  # noqa: S608
                    + " AND ".join(track_filters)
                    + ")"
                )
        if work_mode is not None:
            where.append("json_extract(j.data_json, '$.work_mode') = :work_mode")
            params["work_mode"] = work_mode
        if published_since is not None:
            where.append("date(json_extract(j.data_json, '$.published_on')) >= :published_since")
            params["published_since"] = published_since
        if priority is not None:
            if track_version is None:
                where.append("0 = 1")
            else:
                where.append(
                    "EXISTS (SELECT 1 FROM job_lead lead WHERE lead.job_id = j.id "
                    "AND lead.profile_version = :lead_version "
                    "AND lead.application_priority = :priority)"
                )
                params["lead_version"] = track_version
                params["priority"] = priority
        if candidate_id and order == "score":
            if track is not None:
                join += (
                    " LEFT JOIN ("
                    "   SELECT job_id, MAX(technical_score) AS cand_max_score"
                    "   FROM candidate_job_assessment"
                    "   WHERE candidate_id = :cand_id AND track = :cand_sort_track"
                    "   GROUP BY job_id"
                    " ) cja_best ON cja_best.job_id = j.id"
                )
                params["cand_sort_track"] = track
            else:
                join += (
                    " LEFT JOIN ("
                    "   SELECT job_id, MAX(technical_score) AS cand_max_score"
                    "   FROM candidate_job_assessment"
                    "   WHERE candidate_id = :cand_id"
                    "   GROUP BY job_id"
                    " ) cja_best ON cja_best.job_id = j.id"
                )
            params["cand_id"] = candidate_id
            score_order = (
                "cja_best.cand_max_score IS NULL, cja_best.cand_max_score DESC, "
                "j.created_at DESC, j.id DESC"
            )
        else:
            score_order = "j.score IS NULL, j.score DESC, j.created_at DESC, j.id DESC"

        by = {
            "old": "j.created_at, j.id",
            "score": score_order,
            "relevance": "bm25(job_fts, 0, 8, 6, 3, 4, 1)" if match else "j.created_at DESC",
        }.get(order, "j.created_at DESC, j.id")
        snippet = "snippet(job_fts, -1, char(2), char(3), '…', 14)" if match else "NULL"
        cond = " AND ".join(where)
        with self._tx() as c:
            c.execute(text(self._FTS))
            total = c.execute(text(f"SELECT count(*) FROM job j {join} WHERE {cond}"),  # noqa: S608
                              params).one()[0]  # fmt: skip
            rows = c.execute(
                text(f"SELECT j.id, {snippet} FROM job j {join} WHERE {cond} "  # noqa: S608
                     f"ORDER BY {by} LIMIT :limit OFFSET :offset"),
                params,
            ).all()  # fmt: skip
        ids = [str(r[0]) for r in rows]
        with Session(self.engine) as s:
            found = {j.id: j for j in s.exec(select(Job).where(col(Job.id).in_(ids)))}
        return [(found[r[0]], r[1]) for r in rows if r[0] in found], int(total)

    def sources(self) -> list[tuple[int, str, str]]:
        """(id, platform, title) of every source that posted a job, by title."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT s.id, s.platform, coalesce(s.title, s.chat_id) FROM source s "
                    "WHERE EXISTS (SELECT 1 FROM raw_message m JOIN job_source js "
                    "ON js.raw_message_id = m.id WHERE m.source_id = s.id) ORDER BY 3"
                )
            ).all()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    def last_activity(self) -> str | None:
        """When JobRadar last read anything: a message received or a board checked."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT max(t) FROM (SELECT max(received_at) AS t FROM raw_message "
                    "UNION ALL SELECT max(last_checked) FROM feed_board)"
                )
            ).one()
        return str(row[0]) if row[0] else None

    def latest_match(self, job_id: str) -> str | None:
        """The newest stored match answers (JSON) for this job, or None."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT answers_json FROM match_result WHERE job_id = :job "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"job": job_id},
            ).first()
        return str(row[0]) if row else None

    def good_job_sources(self, min_score: int, since: str) -> list[tuple[str, str, int]]:
        """(platform, chat or board title, jobs) for jobs scoring `min_score`+ first seen since
        `since`, each job counted once per source."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT s.platform, coalesce(s.title, s.chat_id), count(DISTINCT j.id) "
                    "FROM job j JOIN job_source js ON js.job_id = j.id "
                    "JOIN raw_message m ON m.id = js.raw_message_id "
                    "JOIN source s ON s.id = m.source_id "
                    "WHERE j.created_at >= :since AND j.score >= :min "
                    "GROUP BY s.platform, s.id ORDER BY 3 DESC"
                ),
                {"since": since, "min": min_score},
            ).all()
        return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]

    def search_good_count(self, min_score: int) -> int:
        """Jobs that ever scored `min_score` or more (not counting what wasn't a job)."""
        with self._tx() as c:
            row = c.execute(
                text("SELECT count(*) FROM job WHERE score >= :min AND status != 'discarded'"),
                {"min": min_score},
            ).one()
        return int(row[0])

    def jobs_ever_in(self, statuses: list[JobStatus], undo_minutes: int = 0) -> int:
        """Jobs that are, or once were, in one of these statuses (by your status history).
        A status you changed again within `undo_minutes` is taken as a misclick."""
        query = text(
            """
            WITH h AS (
                SELECT job_id, status, changed_at,
                       LEAD(changed_at) OVER (PARTITION BY job_id ORDER BY changed_at, id)
                           AS next_at
                FROM status_change
            )
            SELECT count(*) FROM (
                SELECT id FROM job WHERE status IN :sts
                UNION SELECT job_id FROM h WHERE status IN :sts AND (
                    next_at IS NULL
                    OR (julianday(next_at) - julianday(changed_at)) * 1440 >= :undo)
            )
            """
        ).bindparams(bindparam("sts", expanding=True))
        with self._tx() as c:
            row = c.execute(
                query, {"sts": [st.value for st in statuses], "undo": undo_minutes}
            ).one()
        return int(row[0])

    def applications_by_source(
        self, undo_minutes: int = 10, candidate_id: str | None = None
    ) -> list[tuple[str, str, str, str, bool, bool]]:
        """(platform, source name, job id, its status now, reached the next round, found there
        first) for every job you applied to, once per source that carried it, newest find first.

        Applied means Applied or any later status in your history; the next round means Interview
        or Offer. A status changed again within `undo_minutes` is taken as a misclick and ignored,
        so Interview undone 8 seconds later isn't an interview."""
        if candidate_id:
            query = text(
                """
                WITH h AS (
                    SELECT job_id, status, changed_at,
                           LEAD(changed_at) OVER (PARTITION BY job_id ORDER BY changed_at, id)
                               AS next_at
                    FROM candidate_application_history
                    WHERE candidate_id = :cid
                ),
                kept AS (
                    SELECT job_id, status FROM h
                    WHERE next_at IS NULL
                       OR (julianday(next_at) - julianday(changed_at)) * 1440 >= :undo
                ),
                outcome AS (
                    SELECT job_id,
                           MAX(status IN ('interview', 'interviewing', 'offer', 'offered'))
                           AS next_round
                    FROM kept
                    WHERE status IN (
                        'applied', 'interview', 'interviewing', 'offer', 'offered', 'rejected'
                    )
                    GROUP BY job_id
                ),
                per_source AS (  -- one row per job and source: a repost can't count twice
                    SELECT m.source_id, o.job_id, o.next_round, MIN(m.received_at) AS seen
                    FROM outcome o
                    JOIN job_source js ON js.job_id = o.job_id
                    JOIN raw_message m ON m.id = js.raw_message_id
                    GROUP BY m.source_id, o.job_id
                ),
                first AS (SELECT job_id, MIN(seen) AS seen FROM per_source GROUP BY job_id)
                SELECT s.platform, coalesce(s.title, s.chat_id), p.job_id,
                       coalesce(ca.status, 'applied'), p.next_round,
                       p.seen = f.seen
                FROM per_source p
                JOIN source s ON s.id = p.source_id
                LEFT JOIN candidate_application ca
                    ON ca.candidate_id = :cid AND ca.job_id = p.job_id
                JOIN first f ON f.job_id = p.job_id
                ORDER BY p.seen DESC
                """
            )
            with self._tx() as c:
                rows = c.execute(query, {"undo": undo_minutes, "cid": candidate_id}).all()
            return [
                (str(r[0]), str(r[1]), str(r[2]), str(r[3]), bool(r[4]), bool(r[5])) for r in rows
            ]

        query = text(
            """
            WITH h AS (
                SELECT job_id, status, changed_at,
                       LEAD(changed_at) OVER (PARTITION BY job_id ORDER BY changed_at, id)
                           AS next_at
                FROM status_change
            ),
            kept AS (
                SELECT job_id, status FROM h
                WHERE next_at IS NULL
                   OR (julianday(next_at) - julianday(changed_at)) * 1440 >= :undo
            ),
            outcome AS (
                SELECT job_id, MAX(status IN ('interview', 'offer')) AS next_round
                FROM kept WHERE status IN ('applied', 'interview', 'offer', 'rejected')
                GROUP BY job_id
            ),
            per_source AS (  -- one row per job and source: a repost can't count twice
                SELECT m.source_id, o.job_id, o.next_round, MIN(m.received_at) AS seen
                FROM outcome o
                JOIN job_source js ON js.job_id = o.job_id
                JOIN raw_message m ON m.id = js.raw_message_id
                GROUP BY m.source_id, o.job_id
            ),
            first AS (SELECT job_id, MIN(seen) AS seen FROM per_source GROUP BY job_id)
            SELECT s.platform, coalesce(s.title, s.chat_id), p.job_id, j.status, p.next_round,
                   p.seen = f.seen
            FROM per_source p
            JOIN source s ON s.id = p.source_id
            JOIN job j ON j.id = p.job_id
            JOIN first f ON f.job_id = p.job_id
            ORDER BY p.seen DESC
            """
        )
        with self._tx() as c:
            rows = c.execute(query, {"undo": undo_minutes}).all()
        return [(str(r[0]), str(r[1]), str(r[2]), str(r[3]), bool(r[4]), bool(r[5])) for r in rows]

    def get_artifact(self, artifact_id: int) -> Artifact | None:
        with Session(self.engine) as s:
            return s.get(Artifact, artifact_id)

    def task_waiting(self, task_type: str, key: str) -> bool:
        """A task of this type and key is queued or running."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT 1 FROM task WHERE type = :type AND key = :key "
                    "AND status IN ('pending', 'running') LIMIT 1"
                ),
                {"type": task_type, "key": key},
            ).first()
        return row is not None

    def status_history(self, job_id: str) -> list[tuple[str, str]]:
        """(status, when) for every status you set on this job, oldest first."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT status, changed_at FROM status_change WHERE job_id = :job "
                    "ORDER BY changed_at, id"
                ),
                {"job": job_id},
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]

    def artifacts_of(self, job_id: str) -> list[Artifact]:
        """Every resume and cover letter built for this job, newest first."""
        with Session(self.engine) as s:
            query = (
                select(Artifact)
                .where(Artifact.job_id == job_id)
                .order_by(col(Artifact.created_at).desc(), col(Artifact.id).desc())
            )
            return list(s.exec(query))

    def jobs_closing(self, first_day: str, last_day: str) -> list[Job]:
        """Jobs you may still apply to whose deadline falls in these days (inclusive)."""
        open_ = [JobStatus.NEW.value, JobStatus.SHORTLISTED.value, JobStatus.RESUME_READY.value]
        with Session(self.engine) as s:
            query = (
                select(Job)
                .where(
                    col(Job.status).in_(open_),
                    col(Job.deadline) >= first_day,
                    col(Job.deadline) <= last_day,
                )
                .order_by(col(Job.deadline), col(Job.score).desc())
            )
            return list(s.exec(query))

    def jobs_read_by_llm(self) -> list[Job]:
        """Jobs with extraction data, for comparing extraction methods."""
        with Session(self.engine) as s:
            query = select(Job).where(
                col(Job.data_json).is_not(None),
                col(Job.status).in_([JobStatus.EXTRACTED.value, JobStatus.DISCARDED.value]),
            )
            return list(s.exec(query))

    def jobs_missing_extraction(self) -> list[str]:
        """Jobs that reached Notion before extraction existed (for `jobradar extract`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT id FROM job WHERE status = 'discovered' AND data_json IS NULL "
                    "AND canonical_url IS NOT NULL ORDER BY created_at"
                )
            ).all()
        return [str(r[0]) for r in rows]

    def jobs_from_source(self, title: str) -> list[str]:
        """Jobs first or also seen in the source shown as `title` ("Workday · Accenture"),
        that you haven't moved in Notion (for `jobradar extract --again`)."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT DISTINCT j.id FROM job j JOIN job_source js ON js.job_id = j.id "
                    "JOIN raw_message r ON r.id = js.raw_message_id "
                    "JOIN source s ON s.id = r.source_id "
                    "WHERE lower(s.title) = lower(:title) AND j.user_status_at IS NULL "
                    "AND j.status != 'discarded' AND j.canonical_url IS NOT NULL "
                    "ORDER BY j.created_at"
                ),
                {"title": title},
            ).all()
        return [str(r[0]) for r in rows]

    # ---- LLM cache and spend ------------------------------------------------------------------

    def llm_cache_get(self, key: str) -> str | None:
        with self._tx() as c:
            row = c.execute(
                text("SELECT output FROM llm_cache WHERE key = :key"), {"key": key}
            ).first()
        return None if row is None else str(row[0])

    def llm_cache_put(self, key: str, output: str, cost_inr: float) -> None:
        with self._tx() as c:
            c.execute(
                text(
                    "INSERT INTO llm_cache (key, output, cost_inr, created_at) "
                    "VALUES (:key, :out, :cost, :now) ON CONFLICT (key) DO UPDATE SET "
                    "output = excluded.output, cost_inr = llm_cache.cost_inr + excluded.cost_inr, "
                    "created_at = excluded.created_at"
                ),
                {"key": key, "out": output, "cost": cost_inr, "now": self._now()},
            )

    def llm_spend_since(self, since: str) -> float:
        with self._tx() as c:
            row = c.execute(
                text("SELECT coalesce(sum(cost_inr), 0) FROM llm_cache WHERE created_at >= :s"),
                {"s": since},
            ).one()
        return float(row[0])

    # ---- reporting ----------------------------------------------------------------------------

    def message_counts(self, since: str) -> tuple[int, int]:
        """(messages received since, of which produced no job link)."""
        with self._tx() as c:
            row = c.execute(
                text(
                    "SELECT count(*), sum(processed = 1 AND NOT EXISTS ("
                    " SELECT 1 FROM job_source js WHERE js.raw_message_id = m.id)) "
                    "FROM raw_message m WHERE m.received_at >= :since"
                ),
                {"since": since},
            ).one()
        return int(row[0]), int(row[1] or 0)

    def per_chat(self, since: str) -> list[tuple[str, int, int]]:
        """(chat title or id, posts received, distinct jobs they linked to), busiest first."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT coalesce(s.title, s.chat_id), count(DISTINCT m.id),"
                    " count(DISTINCT js.job_id) "
                    "FROM source s JOIN raw_message m ON m.source_id = s.id "
                    "LEFT JOIN job_source js ON js.raw_message_id = m.id "
                    "WHERE m.received_at >= :since GROUP BY s.id ORDER BY 2 DESC"
                ),
                {"since": since},
            ).all()
        return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]

    def jobs_since(
        self, since: str
    ) -> list[tuple[str, str | None, str, int, int | None, str | None, str | None]]:
        """(id, URL, status, sightings, first post id, first post text, reason) for jobs
        created since."""
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT j.id, j.canonical_url, j.status, count(js.raw_message_id),"
                    " min(js.raw_message_id),"
                    " (SELECT m.text FROM job_source x"
                    "  JOIN raw_message m ON m.id = x.raw_message_id"
                    "  WHERE x.job_id = j.id ORDER BY m.posted_at, m.id LIMIT 1), "
                    " j.score_reason "
                    "FROM job j LEFT JOIN job_source js ON js.job_id = j.id "
                    "WHERE j.created_at >= :since GROUP BY j.id ORDER BY j.created_at"
                ),
                {"since": since},
            ).all()
        return [(str(r[0]), r[1], str(r[2]), int(r[3]), r[4], r[5], r[6]) for r in rows]

    def recent_failures(self, limit: int = 5) -> list[tuple[str, str, str]]:
        with self._tx() as c:
            rows = c.execute(
                text(
                    "SELECT type, key, coalesce(last_error, '') FROM task WHERE status = 'failed' "
                    "ORDER BY run_after DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).all()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    def task_counts(self) -> dict[TaskStatus, int]:
        with self._tx() as c:
            rows = c.execute(text("SELECT status, count(*) FROM task GROUP BY status")).all()
        counts = dict.fromkeys(TaskStatus, 0)
        counts.update({TaskStatus(r[0]): int(r[1]) for r in rows})
        return counts

    # ---- candidate foundation ----------------------------------------------------------------

    def create_candidate(
        self,
        id: str | None = None,
        display_name: str | None = None,
        email: str | None = None,
        name: str | None = None,
    ) -> Candidate:
        cand_id = id or f"cand_{uuid.uuid4().hex[:12]}"
        now = self._now()
        cand = Candidate(
            id=cand_id,
            display_name=display_name or name,
            email=email,
            created_at=now,
            updated_at=now,
        )
        with Session(self.engine) as s:
            s.add(cand)
            s.commit()
            s.refresh(cand)
            return cand

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with Session(self.engine) as s:
            return s.get(Candidate, candidate_id)

    def list_candidates(self) -> list[Candidate]:
        with Session(self.engine) as s:
            return list(s.exec(select(Candidate).order_by(col(Candidate.created_at).desc())).all())

    def resolve_owner_candidate(
        self,
        configured_id: str | None = None,
        owner_email: str | None = None,
    ) -> Candidate:
        """Resolve the workspace owner candidate identity safely.

        Resolution rules:
        1. An explicitly configured owner_candidate_id takes precedence.
        2. If configured, the candidate must exist in the database; otherwise raise
           OwnerResolutionError with an actionable message.
        3. If owner_candidate_id is not configured, resolve ONLY through a reliable, established
           legacy-owner identity (e.g. verified owner_email matching candidate.email).
        4. A candidate being the only confirmed candidate must NOT automatically make that
           candidate the legacy owner.
        5. A candidate being the only candidate in the database must NOT automatically make that
           candidate the legacy owner.
        6. If identity is absent, ambiguous, or cannot be verified, fail safely with
           OwnerResolutionError instructing the operator to configure owner_candidate_id in
           config.yaml or via JOBRADAR_OWNER_CANDIDATE_ID.
        7. Never create a new candidate, duplicate identity, transfer assessments,
           or mutate records.
        """
        # 1 & 2: Explicit configured owner ID
        if configured_id and configured_id.strip():
            cand = self.get_candidate(configured_id.strip())
            if cand is None:
                raise OwnerResolutionError(
                    f"Configured owner_candidate_id '{configured_id}' was not found in database. "
                    "Verify the candidate ID in config.yaml or set JOBRADAR_OWNER_CANDIDATE_ID."
                )
            return cand

        # 3, 4, 5 & 6: Unconfigured ID requires a reliable legacy-owner identity
        if not owner_email or not owner_email.strip():
            raise OwnerResolutionError(
                "Legacy owner candidate identity cannot be verified: owner_candidate_id is not "
                "configured and no verified owner email was provided. Please configure "
                "'owner_candidate_id' in config.yaml or set JOBRADAR_OWNER_CANDIDATE_ID."
            )

        email_norm = owner_email.strip().lower()
        candidates = self.list_candidates()
        if not candidates:
            raise OwnerResolutionError(
                "No candidates found in the database. "
                "Onboard a candidate first or set owner_candidate_id in config.yaml."
            )

        matched = [c for c in candidates if (c.email or "").strip().lower() == email_norm]

        if not matched:
            raise OwnerResolutionError(
                f"Legacy owner identity could not be verified: no candidate found with email "
                f"'{owner_email}'. Please configure 'owner_candidate_id' in config.yaml or set "
                "JOBRADAR_OWNER_CANDIDATE_ID."
            )

        if len(matched) > 1:
            ids = [c.id for c in matched]
            raise OwnerResolutionError(
                f"Ambiguous owner identity: multiple candidates found with email '{owner_email}' "
                f"({ids}). Please explicitly configure 'owner_candidate_id' in config.yaml or set "
                "JOBRADAR_OWNER_CANDIDATE_ID."
            )

        owner = matched[0]
        profiles = self.get_candidate_profiles(owner.id)
        if not any(p.review_status == "confirmed" for p in profiles):
            raise OwnerResolutionError(
                f"Candidate '{owner.id}' matches owner email '{owner_email}' but has no confirmed "
                "profile. Confirm an owner profile or explicitly specify 'owner_candidate_id' in "
                "config.yaml."
            )

        return owner

    def resolve_owner_candidate_id(
        self,
        configured_id: str | None = None,
        owner_email: str | None = None,
    ) -> str:
        """Convenience method returning the resolved owner candidate's ID."""
        return self.resolve_owner_candidate(configured_id, owner_email).id

    def resolve_owner_candidate_from_settings(
        self,
        settings: Settings,
        profile: dict[str, Any] | None = None,
    ) -> Candidate:
        """Resolve workspace owner candidate safely using Settings and profile data.

        Identity evaluation order:
        1. Explicit AppConfig.owner_candidate_id takes precedence.
        2. If unset, inspect the two configured identity sources:
           - Secrets.notify_address (from .env / environment)
           - config/profile.yaml -> basics.email
        3. If both sources are present but disagree, do not silently choose one:
           raise OwnerResolutionError requiring explicit owner_candidate_id.
        4. If both agree, or only one is set, use that verified owner email.
        5. If neither is set, fail safely with OwnerResolutionError.
        """
        configured_id = settings.config.owner_candidate_id
        if configured_id and configured_id.strip():
            return self.resolve_owner_candidate(configured_id=configured_id.strip())

        notify_addr = (settings.secrets.notify_address or "").strip().lower() or None

        profile_data = profile
        if profile_data is None and settings.config.matching.profile_path.is_file():
            try:
                from jobradar.profile import load_profile

                profile_data = load_profile(settings.config.matching.profile_path)
            except Exception:
                profile_data = None

        basics = profile_data.get("basics") if isinstance(profile_data, dict) else None
        profile_email = (
            (basics.get("email") or "").strip().lower() if isinstance(basics, dict) else None
        )

        if notify_addr and profile_email and notify_addr != profile_email:
            raise OwnerResolutionError(
                "Configured owner identity sources disagree: secrets.notify_address "
                f"('{notify_addr}') does not match profile.yaml basics.email "
                f"('{profile_email}'). Please configure 'owner_candidate_id' in config.yaml "
                "or set JOBRADAR_OWNER_CANDIDATE_ID."
            )

        owner_email = notify_addr or profile_email
        if not owner_email:
            raise OwnerResolutionError(
                "Legacy owner candidate identity cannot be verified: owner_candidate_id is not "
                "configured and no owner email was found in secrets or profile.yaml. "
                "Please configure 'owner_candidate_id' in config.yaml or set "
                "JOBRADAR_OWNER_CANDIDATE_ID."
            )

        return self.resolve_owner_candidate(configured_id=None, owner_email=owner_email)

    def resolve_owner_candidate_id_from_settings(
        self,
        settings: Settings,
        profile: dict[str, Any] | None = None,
    ) -> str:
        """Convenience method returning the resolved owner candidate's ID from Settings."""
        return self.resolve_owner_candidate_from_settings(settings, profile).id

    def save_resume(
        self,
        candidate_id: str,
        original_filename: str | None = None,
        stored_path: str = "",
        file_type: str = "pdf",
        file_size: int = 0,
        extraction_status: str = "extracted",
        extracted_text: str | None = None,
        filename: str | None = None,
        raw_text: str | None = None,
        resume_id: str | None = None,
    ) -> Resume:
        r_id = resume_id or f"res_{uuid.uuid4().hex[:12]}"
        now = self._now()
        fname = original_filename or filename or "resume.pdf"
        text = extracted_text or raw_text
        fsize = file_size or len(text or "")
        resume = Resume(
            id=r_id,
            candidate_id=candidate_id,
            original_filename=fname,
            stored_path=stored_path or f"storage/resumes/{r_id}.{file_type}",
            file_type=file_type,
            file_size=fsize,
            extraction_status=extraction_status,
            extracted_text=text,
            created_at=now,
        )
        with Session(self.engine) as s:
            s.add(resume)
            s.commit()
            s.refresh(resume)
            return resume

    def get_resume(self, resume_id: str) -> Resume | None:
        with Session(self.engine) as s:
            return s.get(Resume, resume_id)

    def get_candidate_resumes(self, candidate_id: str) -> list[Resume]:
        with Session(self.engine) as s:
            stmt = (
                select(Resume)
                .where(Resume.candidate_id == candidate_id)
                .order_by(col(Resume.created_at).desc())
            )
            return list(s.exec(stmt).all())

    def save_profile_draft(
        self,
        candidate_id: str,
        resume_id_or_data: Any = None,
        profile_data: dict[str, Any] | None = None,
        resume_id: str | None = None,
        profile_id: str | None = None,
    ) -> CandidateProfile:
        if isinstance(resume_id_or_data, dict):
            p_data = resume_id_or_data
            r_id = resume_id
        else:
            r_id = resume_id or resume_id_or_data
            p_data = profile_data or {}

        p_id = profile_id or f"prof_{uuid.uuid4().hex[:12]}"
        now = self._now()
        with Session(self.engine) as s:
            # Check latest version for this candidate
            latest = s.exec(
                select(CandidateProfile)
                .where(CandidateProfile.candidate_id == candidate_id)
                .order_by(col(CandidateProfile.version).desc())
            ).first()
            version = (
                latest.version
                if latest and latest.review_status == "draft"
                else ((latest.version + 1) if latest else 1)
            )

            if latest and latest.review_status == "draft":
                # Update existing draft
                latest.profile_data_json = json.dumps(p_data)
                latest.resume_id = r_id or latest.resume_id
                latest.updated_at = now
                s.add(latest)
                s.commit()
                s.refresh(latest)
                return latest

            profile = CandidateProfile(
                id=p_id,
                candidate_id=candidate_id,
                resume_id=r_id,
                version=version,
                profile_data_json=json.dumps(p_data),
                review_status="draft",
                is_active=False,
                created_at=now,
                updated_at=now,
            )
            s.add(profile)
            s.commit()
            s.refresh(profile)
            return profile

    create_profile_draft = save_profile_draft

    def confirm_profile(
        self,
        arg1: str,
        arg2: Any = None,
        arg3: dict[str, Any] | None = None,
    ) -> CandidateProfile:
        """Confirm a candidate profile.

        Supports confirm_profile(profile_id, profile_data) and
        confirm_profile(candidate_id, profile_id, profile_data).
        """
        if isinstance(arg2, dict) or arg2 is None:
            profile_id = arg1
            profile_data = arg2
            candidate_id = None
        else:
            candidate_id = arg1
            profile_id = str(arg2)
            profile_data = arg3

        now = self._now()
        with Session(self.engine) as s:
            profile = s.get(CandidateProfile, profile_id)
            if profile is None or (candidate_id and profile.candidate_id != candidate_id):
                raise ValueError("profile not found or unauthorized")

            prev_profiles = s.exec(
                select(CandidateProfile).where(
                    CandidateProfile.candidate_id == profile.candidate_id,
                    col(CandidateProfile.is_active).is_(True),
                )
            ).all()

            # If it was already confirmed, create a new version!
            if profile.review_status == "confirmed":
                for p in prev_profiles:
                    p.is_active = False
                    s.add(p)

                # Monotonically increasing candidate-scoped version to prevent collisions
                max_ver = s.exec(
                    select(col(CandidateProfile.version))
                    .where(CandidateProfile.candidate_id == profile.candidate_id)
                    .order_by(col(CandidateProfile.version).desc())
                ).first()
                new_version = (max_ver or profile.version) + 1
                new_id = uuid.uuid4().hex
                new_profile = CandidateProfile(
                    id=new_id,
                    candidate_id=profile.candidate_id,
                    resume_id=profile.resume_id,
                    version=new_version,
                    profile_data_json=(
                        json.dumps(profile_data)
                        if profile_data is not None
                        else profile.profile_data_json
                    ),
                    review_status="confirmed",
                    is_active=True,
                    confirmed_at=now,
                    created_at=profile.created_at,
                    updated_at=now,
                )
                s.add(new_profile)
                cand = s.get(Candidate, profile.candidate_id)
                if cand:
                    cand.updated_at = now
                    s.add(cand)
                s.commit()
                s.refresh(new_profile)
                return new_profile

            for p in prev_profiles:
                if p.id != profile.id:
                    p.is_active = False
                    s.add(p)

            if profile_data is not None:
                profile.profile_data_json = json.dumps(profile_data)
            profile.review_status = "confirmed"
            profile.is_active = True
            profile.confirmed_at = now
            profile.updated_at = now
            s.add(profile)

            cand = s.get(Candidate, profile.candidate_id)
            if cand:
                cand.updated_at = now
                s.add(cand)

            s.commit()
            s.refresh(profile)
            return profile

    def get_candidate_profiles(self, candidate_id: str) -> list[CandidateProfile]:
        with Session(self.engine) as s:
            stmt = (
                select(CandidateProfile)
                .where(CandidateProfile.candidate_id == candidate_id)
                .order_by(col(CandidateProfile.version).desc())
            )
            return list(s.exec(stmt).all())

    def get_active_profile(self, candidate_id: str) -> CandidateProfile | None:
        with Session(self.engine) as s:
            active = s.exec(
                select(CandidateProfile)
                .where(
                    CandidateProfile.candidate_id == candidate_id,
                    col(CandidateProfile.is_active).is_(True),
                )
                .order_by(col(CandidateProfile.version).desc())
            ).first()
            if active:
                return active
            return s.exec(
                select(CandidateProfile)
                .where(
                    CandidateProfile.candidate_id == candidate_id,
                    CandidateProfile.review_status == "confirmed",
                )
                .order_by(col(CandidateProfile.version).desc())
            ).first()

    def get_profile(self, profile_id: str) -> CandidateProfile | None:
        with Session(self.engine) as s:
            return s.get(CandidateProfile, profile_id)

    # ---- candidate-specific matching assessments -----------------------------------------

    def save_candidate_job_assessment(
        self,
        candidate_id: str,
        job_id: str,
        profile_id: str,
        profile_version: int,
        assessment: TrackAssessment,
        eligibility: EligibilityResult,
        missing_preferred_skills: list[str] | None = None,
    ) -> CandidateJobAssessment:
        """Upsert one candidate-specific track assessment with deterministic identity."""
        now = self._now()
        assessment_id = uuid.uuid4().hex
        reasons = list(eligibility.blockers) + list(eligibility.unresolved)
        pref_missing = missing_preferred_skills or []

        with self._tx() as c:
            c.execute(
                text(
                    """
                    INSERT INTO candidate_job_assessment (
                        id, candidate_id, job_id, profile_id, profile_version, track,
                        technical_score, evidence_coverage, eligibility_status,
                        eligibility_reasons_json, matched_skills_json,
                        missing_required_skills_json, missing_preferred_skills_json,
                        recommended_resume, explanation, assessment_json, eligibility_json,
                        created_at, updated_at
                    ) VALUES (
                        :id, :candidate_id, :job_id, :profile_id, :profile_version, :track,
                        :technical_score, :evidence_coverage, :eligibility_status,
                        :eligibility_reasons_json, :matched_skills_json,
                        :missing_required_skills_json, :missing_preferred_skills_json,
                        :recommended_resume, :explanation, :assessment_json, :eligibility_json,
                        :now, :now
                    )
                    ON CONFLICT (candidate_id, job_id, profile_version, track) DO UPDATE SET
                        profile_id = excluded.profile_id,
                        technical_score = excluded.technical_score,
                        evidence_coverage = excluded.evidence_coverage,
                        eligibility_status = excluded.eligibility_status,
                        eligibility_reasons_json = excluded.eligibility_reasons_json,
                        matched_skills_json = excluded.matched_skills_json,
                        missing_required_skills_json = excluded.missing_required_skills_json,
                        missing_preferred_skills_json = excluded.missing_preferred_skills_json,
                        recommended_resume = excluded.recommended_resume,
                        explanation = excluded.explanation,
                        assessment_json = excluded.assessment_json,
                        eligibility_json = excluded.eligibility_json,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "id": assessment_id,
                    "candidate_id": candidate_id,
                    "job_id": job_id,
                    "profile_id": profile_id,
                    "profile_version": profile_version,
                    "track": assessment.track,
                    "technical_score": assessment.score,
                    "evidence_coverage": assessment.evidence_coverage,
                    "eligibility_status": eligibility.state.value,
                    "eligibility_reasons_json": json.dumps(reasons),
                    "matched_skills_json": json.dumps(assessment.matched_skills),
                    "missing_required_skills_json": json.dumps(assessment.missing_required_skills),
                    "missing_preferred_skills_json": json.dumps(pref_missing),
                    "recommended_resume": assessment.recommended_resume,
                    "explanation": assessment.explanation,
                    "assessment_json": assessment.model_dump_json(),
                    "eligibility_json": eligibility.model_dump_json(),
                    "now": now,
                },
            )

        with Session(self.engine) as s:
            return s.exec(
                select(CandidateJobAssessment).where(
                    CandidateJobAssessment.candidate_id == candidate_id,
                    CandidateJobAssessment.job_id == job_id,
                    CandidateJobAssessment.profile_version == profile_version,
                    CandidateJobAssessment.track == assessment.track,
                )
            ).one()

    def save_candidate_four_track_assessment(
        self,
        candidate_id: str,
        job_id: str,
        profile_id: str,
        profile_version: int,
        result: FourTrackAssessment,
    ) -> list[CandidateJobAssessment]:
        """Save assessments for all tracks in a single atomic transaction."""
        now = self._now()
        reasons = list(result.eligibility.blockers) + list(result.eligibility.unresolved)
        assessments = list(result.tracks.values())

        with self._tx() as c:
            for assessment in assessments:
                assessment_id = uuid.uuid4().hex
                c.execute(
                    text(
                        """
                        INSERT INTO candidate_job_assessment (
                            id, candidate_id, job_id, profile_id, profile_version, track,
                            technical_score, evidence_coverage, eligibility_status,
                            eligibility_reasons_json, matched_skills_json,
                            missing_required_skills_json, missing_preferred_skills_json,
                            recommended_resume, explanation, assessment_json, eligibility_json,
                            created_at, updated_at
                        ) VALUES (
                            :id, :candidate_id, :job_id, :profile_id, :profile_version, :track,
                            :technical_score, :evidence_coverage, :eligibility_status,
                            :eligibility_reasons_json, :matched_skills_json,
                            :missing_required_skills_json, :missing_preferred_skills_json,
                            :recommended_resume, :explanation, :assessment_json, :eligibility_json,
                            :now, :now
                        )
                        ON CONFLICT (candidate_id, job_id, profile_version, track) DO UPDATE SET
                            profile_id = excluded.profile_id,
                            technical_score = excluded.technical_score,
                            evidence_coverage = excluded.evidence_coverage,
                            eligibility_status = excluded.eligibility_status,
                            eligibility_reasons_json = excluded.eligibility_reasons_json,
                            matched_skills_json = excluded.matched_skills_json,
                            missing_required_skills_json = excluded.missing_required_skills_json,
                            missing_preferred_skills_json = excluded.missing_preferred_skills_json,
                            recommended_resume = excluded.recommended_resume,
                            explanation = excluded.explanation,
                            assessment_json = excluded.assessment_json,
                            eligibility_json = excluded.eligibility_json,
                            updated_at = excluded.updated_at
                        """
                    ),
                    {
                        "id": assessment_id,
                        "candidate_id": candidate_id,
                        "job_id": job_id,
                        "profile_id": profile_id,
                        "profile_version": profile_version,
                        "track": assessment.track,
                        "technical_score": assessment.score,
                        "evidence_coverage": assessment.evidence_coverage,
                        "eligibility_status": result.eligibility.state.value,
                        "eligibility_reasons_json": json.dumps(reasons),
                        "matched_skills_json": json.dumps(assessment.matched_skills),
                        "missing_required_skills_json": json.dumps(
                            assessment.missing_required_skills
                        ),
                        "missing_preferred_skills_json": json.dumps([]),
                        "recommended_resume": assessment.recommended_resume,
                        "explanation": assessment.explanation,
                        "assessment_json": assessment.model_dump_json(),
                        "eligibility_json": result.eligibility.model_dump_json(),
                        "now": now,
                    },
                )

        with Session(self.engine) as s:
            stmt = (
                select(CandidateJobAssessment)
                .where(
                    CandidateJobAssessment.candidate_id == candidate_id,
                    CandidateJobAssessment.job_id == job_id,
                    CandidateJobAssessment.profile_version == profile_version,
                )
                .order_by(col(CandidateJobAssessment.track))
            )
            return list(s.exec(stmt).all())

    def get_candidate_job_assessment(
        self,
        candidate_id: str,
        job_id: str,
        profile_version: int,
        track: str,
    ) -> CandidateJobAssessment | None:
        """Retrieve a specific candidate track assessment."""
        with Session(self.engine) as s:
            stmt = select(CandidateJobAssessment).where(
                CandidateJobAssessment.candidate_id == candidate_id,
                CandidateJobAssessment.job_id == job_id,
                CandidateJobAssessment.profile_version == profile_version,
                CandidateJobAssessment.track == track,
            )
            return s.exec(stmt).first()

    def list_candidate_assessments(
        self,
        candidate_id: str,
        job_id: str | None = None,
        profile_version: int | None = None,
        track: str | None = None,
        job_ids: Sequence[str] | None = None,
    ) -> list[CandidateJobAssessment]:
        """Query candidate assessments filtered by job, version, and/or track."""
        with Session(self.engine) as s:
            stmt = select(CandidateJobAssessment).where(
                CandidateJobAssessment.candidate_id == candidate_id
            )
            if job_id is not None:
                stmt = stmt.where(CandidateJobAssessment.job_id == job_id)
            if job_ids is not None:
                stmt = stmt.where(col(CandidateJobAssessment.job_id).in_(list(job_ids)))
            if profile_version is not None:
                stmt = stmt.where(CandidateJobAssessment.profile_version == profile_version)
            if track is not None:
                stmt = stmt.where(CandidateJobAssessment.track == track)
            stmt = stmt.order_by(
                col(CandidateJobAssessment.job_id),
                col(CandidateJobAssessment.profile_version).desc(),
                col(CandidateJobAssessment.track),
            )
            return list(s.exec(stmt).all())

    # ---- candidate historical legacy assessments -----------------------------------------

    def save_historical_assessment(
        self,
        candidate_id: str,
        job_id: str,
        score: int,
        score_reason: str = "",
        scoring_system: str = "legacy_matcher",
        scored_at: str | None = None,
    ) -> CandidateHistoricalAssessment:
        """Preserve historical legacy Matcher score and reasoning for a candidate."""
        now = self._now()
        hist_id = uuid.uuid4().hex
        with self._tx() as c:
            c.execute(
                text(
                    """
                    INSERT INTO candidate_historical_assessment (
                        id, candidate_id, job_id, score, score_reason,
                        scoring_system, scored_at, created_at, updated_at
                    ) VALUES (
                        :id, :cand_id, :job_id, :score, :score_reason,
                        :scoring_system, :scored_at, :now, :now
                    )
                    ON CONFLICT (candidate_id, job_id) DO UPDATE SET
                        score = excluded.score,
                        score_reason = excluded.score_reason,
                        scoring_system = excluded.scoring_system,
                        scored_at = coalesce(
                            excluded.scored_at,
                            candidate_historical_assessment.scored_at
                        ),
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "id": hist_id,
                    "cand_id": candidate_id,
                    "job_id": job_id,
                    "score": score,
                    "score_reason": score_reason,
                    "scoring_system": scoring_system,
                    "scored_at": scored_at,
                    "now": now,
                },
            )
        assessment = self.get_historical_assessment(candidate_id, job_id)
        if assessment is None:
            raise RuntimeError(
                f"failed to retrieve saved historical assessment for {candidate_id}:{job_id}"
            )
        return assessment

    def get_historical_assessment(
        self, candidate_id: str, job_id: str
    ) -> CandidateHistoricalAssessment | None:
        """Fetch a preserved historical legacy assessment for a candidate and job."""
        with Session(self.engine) as s:
            return s.exec(
                select(CandidateHistoricalAssessment).where(
                    CandidateHistoricalAssessment.candidate_id == candidate_id,
                    CandidateHistoricalAssessment.job_id == job_id,
                )
            ).first()

    def list_historical_assessments(self, candidate_id: str) -> list[CandidateHistoricalAssessment]:
        """Fetch all historical legacy assessments for a candidate."""
        with Session(self.engine) as s:
            return list(
                s.exec(
                    select(CandidateHistoricalAssessment)
                    .where(CandidateHistoricalAssessment.candidate_id == candidate_id)
                    .order_by(col(CandidateHistoricalAssessment.score).desc())
                ).all()
            )

    # ---- candidate applications & tracking ------------------------------------------------

    def save_candidate_application(
        self,
        candidate_id: str,
        job_id: str,
        status: str,
        notes: str = "",
        applied_at: str | None = None,
    ) -> CandidateApplication:
        """Save or update candidate-specific application state for a job."""
        allowed_statuses = {s.value for s in CandidateApplicationStatus}
        if status not in allowed_statuses:
            allowed_msg = sorted(allowed_statuses)
            raise ValueError(
                f"Invalid candidate application status '{status}'. Must be one of: {allowed_msg}"
            )

        now = self._now()
        if status == "applied" and not applied_at:
            applied_at = now

        app_id = uuid.uuid4().hex
        with self._tx() as c:
            c.execute(
                text(
                    """
                    INSERT INTO candidate_application (
                        id, candidate_id, job_id, status, applied_at, notes, created_at, updated_at
                    ) VALUES (
                        :id, :cand_id, :job_id, :status, :applied_at, :notes, :now, :now
                    )
                    ON CONFLICT (candidate_id, job_id) DO UPDATE SET
                        status = excluded.status,
                        applied_at = coalesce(
                            excluded.applied_at, candidate_application.applied_at
                        ),
                        notes = excluded.notes,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "id": app_id,
                    "cand_id": candidate_id,
                    "job_id": job_id,
                    "status": status,
                    "applied_at": applied_at,
                    "notes": notes,
                    "now": now,
                },
            )

            c.execute(
                text(
                    """
                    INSERT INTO candidate_application_history (
                        candidate_id, job_id, status, notes, changed_at
                    ) VALUES (
                        :cand_id, :job_id, :status, :notes, :now
                    )
                    """
                ),
                {
                    "cand_id": candidate_id,
                    "job_id": job_id,
                    "status": status,
                    "notes": notes,
                    "now": now,
                },
            )

        app = self.get_candidate_application(candidate_id, job_id)
        if app is None:
            raise RuntimeError(f"Failed to retrieve saved application for {candidate_id}:{job_id}")
        return app

    def get_candidate_application(
        self, candidate_id: str, job_id: str
    ) -> CandidateApplication | None:
        """Retrieve candidate's application record for a specific job."""
        with Session(self.engine) as s:
            stmt = select(CandidateApplication).where(
                CandidateApplication.candidate_id == candidate_id,
                CandidateApplication.job_id == job_id,
            )
            return s.exec(stmt).first()

    def list_candidate_applications(
        self,
        candidate_id: str,
        status: str | None = None,
        job_ids: Sequence[str] | None = None,
    ) -> list[CandidateApplication]:
        """List all application records for a candidate, optionally filtered by status."""
        with Session(self.engine) as s:
            stmt = select(CandidateApplication).where(
                CandidateApplication.candidate_id == candidate_id
            )
            if status is not None:
                stmt = stmt.where(CandidateApplication.status == status)
            if job_ids is not None:
                stmt = stmt.where(col(CandidateApplication.job_id).in_(list(job_ids)))
            stmt = stmt.order_by(col(CandidateApplication.updated_at).desc())
            return list(s.exec(stmt).all())

    def get_candidate_application_counts(self, candidate_id: str) -> dict[str, int]:
        """Summary counts of candidate applications by status."""
        counts = {
            "saved": 0,
            "applied": 0,
            "interviewing": 0,
            "offered": 0,
            "rejected": 0,
            "skipped": 0,
            "total": 0,
        }
        with self._tx() as c:
            rows = c.execute(
                text(
                    """
                    SELECT status, count(*)
                    FROM candidate_application
                    WHERE candidate_id = :cand_id
                    GROUP BY status
                    """
                ),
                {"cand_id": candidate_id},
            ).fetchall()
            for st, cnt in rows:
                st_str = str(st)
                if st_str in counts:
                    counts[st_str] = int(cnt)
                    counts["total"] += int(cnt)
        return counts

    def list_candidate_application_history(
        self, candidate_id: str, job_id: str | None = None
    ) -> list[CandidateApplicationHistory]:
        """List status audit log entries for a candidate's applications."""
        with Session(self.engine) as s:
            stmt = select(CandidateApplicationHistory).where(
                CandidateApplicationHistory.candidate_id == candidate_id
            )
            if job_id is not None:
                stmt = stmt.where(CandidateApplicationHistory.job_id == job_id)
            stmt = stmt.order_by(col(CandidateApplicationHistory.id).desc())
            return list(s.exec(stmt).all())

    def get_candidate_assessment_stats(
        self, candidate_id: str, profile_version: int, track: str | None = None
    ) -> dict[str, int]:
        """Summary statistics of candidate assessments for a profile version."""
        stats = {
            "total": 0,
            "eligible": 0,
            "likely_eligible": 0,
            "needs_verification": 0,
            "not_eligible": 0,
        }
        with self._tx() as c:
            sql = """
                SELECT eligibility_status, count(DISTINCT job_id)
                FROM candidate_job_assessment
                WHERE candidate_id = :cand_id AND profile_version = :version
            """
            params: dict[str, Any] = {"cand_id": candidate_id, "version": profile_version}
            if track:
                sql += " AND track = :track"
                params["track"] = track
            sql += " GROUP BY eligibility_status"
            rows = c.execute(text(sql), params).fetchall()
            for st, cnt in rows:
                cnt_int = int(cnt)
                st_lower = str(st).lower()
                if st_lower in stats:
                    stats[st_lower] = cnt_int
                stats["total"] += cnt_int
        return stats

    def get_candidate_recommendations(
        self,
        candidate_id: str,
        profile_version: int,
        track: str | None = None,
        eligibility: str | None = None,
        min_score: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Retrieve personalized job recommendations for a candidate.

        Joins CandidateJobAssessment with Job details and CandidateApplication status.
        Sorts eligible first, then by technical fit score descending.
        """
        with Session(self.engine) as s:
            stmt = select(CandidateJobAssessment).where(
                CandidateJobAssessment.candidate_id == candidate_id,
                CandidateJobAssessment.profile_version == profile_version,
            )
            if track:
                stmt = stmt.where(CandidateJobAssessment.track == track)
            if eligibility:
                elig_lower = eligibility.lower()
                if elig_lower == "eligible":
                    stmt = stmt.where(
                        col(CandidateJobAssessment.eligibility_status).in_(
                            ["ELIGIBLE", "LIKELY_ELIGIBLE"]
                        )
                    )
                elif elig_lower == "all":
                    pass  # Explicitly include all statuses including NOT_ELIGIBLE
                else:
                    stmt = stmt.where(
                        CandidateJobAssessment.eligibility_status == eligibility.upper()
                    )
            else:
                # Default recommendation feed: keep confirmed NOT_ELIGIBLE separate
                stmt = stmt.where(col(CandidateJobAssessment.eligibility_status) != "NOT_ELIGIBLE")
            if min_score is not None:
                stmt = stmt.where(col(CandidateJobAssessment.technical_score) >= min_score)

            assessments = list(s.exec(stmt).all())

        if not assessments:
            return []

        # If no specific track requested, pick the highest scoring track per job
        if not track:
            best_by_job: dict[str, CandidateJobAssessment] = {}
            for a in assessments:
                current_best = best_by_job.get(a.job_id)
                if current_best is None:
                    best_by_job[a.job_id] = a
                else:
                    a_score = a.technical_score if a.technical_score is not None else -1
                    c_score = (
                        current_best.technical_score
                        if current_best.technical_score is not None
                        else -1
                    )
                    if a_score > c_score:
                        best_by_job[a.job_id] = a
            assessments = list(best_by_job.values())

        job_ids = [a.job_id for a in assessments]
        with Session(self.engine) as s:
            app_stmt = select(CandidateApplication).where(
                CandidateApplication.candidate_id == candidate_id,
                col(CandidateApplication.job_id).in_(job_ids),
            )
            apps = {app.job_id: app for app in s.exec(app_stmt).all()}

            job_stmt = select(Job).where(
                col(Job.id).in_(job_ids),
                col(Job.status).notin_([JobStatus.HIDDEN.value, JobStatus.DISCARDED.value]),
            )
            jobs = {j.id: j for j in s.exec(job_stmt).all()}

        results: list[dict[str, Any]] = []
        for a in assessments:
            job = jobs.get(a.job_id)
            if job is None or job.status in (JobStatus.HIDDEN.value, JobStatus.DISCARDED.value):
                continue
            app = apps.get(a.job_id)
            job_data: dict[str, Any] = {}
            if job and job.data_json:
                try:
                    job_data = json.loads(job.data_json)
                except Exception:
                    job_data = {}

            title = (
                job_data.get("role")
                or job_data.get("title")
                or (job.role if job else "")
                or "Software Engineer"
            )
            company = job_data.get("company") or (job.company if job else "") or "Company"
            location = job_data.get("location") or "India / Remote"
            apply_url = job_data.get("apply_url") or (job.canonical_url if job else "") or "#"

            results.append(
                {
                    "assessment": a,
                    "job_id": a.job_id,
                    "title": title,
                    "company": company,
                    "location": location,
                    "apply_url": apply_url,
                    "track": a.track,
                    "technical_score": a.technical_score,
                    "evidence_coverage": a.evidence_coverage,
                    "eligibility_status": a.eligibility_status,
                    "eligibility_reasons": a.eligibility_reasons,
                    "matched_skills": a.matched_skills,
                    "missing_required_skills": a.missing_required_skills,
                    "missing_preferred_skills": a.missing_preferred_skills,
                    "recommended_resume": a.recommended_resume,
                    "explanation": a.explanation,
                    "application_status": app.status if app else None,
                    "applied_at": app.applied_at if app else None,
                    "application_notes": app.notes if app else "",
                    "created_at": a.created_at,
                }
            )

        eligibility_order = {
            "ELIGIBLE": 1,
            "LIKELY_ELIGIBLE": 2,
            "NEEDS_VERIFICATION": 3,
            "NOT_ELIGIBLE": 4,
        }

        results.sort(
            key=lambda r: (
                eligibility_order.get(r["eligibility_status"], 99),
                -(r["technical_score"] if r["technical_score"] is not None else -1),
                r["company"].lower(),
            )
        )

        return results[:limit]

    def get_job_with_candidate_context(
        self, job_id: str, candidate_id: str
    ) -> dict[str, Any] | None:
        """Retrieve shared job record alongside candidate-isolated assessment and application state.

        Ensures shared Job data is loaded separately from candidate-scoped records.
        Does not mutate or contaminate shared Job records.
        """
        job = self.get_job(job_id)
        if job is None:
            return None

        app = self.get_candidate_application(candidate_id, job_id)
        assessments = self.list_candidate_assessments(candidate_id, job_id=job_id)
        historical = self.get_historical_assessment(candidate_id, job_id)
        resumes = self.get_candidate_resumes(candidate_id)

        return {
            "job": job,
            "application": app,
            "assessments": assessments,
            "historical_assessment": historical,
            "has_resume": bool(resumes),
        }

    def list_candidate_applications_with_jobs(
        self, candidate_id: str, statuses: list[str] | None = None
    ) -> list[tuple[CandidateApplication, Job]]:
        """Retrieve candidate's applications paired with shared Job records.

        Loads candidate application workflow state independently of shared Job.status.
        """
        with Session(self.engine) as s:
            stmt = select(CandidateApplication).where(
                CandidateApplication.candidate_id == candidate_id
            )
            if statuses:
                stmt = stmt.where(col(CandidateApplication.status).in_(statuses))
            stmt = stmt.order_by(col(CandidateApplication.updated_at).desc())
            applications = list(s.exec(stmt).all())

        if not applications:
            return []

        job_ids = [a.job_id for a in applications]
        with Session(self.engine) as s:
            job_stmt = select(Job).where(col(Job.id).in_(job_ids))
            jobs_by_id = {j.id: j for j in s.exec(job_stmt).all()}

        return [(a, jobs_by_id[a.job_id]) for a in applications if a.job_id in jobs_by_id]

    def apply_candidate_action(
        self,
        candidate_id: str,
        job_id: str,
        action: str,
        notes: str = "",
        applied_at: str | None = None,
    ) -> CandidateApplication:
        """Apply a candidate workflow transition strictly within CandidateApplication.

        Translates user action strings into valid CandidateApplicationStatus values.
        Never mutates shared Job.status or Job.user_status_at.
        """
        action_map = {
            "save": CandidateApplicationStatus.SAVED.value,
            "saved": CandidateApplicationStatus.SAVED.value,
            "shortlist": CandidateApplicationStatus.SAVED.value,
            "shortlisted": CandidateApplicationStatus.SAVED.value,
            "resume_ready": CandidateApplicationStatus.SAVED.value,
            "apply": CandidateApplicationStatus.APPLIED.value,
            "applied": CandidateApplicationStatus.APPLIED.value,
            "interview": CandidateApplicationStatus.INTERVIEWING.value,
            "interviewing": CandidateApplicationStatus.INTERVIEWING.value,
            "offer": CandidateApplicationStatus.OFFERED.value,
            "offered": CandidateApplicationStatus.OFFERED.value,
            "reject": CandidateApplicationStatus.REJECTED.value,
            "rejected": CandidateApplicationStatus.REJECTED.value,
            "skip": CandidateApplicationStatus.SKIPPED.value,
            "skipped": CandidateApplicationStatus.SKIPPED.value,
        }
        normalized = action.strip().lower()
        target_status = action_map.get(normalized)
        if not target_status:
            raise ValueError(
                f"Unknown candidate action '{action}'. Must map to one of: "
                f"{sorted(set(action_map.values()))}"
            )

        if normalized == "resume_ready" and "resume_ready" not in notes:
            notes = f"Migrated from legacy resume_ready state. {notes}".strip()

        return self.save_candidate_application(
            candidate_id=candidate_id,
            job_id=job_id,
            status=target_status,
            notes=notes,
            applied_at=applied_at,
        )
