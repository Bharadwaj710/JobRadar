"""Answer JobRadar's emails instead of opening Notion: "I applied", "Not interested",
"I followed up".

Each job card in an alert, digest or reminder email carries small buttons. A button is a
`mailto:` link to your own address with a ready subject such as

    JobRadar action: applied | Software Engineer @ Rubrik | 1a2b3c4d5e6f.0c9d8e7f

You tap it and press Send. A `reply_check` task reads the INBOX over IMAP every 2 minutes
(`EMAIL_ADDRESS` and its app password, the same login that sends alerts) and acts on these
emails: the job's Status becomes Applied or Skipped, or its Followed Up date is set, in
JobRadar and in Notion. The email is then archived (Gmail) or marked read.

Only emails that pass every check count, so nobody else can change your jobs:
- the subject carries the job id and a signature made with a key only this JobRadar has
  (kept in its database), so it must come from a button JobRadar sent you;
- it is from `EMAIL_ADDRESS` or `NOTIFY_TO`;
- the server's own Authentication-Results, when present, show no failed SPF, DKIM or DMARC.

Statuses only move forward: "I applied" works on a job you haven't moved past Applied,
"Not interested" on one you haven't applied to, "I followed up" on an Applied job.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import hashlib
import hmac
import imaplib
import logging
import re
import secrets
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

from jobradar.clock import Clock, to_iso, utcnow
from jobradar.config import Settings
from jobradar.db.models import Job, JobStatus
from jobradar.db.repo import Repo, TaskRecord
from jobradar.pipeline import status_rules as rules
from jobradar.pipeline.dedupe import sha1
from jobradar.pipeline.task_types import REPLY_CHECK
from jobradar.queue.tasks import PermanentError, RetryableError, TaskQueue
from jobradar.sinks.notion_sync import FOLLOWED_UP

log = logging.getLogger(__name__)

Action = Literal["applied", "skip", "followed up", "interview", "rejected"]
PREFIX = "JobRadar action:"
KEY_SETTING = "email_action_key"
DONE = "email_action_done:"  # setting key prefix, per message: handled already
REPLIED = "company_replied:"  # setting key prefix, per job: the company wrote back (JSON)
CHECK_SECONDS = 120
LOOK_BACK = timedelta(days=7)
ID_CHARS = 12
TOKEN_EXPIRY_DAYS = 7

_SUBJECT = re.compile(
    r"JobRadar action:\s*(applied|skip|followed up|interview|rejected)\b.*?"
    r"\b([0-9a-f]{12})\.([0-9a-f]{8})\b",
    re.IGNORECASE | re.DOTALL,
)
_CANDIDATE_SUBJECT = re.compile(
    r"JobRadar action:\s*(applied|skip|followed up|interview|rejected)\b.*?"
    r"\b([0-9a-f]{12})\.([A-Za-z0-9_-]+)\.(\d+)\.([0-9a-f]{16})\b",
    re.IGNORECASE | re.DOTALL,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HR_WORDS = re.compile(r"hr|career|recruit|talent|hiring|jobs|people|placement", re.IGNORECASE)

# The rules are shared with the dashboard (pipeline/status_rules.py).
_FROM = rules.FROM
NOTION_OPTION = {"applied": "Applied", "skip": "Skipped", "interview": "Interview",
                 "rejected": "Rejected"}  # fmt: skip


@dataclass
class ParsedAction:
    action: Action
    prefix: str
    candidate_id: str | None = None
    expiry: int | None = None
    signature: str = ""
    is_legacy: bool = False

    def __iter__(self) -> Any:
        yield self.action
        yield self.prefix

    def __getitem__(self, idx: int) -> Any:
        return (self.action, self.prefix)[idx]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple) and len(other) == 2:
            return (self.action, self.prefix) == other
        if isinstance(other, ParsedAction):
            return (
                self.action == other.action
                and self.prefix == other.prefix
                and self.candidate_id == other.candidate_id
                and self.expiry == other.expiry
                and self.signature == other.signature
                and self.is_legacy == other.is_legacy
            )
        return False


def _title(job: Job) -> str:
    role = (job.role or "Role not stated")[:50]
    return f"{role} @ {job.company[:40]}" if job.company else role


def mailto(to: str, subject: str, body: str) -> str:
    return f"mailto:{quote(to, safe='@')}?subject={quote(subject)}&body={quote(body)}"


class Actions:
    """Makes the signed buttons and checks the emails they produce."""

    def __init__(
        self,
        key: str,
        address: str,
        also_from: str | None = None,
        candidate_id: str | None = None,
        repo: Repo | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self.key = key.encode()
        self.address = address
        self.senders = {a.casefold() for a in (address, also_from) if a}
        self.candidate_id = candidate_id
        self.repo = repo
        self.clock = clock

    @classmethod
    def load(
        cls,
        settings: Settings,
        repo: Repo,
        candidate_id: str | None = None,
        clock: Clock = utcnow,
    ) -> Actions | None:
        """None when the buttons are off or there's no mailbox to read them from."""
        address = settings.secrets.email_address
        if not settings.config.notify.email_actions or not address:
            return None
        key = repo.get_setting(KEY_SETTING)
        if not key:
            key = secrets.token_hex(16)
            repo.set_setting(KEY_SETTING, key)
        if candidate_id is None:
            with contextlib.suppress(Exception):
                cand = repo.resolve_owner_candidate_from_settings(settings)
                candidate_id = cand.id
        return cls(
            key,
            address,
            settings.secrets.notify_address,
            candidate_id=candidate_id,
            repo=repo,
            clock=clock,
        )

    def sign(self, action: str, job_id: str) -> str:
        msg = f"{action.casefold()}:{job_id[:ID_CHARS]}".encode()
        return hmac.new(self.key, msg, hashlib.sha256).hexdigest()[:8]

    def sign_candidate(self, action: str, job_id: str, candidate_id: str, expiry_ts: int) -> str:
        msg = f"{action.casefold()}:{job_id[:ID_CHARS]}:{candidate_id}:{expiry_ts}".encode()
        return hmac.new(self.key, msg, hashlib.sha256).hexdigest()[:16]

    def subject(self, action: Action, job: Job, candidate_id: str | None = None) -> str:
        cid = candidate_id or self.candidate_id
        if cid:
            expiry_ts = int(self.clock().timestamp() + TOKEN_EXPIRY_DAYS * 86400)
            sig = self.sign_candidate(action, job.id, cid, expiry_ts)
            token = f"{job.id[:ID_CHARS]}.{cid}.{expiry_ts}.{sig}"
        else:
            token = f"{job.id[:ID_CHARS]}.{self.sign(action, job.id)}"
        return f"{PREFIX} {action} | {_title(job)} | {token}"

    def link(self, action: Action, job: Job) -> str:
        what = {"applied": "Applied", "skip": "Skipped (not interested)",
                "followed up": "followed up today", "interview": "Interview",
                "rejected": "Rejected"}[action]  # fmt: skip
        body = (
            f"Send this email as it is. JobRadar marks {_title(job)} as {what} within "
            "a few minutes, then archives this email."
        )
        return mailto(self.address, self.subject(action, job), body)

    def job_links(self, job: Job) -> list[tuple[str, str]]:
        """Buttons for a job you haven't applied to yet."""
        if self.candidate_id and self.repo:
            app = self.repo.get_candidate_application(self.candidate_id, job.id)
            if app and app.status in ("applied", "interviewing", "offered", "rejected", "skipped"):
                return []
            return [
                ("I applied", self.link("applied", job)),
                ("Not interested", self.link("skip", job)),
            ]
        if job.status not in _FROM["applied"]:
            return []
        return [
            ("I applied", self.link("applied", job)),
            ("Not interested", self.link("skip", job)),
        ]

    def parse(self, subject: str) -> ParsedAction | None:
        """ParsedAction for a genuine button email; None for anything else."""
        m_cand = _CANDIDATE_SUBJECT.search(subject or "")
        if m_cand:
            action, prefix, cid, exp_str, sig = (
                m_cand.group(1).casefold(),
                m_cand.group(2),
                m_cand.group(3),
                int(m_cand.group(4)),
                m_cand.group(5),
            )
            now_ts = int(self.clock().timestamp())
            if now_ts > exp_str:
                log.info("action token expired", extra={"job_prefix": prefix, "candidate_id": cid})
                return None
            expected_sig = self.sign_candidate(action, prefix, cid, exp_str)
            if not hmac.compare_digest(expected_sig, sig):
                log.warning(
                    "action token signature mismatch",
                    extra={"job_prefix": prefix, "candidate_id": cid},
                )
                return None
            return ParsedAction(
                action=action,  # type: ignore[arg-type]
                prefix=prefix,
                candidate_id=cid,
                expiry=exp_str,
                signature=sig,
                is_legacy=False,
            )

        m = _SUBJECT.search(subject or "")
        if not m:
            return None
        action, prefix, sig = m.group(1).casefold(), m.group(2), m.group(3)
        if not hmac.compare_digest(self.sign(action, prefix), sig):
            return None
        return ParsedAction(
            action=action,  # type: ignore[arg-type]
            prefix=prefix,
            candidate_id=None,
            expiry=None,
            signature=sig,
            is_legacy=True,
        )

    def trusted(self, sender: str, auth_results: str) -> bool:
        addresses = {a.casefold() for _, a in getaddresses([sender]) if a}
        if not addresses or not addresses <= self.senders:
            return False
        return not re.search(r"\b(?:spf|dkim|dmarc)=(?:fail|softfail)\b", auth_results or "", re.I)


def dashboard_url(settings: Settings) -> str:
    """Where links open the dashboard: `web.url` (e.g. through Tailscale, so they work on
    your phone too) or this computer's address."""
    web = settings.config.web
    return web.url or f"http://{web.host}:{web.port}"


_WORDS = {"applied": "I applied", "skip": "Not interested", "followed up": "I followed up",
          "interview": "Yes, set Interview", "rejected": "Yes, set Rejected"}  # fmt: skip


def build_ignored(
    ignored: list[tuple[Job, str]], sender: str, to: str, dashboard: str
) -> EmailMessage:
    """'Not interested ignored for SDE @ Rubrik: it's Applied.' with a link to change it."""
    from jobradar.sinks.email_template import esc, layout

    first, action = ignored[0]
    subject = (
        f"[JobRadar] “{_WORDS.get(action, action)}” didn't change {_title(first)}"
        if len(ignored) == 1
        else f"[JobRadar] {len(ignored)} button emails didn't change anything"
    )
    why = (
        "Buttons only move a job forward, so an old email can't undo a later step. "
        "To change it anyway, use Change status on the job's page in your dashboard."
    )
    lines, rows = [], []
    for job, act in ignored:
        now = rules.YOURS.get(JobStatus(job.status), job.status)
        link = f"{dashboard}/jobs/{job.id}"
        lines.append(f"- {_WORDS.get(act, act)}: {_title(job)} is {now} already. {link}")
        rows.append(
            "<tr><td style='padding:10px 0;border-top:1px solid #d0d7de;font-size:14px'>"
            f"<b>{esc(_WORDS.get(act, act))}</b> on <b>{esc(_title(job))}</b>: it's "
            f"<b>{esc(now)}</b> already. <a href='{esc(link)}'>Change it in the dashboard</a>"
            "</td></tr>"
        )
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("\n".join([why, "", *lines]) + "\n\n— JobRadar\n")
    footer = "The dashboard runs on your computer: start it with <code>jobradar web</code>."
    msg.add_alternative(
        layout("JobRadar", "Nothing changed", why, subject.removeprefix("[JobRadar] "),
               "".join(rows), footer),
        subtype="html",
    )  # fmt: skip
    return msg


def recruiter_email(job: Job, apply_url: str | None, post: str | None) -> str | None:
    """Where a follow-up can go, when it's clear: a mailto apply link, or an address in the
    job's own post that looks like HR or careers, or is on the company's own domain."""
    if apply_url and apply_url.lower().startswith("mailto:"):
        found = _EMAIL.search(apply_url)
        return str(found.group(0)) if found else None
    company = re.sub(r"[^a-z0-9]", "", (job.company or "").casefold())
    for address in dict.fromkeys(str(a) for a in _EMAIL.findall(post or "")):
        local, _, domain = address.partition("@")
        if domain.casefold() in ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com"):
            continue
        squashed = re.sub(r"[^a-z0-9]", "", domain.casefold())
        if _HR_WORDS.search(local) or (len(company) >= 3 and company in squashed):
            return address
    return None


# ---- reading the mailbox ------------------------------------------------------------------


class InboxScanner(Protocol):
    """Something else that reads the inbox while JobRadar is logged in (company replies)."""

    def scan(self, imap: imaplib.IMAP4_SSL) -> list[str]: ...


@dataclass
class Incoming:
    uid: str
    message_id: str
    sender: str
    subject: str
    auth_results: str
    sent: datetime | None


def fetch(settings: Settings, since: date) -> tuple[imaplib.IMAP4_SSL, list[Incoming]]:
    """Log in and read the headers of recent button emails (the connection stays open)."""
    address, password = settings.secrets.email_address, settings.secrets.email_app_password
    if not address or password is None:
        raise PermanentError("EMAIL_ADDRESS / EMAIL_APP_PASSWORD missing in .env")
    host = settings.config.email.imap_host
    try:
        imap = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=30)
    except OSError as e:
        raise RetryableError(f"IMAP connect to {host} failed: {e}") from e
    try:
        imap.login(address, password.get_secret_value())
    except imaplib.IMAP4.abort:
        raise  # the connection dropped mid-login: a retry, not a wrong password
    except imaplib.IMAP4.error as e:
        imap.logout()
        raise PermanentError(
            f"IMAP login rejected ({e}). In Gmail, check Settings > Forwarding and POP/IMAP > "
            "IMAP is enabled; the app password is the one used for alerts."
        ) from e
    imap.select("INBOX")
    _, data = imap.uid("SEARCH", None, f'(SINCE {since:%d-%b-%Y} SUBJECT "{PREFIX}")')  # type: ignore[arg-type]
    found = []
    for uid in (data[0] or b"").decode().split()[-200:]:
        _, parts = imap.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE "
            "AUTHENTICATION-RESULTS)])",
        )  # fmt: skip
        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
        msg = email.message_from_bytes(raw, policy=default_policy)
        try:
            sent = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
        except (TypeError, ValueError):
            sent = None
        found.append(
            Incoming(uid, str(msg["Message-ID"] or uid), str(msg["From"] or ""),
                     str(msg["Subject"] or ""), str(msg["Authentication-Results"] or ""), sent)
        )  # fmt: skip
    return imap, found


_LIST_ROW = re.compile(rb'^\(([^)]*)\) (?:"[^"]*"|NIL) (.+)$')


def all_mail(imap: imaplib.IMAP4_SSL) -> str | None:
    """Gmail's All Mail folder, as LIST names it (quoted): "[Gmail]/All Mail", or in other
    languages "[Google Mail]/Alle Nachrichten" and so on. It's the one flagged \\All."""
    typ, rows = imap.list()
    for row in rows if typ == "OK" else []:
        found = _LIST_ROW.match(row) if isinstance(row, bytes) else None
        if found and b"\\All" in found.group(1).split():
            return found.group(2).decode()
    return None


def archive(imap: imaplib.IMAP4_SSL, uids: list[str]) -> None:
    """Gmail: move them to All Mail, which is what Gmail's Archive does. (Taking the \\Inbox
    label off while INBOX is open is accepted and silently ignored, so they never left.)
    Other servers: mark them read."""
    for uid in uids:
        imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    if "X-GM-EXT-1" not in {str(c).upper() for c in imap.capabilities}:
        return
    target = all_mail(imap)
    if target is None:
        log.warning("Gmail's All Mail folder not found: button emails stay in the inbox, read")
        return
    try:
        typ, data = imap.uid("MOVE", ",".join(uids), target)
    except imaplib.IMAP4.error as e:
        typ, data = "NO", [str(e).encode()]
    if typ != "OK":
        log.warning("button emails not archived", extra={"error": str(data)[:200]})


class ReplyReader:
    def __init__(
        self,
        settings: Settings,
        repo: Repo,
        queue: TaskQueue,
        actions: Actions,
        clock: Clock = utcnow,
        companies: InboxScanner | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.queue = queue
        self.actions = actions
        self.clock = clock
        self.tz = settings.config.notify.tz
        self.companies = companies  # replies from companies you applied to (same login)
        self.ignored: list[tuple[Job, str]] = []  # buttons that couldn't apply, this check
        self.preview_dir: Path | None = None  # dry run: where the "ignored" email is saved

    def schedule(self, delay: float = 0, key: str = "a") -> None:
        self.queue.enqueue(REPLY_CHECK, key, delay=delay)

    async def tick(self, task: TaskRecord) -> None:
        try:
            await self.check_now()
        finally:
            self.schedule(CHECK_SECONDS, key="b" if task.key == "a" else "a")

    async def check_now(self) -> list[str]:
        """Read the inbox once; returns what was done, one line per email acted on."""
        return await asyncio.to_thread(self._check)

    def _check(self) -> list[str]:
        since = (self.clock() - LOOK_BACK).date()
        try:
            imap, incoming = fetch(self.settings, since)
        except (OSError, imaplib.IMAP4.abort) as e:  # Gmail timed out or dropped the line
            raise RetryableError(f"inbox not reachable right now: {e}") from e
        done: list[str] = []
        handled: list[str] = []
        self.ignored = []
        try:
            for mail in incoming:
                key = DONE + sha1(mail.message_id)[:16]
                if self.repo.get_setting(key):
                    handled.append(mail.uid)
                    continue
                outcome = self.apply(mail)
                if outcome is None:
                    continue  # not a genuine button email: left alone in the inbox
                self.repo.set_setting(key, outcome)
                handled.append(mail.uid)
                done.append(outcome)
            if handled:
                archive(imap, handled)
            if self.companies is not None:
                try:
                    done += self.companies.scan(imap)
                except (OSError, imaplib.IMAP4.error) as e:  # the buttons still count
                    log.warning("company replies not read this time", extra={"error": str(e)})
        finally:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                imap.logout()
        if self.ignored:
            self._tell_ignored(self.ignored)
        return done

    def _tell_ignored(self, ignored: list[tuple[Job, str]]) -> None:
        """Email you about button emails that changed nothing, so it isn't a silent no."""
        from jobradar.sinks.email_notify import PREVIEW_DIR, save_preview, smtp_send

        secrets = self.settings.secrets
        sender = secrets.email_address or "jobradar@localhost"
        msg = build_ignored(ignored, sender, secrets.notify_address or sender,
                            dashboard_url(self.settings))  # fmt: skip
        try:
            if self.settings.config.notify.dry_run:
                save_preview(msg, (self.preview_dir or PREVIEW_DIR)
                             / f"ignored-{self.clock():%Y%m%d-%H%M%S}.html")  # fmt: skip
            else:
                smtp_send(self.settings, msg)
        except OSError as e:  # the status is right either way; this is only a note
            log.warning("couldn't send the ignored-button note", extra={"error": str(e)})

    def apply(self, mail: Incoming) -> str | None:
        parsed = self.actions.parse(mail.subject)
        if parsed is None:
            return None
        if not self.actions.trusted(mail.sender, mail.auth_results):
            log.warning("button email not from you; ignored", extra={"from": mail.sender[:80]})
            return None

        # Replay prevention check
        sig_key = f"email_action_used:{parsed.signature}"
        if self.repo.get_setting(sig_key):
            log.warning(
                "email action token already used (replay attack prevented)",
                extra={"sig": parsed.signature},
            )
            return f"{parsed.action}: already used"

        action, prefix = parsed.action, parsed.prefix
        matches = self.repo.jobs_with_prefix(prefix)
        job = self.repo.get_job(matches[0]) if len(matches) == 1 else None
        if job is None:
            return f"{action}: job {prefix} not found"
        title = _title(job)
        day = (mail.sent or self.clock()).astimezone(self.tz).date()

        # Candidate-bound action
        if parsed.candidate_id:
            cand = self.repo.get_candidate(parsed.candidate_id)
            if cand is None:
                log.warning(
                    "candidate not found for action token",
                    extra={"candidate_id": parsed.candidate_id},
                )
                return f"{action}: candidate {parsed.candidate_id} not found"

            if action == "followed up":
                day_iso = day.isoformat()
                self.repo.set_setting(f"followed_up:{cand.id}:{job.id}", day_iso)
                self.repo.set_setting(FOLLOWED_UP + job.id, day_iso)
            else:
                applied_at = day.isoformat() if action == "applied" else None
                try:
                    self.repo.apply_candidate_action(
                        candidate_id=cand.id,
                        job_id=job.id,
                        action=action,
                        applied_at=applied_at,
                    )
                except Exception as e:
                    log.warning(
                        "failed to apply candidate action",
                        extra={"error": str(e), "action": action},
                    )
                    return f"{action}: error applying action"

            self.repo.set_setting(sig_key, to_iso(self.clock()))
            log.info(
                "applied candidate action from email",
                extra={"candidate_id": cand.id, "job_id": job.id, "action": action},
            )
            return f"{action}: {title}"

        # Legacy fallback
        outcome = rules.act(self.repo, self.queue, job, action, day, build_resumes=False)
        if not outcome.ok:
            about = {"job_id": job.id[:10], "action": action, "status": job.status}
            log.info("button email ignored: status has moved on", extra=about)
            self.ignored.append((job, action))
            return f"{action} ignored for {title}: it's {job.status} now"
        self.repo.set_setting(sig_key, to_iso(self.clock()))
        log.info("your email action", extra={"job_id": job.id[:10], "action": action})
        return f"{action}: {title}"
