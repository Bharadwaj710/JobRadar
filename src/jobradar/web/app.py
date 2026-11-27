"""The dashboard's web app: pages for today's matches, your applications, every job, and one
job, read from JobRadar's database. Read-only for now: statuses still change from the email
buttons or Notion.

It runs on this computer only. A browser sends requests to 127.0.0.1 from any site you have
open, so requests naming any other host are refused (that stops a site posing as localhost,
"DNS rebinding"), and pages may not be framed or load anything from elsewhere.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from jobradar.clock import Clock, utcnow
from jobradar.config import ConfigError, Settings
from jobradar.db.models import (
    Candidate,
    CandidateApplicationStatus,
    CandidateJobAssessment,
    Job,
)
from jobradar.db.repo import OwnerResolutionError, Repo
from jobradar.pipeline import status_rules as rules
from jobradar.pipeline.task_types import BUILD_RESUME
from jobradar.pipeline.technical_match import track_profile_version
from jobradar.profile import add_skill, load_profile
from jobradar.queue.tasks import TaskQueue
from jobradar.resume.extract import ExtractionError, extract_resume_content
from jobradar.resume.parser import parse_resume_to_draft
from jobradar.services.matching_service import CandidateMatchingService
from jobradar.sinks.digest import build_digest, gather
from jobradar.web import views
from jobradar.web.session import (
    candidate_csrf_token,
    get_session_candidate_id,
    resolve_candidate_context,
    set_candidate_cookie,
    validate_candidate_csrf,
    validate_onboarding_page_csrf,
    verify_profile_ownership,
)

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
MAX_MULTIPART_REQUEST_SIZE = 6 * 1024 * 1024  # 6 MB bounded request ceiling


EMAIL_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def asset(path: str) -> str:
    """'/static/app.js?v=3f9c1a2b7d': a new version of the file gets a new address, so the
    browser never keeps running an old one after an update."""
    digest = hashlib.sha256((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"


def allowed_hosts(host: str, url: str | None = None) -> list[str]:
    """Host names a request may carry: this computer's own, the configured one, and the
    name in `web.url` (your Tailscale address, for the phone)."""
    names = {"127.0.0.1", "localhost", "[::1]", host}
    if url and (name := urlsplit(url).hostname):
        names.add(name)
    return sorted(names)


TOKEN_SETTING = "web_token"  # noqa: S105 (the setting's name, not a secret)


def page_token(repo: Repo) -> str:
    """The secret every button on the dashboard sends back. Only JobRadar's own pages carry
    it, so another site open in your browser can't change your jobs (cross-site requests)."""
    token = repo.get_setting(TOKEN_SETTING)
    if not token:
        token = secrets.token_urlsafe(24)
        repo.set_setting(TOKEN_SETTING, token)
    return token


def safe_next(path: str | None) -> str:
    """Where to go back to after a form post: a path on this dashboard, never another site."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


def create_app(
    settings: Settings,
    repo: Repo,
    clock: Clock = utcnow,
    queue: TaskQueue | None = None,
    wake: Callable[[], None] | None = None,
) -> FastAPI:
    config = settings.config
    tz = config.notify.tz
    queue = queue or TaskQueue(repo, clock)
    nudge = wake or (lambda: None)  # inside `jobradar run`: start queued work now
    token = page_token(repo)
    hosts = allowed_hosts(config.web.host, config.web.url)
    app = FastAPI(title="JobRadar", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def _headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        email_csp = response.headers.get("Content-Security-Policy") == EMAIL_CSP
        response.headers.update(HEADERS)
        if email_csp:
            response.headers["Content-Security-Policy"] = EMAIL_CSP
        if request.url.path.startswith("/files/") and response.status_code == 200:
            # A PDF, shown by the browser's own viewer: the page policy can stop that viewer.
            del response.headers["Content-Security-Policy"]
            del response.headers["X-Frame-Options"]
        static = request.url.path.startswith("/static/")
        # Static files are versioned by asset(): cache them, but check back before reuse.
        response.headers["Cache-Control"] = "no-cache" if static else "no-store"
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    good = config.scoring.alert_above
    templates.env.globals.update(
        good=good, fair=config.scoring.hide_below, asset=asset, token=token,
        statuses=list(rules.YOURS.items()), notion=config.notion.enabled,
        initials=views.initials, hue=views.hue,
    )  # fmt: skip
    cache: dict[str, Any] = {"mtime": None, "profile": None}

    def current_profile() -> dict[str, Any] | None:
        """profile.yaml, read again whenever the file changes (e.g. "I know this")."""
        path = config.matching.profile_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        if cache["mtime"] != mtime:
            cache["mtime"], cache["profile"] = mtime, your_profile(settings)
        return cache["profile"]  # type: ignore[no-any-return]

    def current_track_version() -> str | None:
        profile = current_profile()
        return track_profile_version(profile, clock().astimezone(tz).date()) if profile else None

    profile = current_profile()
    basics = profile.get("basics") if profile else None
    name = str(basics.get("name") or "") if isinstance(basics, dict) else ""

    def resolve_owner(strict: bool = False) -> Candidate | None:
        """Resolve workspace owner candidate safely using configured identity."""
        if strict:
            return repo.resolve_owner_candidate_from_settings(settings, current_profile())
        try:
            return repo.resolve_owner_candidate_from_settings(settings, current_profile())
        except OwnerResolutionError as e:
            log.debug("Legacy owner candidate not resolved: %s", e)
            return None

    app.state.resolve_owner = resolve_owner

    def render(
        request: Request, page: str, nav: str, status_code: int = 200, **context: object
    ) -> HTMLResponse:
        now = clock()
        ov = views.overview(repo, good, now, tz, name)
        return templates.TemplateResponse(
            request, page, {"nav": nav, "ov": ov, "now": now, **context}, status_code
        )

    @app.exception_handler(StarletteHTTPException)
    async def _not_found(request: Request, exc: StarletteHTTPException) -> Response:
        return render(request, "error.html", "", exc.status_code, status=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> Response:
        return render(request, "error.html", "", 400, status=400)

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request) -> Response:
        cand_id = get_session_candidate_id(request, token)
        if cand_id and repo.get_candidate(cand_id):
            return RedirectResponse("/candidate/dashboard", status_code=303)
        now = clock()
        cand_ctx = resolve_candidate_context(
            request,
            token,
            repo,
            settings,
            current_profile(),
            allow_owner_fallback=True,
            strict=False,
        )
        resolved_cand_id = cand_ctx.candidate_id if cand_ctx else None
        cards = views.today(repo, good, now, tz, candidate_id=resolved_cand_id)
        owner_candidate = resolve_owner()
        return render(
            request, "today.html", "today", cards=cards, days=views.TODAY_DAYS,
            greeting=views.greeting(now, tz, name),
            says=views.blip_says(cards, good),
            pulse=views.pulse(repo, good, now, tz),
            sources=views.sources(repo, good, now),
            filters=views.QUICK_FILTERS,
            candidate=owner_candidate,
        )  # fmt: skip

    @app.get("/applications", response_class=HTMLResponse)
    def applications(request: Request) -> HTMLResponse:
        cand_ctx = resolve_candidate_context(
            request,
            token,
            repo,
            settings,
            current_profile(),
            allow_owner_fallback=True,
            strict=False,
        )
        resolved_cand_id = cand_ctx.candidate_id if cand_ctx else None
        columns = views.board(repo, settings, clock(), candidate_id=resolved_cand_id)
        source_strip_data = views.source_strip(repo, tz, candidate_id=resolved_cand_id)
        return render(
            request,
            "applications.html",
            "applications",
            columns=columns,
            quiet_after=config.notify.followup_after_days,
            funnel=views.funnel(repo, good),
            by_source=source_strip_data,
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(
        request: Request,
        show: str = "jobs",
        q: str = "",
        page: int = 1,
        sort: str = "",
        min: int = 0,
        within: int = 0,
        place: str = "",
        remote: str = "",
        source: int = 0,
        track: str = "",
        eligibility: str = "",
        technical: int = 0,
        mode: str = "",
        published: int = 0,
        priority: str = "",
    ) -> HTMLResponse:
        filters = views.Filters(
            min_score=min if min in dict(views.MIN_SCORES) and min else None,
            within=within if within in dict(views.WITHIN) and within else None,
            place=place.strip()[:60],
            remote=bool(remote),
            source=source or None,
            track=track if track in dict(views.TRACK_CHOICES) else "",
            eligibility=(
                eligibility.upper()
                if eligibility.upper() in dict(views.ELIGIBILITY_CHOICES)
                else ("eligible" if eligibility.lower() == "eligible" else "")
            ),
            technical_min=(
                technical if technical in dict(views.TECHNICAL_SCORES) and technical else None
            ),
            work_mode=mode if mode in dict(views.WORK_MODES) else "",
            published_within=(
                published if published in dict(views.PUBLISHED_WITHIN) and published else None
            ),
            priority=priority if priority in dict(views.PRIORITY_CHOICES) else "",
        )
        cand_ctx = resolve_candidate_context(
            request,
            token,
            repo,
            settings,
            current_profile(),
            allow_owner_fallback=True,
            strict=False,
        )
        resolved_cand_id = cand_ctx.candidate_id if cand_ctx else None
        found = views.all_jobs(
            repo,
            tz,
            show,
            q,
            page,
            clock(),
            sort or "",
            filters,
            current_track_version(),
            candidate_id=resolved_cand_id,
        )
        return render(request, "jobs.html", "jobs", found=found, choices=views.SHOW_CHOICES,
                      sorts=views.SORTS, min_scores=views.MIN_SCORES, withins=views.WITHIN,
                       sources=repo.sources(), track_choices=views.TRACK_CHOICES,
                       eligibility_choices=views.ELIGIBILITY_CHOICES,
                       technical_scores=views.TECHNICAL_SCORES, work_modes=views.WORK_MODES,
                       published_withins=views.PUBLISHED_WITHIN,
                       priority_choices=views.PRIORITY_CHOICES)  # fmt: skip

    @app.get("/digest", response_class=HTMLResponse)
    def digest(request: Request, w: str = "") -> HTMLResponse:
        page = views.digest(repo, settings, clock(), w, current_profile())
        return render(request, "digest.html", "digest", d=page)

    @app.get("/digest/email", response_class=HTMLResponse)
    def digest_email(w: str = "") -> HTMLResponse:
        """The digest exactly as the email looks. Email HTML is styled inline, so this one
        page may use inline styles; it still runs no script and loads nothing."""
        notify = config.notify
        local = clock().astimezone(tz)
        options = views.windows(local, notify.digest_times)
        window = next((x for x in options if x.key == w), options[1])
        data = gather(repo, settings, window.since, window.until, bounded=not window.current)
        sender = settings.secrets.email_address or "jobradar@localhost"
        msg = build_digest(data, sender, sender, settings.secrets.notion_database_id,
                           notion=config.notion.enabled)  # fmt: skip
        part = msg.get_body(("html",))
        html = str(part.get_content()) if part is not None else "<p>No HTML part.</p>"
        return HTMLResponse(html, headers={"Content-Security-Policy": EMAIL_CSP})

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: str) -> HTMLResponse:
        cand_ctx = resolve_candidate_context(
            request,
            token,
            repo,
            settings,
            current_profile(),
            allow_owner_fallback=True,
            strict=False,
        )
        resolved_cand_id = cand_ctx.candidate_id if cand_ctx else None
        detail = views.job_detail(
            repo,
            job_id,
            tz,
            clock(),
            config.matching.weights,
            current_profile(),
            settings,
            current_track_version(),
            candidate_id=resolved_cand_id,
        )
        if detail is None:
            raise HTTPException(status_code=404)
        return render(request, "job.html", "", job=detail)

    # ---- changing a status ----------------------------------------------------------------

    async def checked_form(request: Request) -> dict[str, str]:
        """The posted fields, after checking the request came from one of our pages."""
        origin = request.headers.get("origin")
        if origin and origin != "null":
            orig_host = urlsplit(origin).hostname
            if orig_host and orig_host not in {h.strip("[]") for h in hosts}:
                raise HTTPException(status_code=403)
        body = (await request.body())[:4096].decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        sent = (request.headers.get("x-jobradar-token") or fields.get("token", "")).strip()
        if not hmac.compare_digest(sent.encode(), token.encode()):
            raise HTTPException(status_code=403)
        return fields

    def answer(
        request: Request,
        outcome: rules.Outcome,
        job_id: str,
        back: str,
        candidate_id: str | None = None,
    ) -> Response:
        if request.headers.get("x-jobradar-token"):  # the page's own script: it updates itself
            undo = outcome.before if outcome.before in {s.value for s in rules.YOURS} else (
                "shortlisted" if outcome.before == "resume_ready" else "new")  # fmt: skip
            job = repo.get_job(job_id)
            row = ""
            if job is not None and outcome.ok:
                track = views.track_card(
                    repo.track_matches_for_job(job.id), current_track_version()
                )
                cand_app = (
                    repo.get_candidate_application(candidate_id, job.id) if candidate_id else None
                )
                cand_a = None
                hist_a = (
                    repo.get_historical_assessment(candidate_id, job.id) if candidate_id else None
                )
                if candidate_id:
                    cand_assessments = repo.list_candidate_assessments(candidate_id, job_id=job.id)
                    cand_a = cand_assessments[0] if cand_assessments else None
                row = templates.get_template("_row.html").render(
                    c=views.card(
                        job,
                        tz,
                        clock(),
                        track,
                        application=cand_app,
                        candidate_assessment=cand_a,
                        historical_assessment=hist_a,
                    ),
                    with_status=True,
                )
            return JSONResponse(
                {"ok": outcome.ok, "message": outcome.message, "status": outcome.after,
                 "label": views.candidate_label(outcome.after),
                 "undo": undo if outcome.ok and outcome.before != outcome.after else None,
                 "row": row},
                status_code=200 if outcome.ok else 409,
            )  # fmt: skip
        return RedirectResponse(safe_next(back), status_code=303)

    @app.post("/jobs/{job_id}/act")
    async def act(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404)

        action = fields.get("action", "").strip()
        if not action:
            raise HTTPException(status_code=400, detail="Action is required.")

        notes = fields.get("notes", "").strip()
        applied_at = fields.get("applied_at", None)

        cand_ctx = resolve_candidate_context(
            request,
            token,
            repo,
            settings,
            current_profile(),
            allow_owner_fallback=True,
            strict=False,
        )
        if cand_ctx is not None:
            try:
                app_record = repo.apply_candidate_action(
                    candidate_id=cand_ctx.candidate_id,
                    job_id=job_id,
                    action=action,
                    notes=notes,
                    applied_at=applied_at,
                )
            except ValueError as e:
                return JSONResponse({"ok": False, "message": str(e)}, status_code=400)

            nudge()
            log.info(
                "candidate dashboard action",
                extra={
                    "candidate_id": cand_ctx.candidate_id,
                    "job_id": job_id[:10],
                    "action": action,
                    "status": app_record.status,
                },
            )

            if (
                config.resume.enabled
                and app_record.status == CandidateApplicationStatus.SAVED.value
                and repo.latest_artifact(job.id) is None
            ):
                queue.enqueue(BUILD_RESUME, job.id, {"why": "shortlisted"})

            outcome = rules.Outcome(
                ok=True,
                action=action,
                before=app_record.status,
                after=app_record.status,
                message=f"{views.candidate_label(app_record.status)}: {job.role or job.id}",
            )
            return answer(
                request, outcome, job_id, fields.get("next", ""), candidate_id=cand_ctx.candidate_id
            )

        # Legacy fallback when no candidate is configured
        today = clock().astimezone(tz).date()
        outcome = rules.act(repo, queue, job, action, today)
        nudge()
        log.info(
            "your dashboard action",
            extra={"job_id": job_id[:10], "ok": outcome.ok, "action": outcome.action},
        )
        return answer(request, outcome, job_id, fields.get("next", ""))

    @app.post("/jobs/{job_id}/status")
    async def set_status(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404)

        status_str = fields.get("status", "").strip()
        if not status_str:
            raise HTTPException(status_code=400, detail="Status is required.")

        notes = fields.get("notes", "").strip()

        cand_ctx = resolve_candidate_context(
            request,
            token,
            repo,
            settings,
            current_profile(),
            allow_owner_fallback=True,
            strict=False,
        )
        if cand_ctx is not None:
            try:
                app_record = repo.apply_candidate_action(
                    candidate_id=cand_ctx.candidate_id,
                    job_id=job_id,
                    action=status_str,
                    notes=notes,
                )
            except ValueError as e:
                return JSONResponse({"ok": False, "message": str(e)}, status_code=400)

            nudge()
            log.info(
                "candidate dashboard status",
                extra={
                    "candidate_id": cand_ctx.candidate_id,
                    "job_id": job_id[:10],
                    "status": app_record.status,
                },
            )

            outcome = rules.Outcome(
                ok=True,
                action="set",
                before=app_record.status,
                after=app_record.status,
                message=f"Now {views.candidate_label(app_record.status)}: {job.role or job.id}",
            )
            return answer(
                request, outcome, job_id, fields.get("next", ""), candidate_id=cand_ctx.candidate_id
            )

        # Legacy fallback when no candidate is configured
        outcome = rules.set_status(repo, queue, job, status_str)
        nudge()
        log.info("your dashboard status", extra={"job_id": job_id[:10], "status": outcome.after})
        return answer(request, outcome, job_id, fields.get("next", ""))

    # ---- resumes and cover letters ---------------------------------------------------------

    @app.get("/files/{artifact_id}")
    def file(artifact_id: int, cover: int = 0) -> FileResponse:
        """A resume (or the cover letter beside it) by its id in the database. The path comes
        from the database, never the address, and must be a PDF inside storage.local_dir."""
        artifact = repo.get_artifact(artifact_id)
        path = views.artifact_file(settings, artifact, bool(cover)) if artifact else None
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="application/pdf",
                            content_disposition_type="inline", filename=path.name)  # fmt: skip

    @app.post("/jobs/{job_id}/resume")
    async def build_resume(request: Request, job_id: str) -> Response:
        fields = await checked_form(request)
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        if not config.resume.enabled:
            message, ok = "Resumes are off (resume.enabled in config.yaml).", False
        else:
            queue.enqueue(BUILD_RESUME, job.id, {"why": "manual"})
            nudge()
            message, ok = f"Building a resume for {views.card(job, tz).role}…", True
        log.info("your dashboard resume", extra={"job_id": job_id[:10], "ok": ok})
        if request.headers.get("x-jobradar-token"):
            return JSONResponse({"ok": ok, "message": message,
                                 "count": len(repo.artifacts_of(job.id))},
                                status_code=200 if ok else 409)  # fmt: skip
        return RedirectResponse(safe_next(fields.get("next")), status_code=303)

    @app.get("/api/jobs/{job_id}/resumes")
    def resumes(job_id: str) -> JSONResponse:
        """How many resumes a job has and whether one is being built: the page polls this."""
        return JSONResponse({
            "count": len(repo.artifacts_of(job_id)),
            "building": repo.task_waiting(BUILD_RESUME, job_id),
        })  # fmt: skip

    # ---- skills ------------------------------------------------------------------------------

    @app.get("/skills", response_class=HTMLResponse)
    def skills(request: Request, days: int = 30) -> HTMLResponse:
        cand_id = get_session_candidate_id(request, token)
        if cand_id:
            candidate = repo.get_candidate(cand_id)
            if candidate:
                active_prof = repo.get_active_profile(cand_id)
                c_csrf = candidate_csrf_token(cand_id, token)
                if not active_prof:
                    return render(
                        request,
                        "skills.html",
                        "skills",
                        s=views.SkillsPage(
                            days=days,
                            looked_at=0,
                            min_score=config.scoring.alert_above,
                            missing=[],
                            have=[],
                            warnings=[
                                "You do not have a confirmed candidate profile yet. "
                                "Please upload and confirm your resume in Profile to see "
                                "personalized skill gaps."
                            ],
                        ),
                        candidate=candidate,
                        needs_onboarding=True,
                        csrf_token=c_csrf,
                    )
                cand_profile_dict = active_prof.profile_data
                page = views.skills_page(repo, settings, cand_profile_dict, clock(), days)
                return render(
                    request,
                    "skills.html",
                    "skills",
                    s=page,
                    candidate=candidate,
                    active_profile=active_prof,
                    csrf_token=c_csrf,
                )
        owner_candidate = resolve_owner()
        page = views.skills_page(repo, settings, current_profile(), clock(), days)
        return render(request, "skills.html", "skills", s=page, candidate=owner_candidate)

    @app.get("/skills/{key}", response_class=HTMLResponse)
    def skill(request: Request, key: str, days: int = 30) -> Response:
        cand_id = get_session_candidate_id(request, token)
        if cand_id:
            candidate = repo.get_candidate(cand_id)
            if candidate:
                active_prof = repo.get_active_profile(cand_id)
                if not active_prof:
                    return RedirectResponse("/onboarding", status_code=303)
                c_csrf = candidate_csrf_token(cand_id, token)
                cand_profile_dict = active_prof.profile_data
                page = views.skill_detail_page(
                    repo, settings, cand_profile_dict, clock(), days, key
                )
                if page is None:
                    raise HTTPException(status_code=404)
                return render(
                    request,
                    "skill.html",
                    "skills",
                    s=page,
                    candidate=candidate,
                    active_profile=active_prof,
                    csrf_token=c_csrf,
                )
        owner_candidate = resolve_owner()
        page = views.skill_detail_page(repo, settings, current_profile(), clock(), days, key)
        if page is None:
            raise HTTPException(status_code=404)
        return render(request, "skill.html", "skills", s=page, candidate=owner_candidate)

    @app.post("/skills/know")
    async def know(request: Request) -> Response:
        """ "I know this": add a skill to profile, so matching counts it."""
        origin = request.headers.get("origin")
        if origin and origin != "null":
            orig_host = urlsplit(origin).hostname
            if orig_host and orig_host not in {h.strip("[]") for h in hosts}:
                raise HTTPException(status_code=403)
        body = (await request.body())[:4096].decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        sent_token = (request.headers.get("x-jobradar-token") or fields.get("token", "")).strip()

        cand_id = get_session_candidate_id(request, token)
        candidate = repo.get_candidate(cand_id) if cand_id else None

        if cand_id and candidate:
            if not (
                validate_candidate_csrf(cand_id, sent_token, token)
                or validate_onboarding_page_csrf(sent_token, token)
            ):
                raise HTTPException(status_code=403, detail="Invalid CSRF token")

            name = fields.get("skill", "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="Skill name is required")

            active_prof = repo.get_active_profile(cand_id)
            if not active_prof:
                return JSONResponse(
                    {
                        "ok": False,
                        "message": "No confirmed profile found. Complete onboarding first.",
                    },
                    status_code=400,
                )

            profile_data = dict(active_prof.profile_data)
            raw_skills = profile_data.get("skills")
            if isinstance(raw_skills, list):
                skills_list = list(raw_skills)
            elif isinstance(raw_skills, dict):
                skills_list = list(raw_skills.get("other", []))
            else:
                skills_list = []

            if not any(s.casefold() == name.casefold() for s in skills_list):
                skills_list.append(name)
                if isinstance(raw_skills, dict):
                    raw_skills["other"] = skills_list
                    profile_data["skills"] = raw_skills
                else:
                    profile_data["skills"] = skills_list

            new_profile = repo.confirm_profile(cand_id, active_prof.id, profile_data)

            with Session(repo.engine) as s:
                job_rows = list(
                    s.exec(select(Job).order_by(col(Job.created_at).desc()).limit(100)).all()
                )
            if job_rows:
                matcher = CandidateMatchingService(repo)
                matcher.assess_candidate_jobs(
                    candidate_id=cand_id,
                    jobs=job_rows,
                    profile_version=new_profile.version,
                )

            ok = True
            message = (
                f"Added {name} to your profile (Version {new_profile.version}). "
                "Match Radar updated."
            )
            log.info(
                "candidate profile skill added",
                extra={
                    "candidate_id": cand_id,
                    "skill": name[:60],
                    "version": new_profile.version,
                },
            )

            if request.headers.get("x-jobradar-token"):
                return JSONResponse({"ok": ok, "message": message}, status_code=200)
            return RedirectResponse(safe_next(fields.get("next")), status_code=303)

        if not hmac.compare_digest(sent_token.encode(), token.encode()):
            raise HTTPException(status_code=403)
        _ = resolve_owner()
        name = fields.get("skill", "")
        stamp = clock().astimezone(tz).strftime("%Y%m%d-%H%M%S")
        try:
            backup = add_skill(config.matching.profile_path, name, stamp)
        except ConfigError as e:
            ok, message = False, str(e)
        else:
            ok = True
            message = (
                f"Added {name} to your profile (backup: {backup}). Matching counts it "
                "from the next restart of jobradar run."
            )
            log.info("your dashboard skill", extra={"skill": name[:60]})
        if request.headers.get("x-jobradar-token"):
            return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)
        return RedirectResponse(safe_next(fields.get("next")), status_code=303)

    # ---- candidate onboarding & profile review -------------------------------------------

    @app.get("/onboarding", response_class=HTMLResponse)
    def onboarding(request: Request, error: str = "", confirmed: int = 0) -> HTMLResponse:
        cand_id = get_session_candidate_id(request, token)
        active_prof = repo.get_active_profile(cand_id) if cand_id else None
        csrf = candidate_csrf_token(cand_id, token) if cand_id else token
        return render(
            request,
            "onboarding.html",
            "onboarding",
            error=error,
            confirmed=bool(confirmed),
            active_profile=active_prof,
            csrf_token=csrf,
        )

    def _parse_multipart_upload(
        body_bytes: bytes, content_type: str
    ) -> tuple[bytes | None, str, dict[str, str]]:
        header = f"Content-Type: {content_type}\r\n\r\n".encode()
        msg = email.message_from_bytes(header + body_bytes, policy=email.policy.default)
        extracted_file: bytes | None = None
        extracted_filename = "resume.pdf"
        fields: dict[str, str] = {}
        if msg.is_multipart():
            for part in msg.iter_parts():
                fname = part.get_filename()
                field_name = part.get_param("name", header="content-disposition")
                if fname:
                    extracted_filename = fname
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        extracted_file = payload
                    elif isinstance(payload, str):
                        extracted_file = payload.encode("utf-8")
                elif field_name:
                    val = ""
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        val = payload.decode("utf-8", "replace")
                    elif isinstance(payload, str):
                        val = payload
                    elif payload is None:
                        raw_payload = part.get_payload()
                        if isinstance(raw_payload, str):
                            val = raw_payload
                        elif isinstance(raw_payload, bytes):
                            val = raw_payload.decode("utf-8", "replace")
                    fields[str(field_name)] = val.strip()
        return extracted_file, extracted_filename, fields

    @app.post("/onboarding/upload")
    async def onboarding_upload(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin and origin != "null":
            orig_host = urlsplit(origin).hostname
            if orig_host and orig_host not in {h.strip("[]") for h in hosts}:
                raise HTTPException(status_code=403)

        # 1. Early check on Content-Length header to reject oversized payloads immediately
        content_length_header = request.headers.get("content-length")
        if content_length_header:
            try:
                if int(content_length_header) > MAX_MULTIPART_REQUEST_SIZE:
                    raise HTTPException(status_code=413, detail="Payload too large")
            except ValueError as err:
                raise HTTPException(
                    status_code=400, detail="Invalid Content-Length header"
                ) from err

        # 2. Stream request body with strict byte accumulation limit to prevent memory exhaustion
        accumulated = bytearray()
        async for chunk in request.stream():
            accumulated.extend(chunk)
            if len(accumulated) > MAX_MULTIPART_REQUEST_SIZE:
                raise HTTPException(status_code=413, detail="Payload too large")
        body_bytes = bytes(accumulated)

        content_type = request.headers.get("content-type", "")
        file_bytes, filename, fields = _parse_multipart_upload(body_bytes, content_type)

        # 3. Enforce CSRF verification before any database record or state change
        sent_token = (request.headers.get("x-jobradar-token") or fields.get("token", "")).strip()
        cand_id = get_session_candidate_id(request, token)
        is_valid_csrf = validate_onboarding_page_csrf(sent_token, token) or (
            validate_candidate_csrf(cand_id, sent_token, token) if cand_id else False
        )
        if not is_valid_csrf:
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

        if not file_bytes:
            return RedirectResponse(
                "/onboarding?error=Please+select+a+valid+PDF+or+DOCX+file+to+upload.",
                status_code=303,
            )

        try:
            extracted = extract_resume_content(file_bytes, filename)
        except ExtractionError as e:
            return RedirectResponse(f"/onboarding?error={e}", status_code=303)

        draft = parse_resume_to_draft(
            extracted.raw_text, extracted_urls=list(extracted.extracted_urls)
        )
        draft_dict = draft.to_profile_dict()

        candidate = repo.get_candidate(cand_id) if cand_id else None
        if candidate is None:
            c_name = draft.personal.name or "New Candidate"
            c_email = draft.personal.email or f"candidate_{secrets.token_hex(4)}@local"
            candidate = repo.create_candidate(name=c_name, email=c_email)

        resume = repo.save_resume(
            candidate_id=candidate.id,
            filename=extracted.original_filename,
            file_type=extracted.file_type,
            raw_text=extracted.raw_text,
            extraction_status=extracted.status,
        )

        profile = repo.save_profile_draft(
            candidate_id=candidate.id,
            resume_id=resume.id,
            profile_data=draft_dict,
        )

        resp = RedirectResponse(f"/onboarding/review/{profile.id}", status_code=303)
        set_candidate_cookie(resp, candidate.id, token)
        return resp

    @app.get("/onboarding/manual")
    def onboarding_manual(request: Request) -> Response:
        cand_id = get_session_candidate_id(request, token)
        candidate = repo.get_candidate(cand_id) if cand_id else None
        if candidate is None:
            candidate = repo.create_candidate(
                name="Candidate",
                email=f"candidate_{secrets.token_hex(4)}@local",
            )
        blank_draft: dict[str, Any] = {
            "basics": {"name": "", "email": "", "phone": "", "location": ""},
            "education": [
                {
                    "degree": "",
                    "branch": "",
                    "institution": "",
                    "graduation_year": None,
                    "cgpa": "",
                }
            ],
            "skills": [],
            "experience": [],
            "projects": [],
            "evidence": {
                "snippets": {},
                "metadata": {"parser_used": "manual", "confidence": 1.0, "warnings": []},
            },
        }
        profile = repo.save_profile_draft(
            candidate_id=candidate.id,
            resume_id=None,
            profile_data=blank_draft,
        )
        resp = RedirectResponse(f"/onboarding/review/{profile.id}", status_code=303)
        set_candidate_cookie(resp, candidate.id, token)
        return resp

    @app.get("/onboarding/review/{profile_id}", response_class=HTMLResponse)
    def onboarding_review(request: Request, profile_id: str, confirmed: int = 0) -> HTMLResponse:
        cand_id = get_session_candidate_id(request, token)
        if not cand_id:
            raise HTTPException(status_code=401, detail="Candidate session required.")
        profile = repo.get_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        if not verify_profile_ownership(cand_id, profile):
            raise HTTPException(
                status_code=403, detail="Access denied: profile belongs to another candidate."
            )

        c_csrf = candidate_csrf_token(cand_id, token)
        return render(
            request,
            "profile_review.html",
            "onboarding",
            profile=profile,
            csrf_token=c_csrf,
            confirmed=bool(confirmed),
        )

    @app.post("/onboarding/confirm/{profile_id}")
    async def onboarding_confirm(request: Request, profile_id: str) -> Response:
        origin = request.headers.get("origin")
        if origin and origin != "null":
            orig_host = urlsplit(origin).hostname
            if orig_host and orig_host not in {h.strip("[]") for h in hosts}:
                raise HTTPException(status_code=403)

        cand_id = get_session_candidate_id(request, token)
        if not cand_id:
            raise HTTPException(status_code=401, detail="Candidate session required.")
        profile = repo.get_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        if not verify_profile_ownership(cand_id, profile):
            raise HTTPException(
                status_code=403, detail="Access denied: profile belongs to another candidate."
            )

        body = (await request.body())[:8192].decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        sent_token = (request.headers.get("x-jobradar-token") or fields.get("token", "")).strip()
        if not (
            validate_candidate_csrf(cand_id, sent_token, token)
            or validate_onboarding_page_csrf(sent_token, token)
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token.")

        current_data = dict(profile.profile_data)
        basics = dict(current_data.get("basics", {}))
        if "name" in fields:
            basics["name"] = fields["name"]
        if "email" in fields:
            basics["email"] = fields["email"]
        if "phone" in fields:
            basics["phone"] = fields["phone"]
        if "location" in fields:
            basics["location"] = fields["location"]
        if "github" in fields:
            basics["github"] = fields["github"]
        if "linkedin" in fields:
            basics["linkedin"] = fields["linkedin"]
        current_data["basics"] = basics

        edus = list(current_data.get("education", []))
        edu0 = dict(edus[0]) if edus else {}
        if "degree" in fields:
            edu0["degree"] = fields["degree"]
        if "branch" in fields:
            edu0["branch"] = fields["branch"]
        if "institution" in fields:
            edu0["institution"] = fields["institution"]
        if "graduation_year" in fields and fields["graduation_year"].strip().isdigit():
            edu0["graduation_year"] = int(fields["graduation_year"].strip())
        if "cgpa" in fields:
            edu0["cgpa"] = fields["cgpa"]
        current_data["education"] = [edu0]

        if "skills" in fields:
            skills_raw = fields["skills"]
            current_data["skills"] = [s.strip() for s in skills_raw.split(",") if s.strip()]

        confirmed_profile = repo.confirm_profile(profile_id, current_data)

        if request.headers.get("x-jobradar-token"):
            return JSONResponse(
                {
                    "ok": True,
                    "profile_id": confirmed_profile.id,
                    "version": confirmed_profile.version,
                    "status": confirmed_profile.status,
                }
            )

        return RedirectResponse(
            f"/onboarding/review/{confirmed_profile.id}?confirmed=1", status_code=303
        )

    # ---- candidate dashboard & tracking --------------------------------------------------

    @app.get("/candidate/dashboard", response_class=HTMLResponse)
    def candidate_dashboard(
        request: Request,
        track: str | None = None,
        eligibility: str | None = None,
        refreshed: int = 0,
    ) -> Response:
        cand_id = get_session_candidate_id(request, token)
        if not cand_id:
            return RedirectResponse("/onboarding", status_code=303)

        candidate = repo.get_candidate(cand_id)
        if candidate is None:
            return RedirectResponse("/onboarding", status_code=303)

        active_profile = repo.get_active_profile(cand_id)
        if active_profile is None or active_profile.review_status != "confirmed":
            profiles = repo.get_candidate_profiles(cand_id)
            draft_profile = profiles[0] if profiles else None
            return render(
                request,
                "candidate_dashboard.html",
                "candidate_dashboard",
                candidate=candidate,
                active_profile=None,
                draft_profile=draft_profile,
                stats={
                    "total": 0,
                    "eligible": 0,
                    "likely_eligible": 0,
                    "needs_verification": 0,
                    "not_eligible": 0,
                },
                app_counts={
                    "saved": 0,
                    "applied": 0,
                    "interviewing": 0,
                    "offered": 0,
                    "rejected": 0,
                    "total": 0,
                },
                recommendations=[],
                eligible_recs=[],
                verification_recs=[],
                ineligible_recs=[],
                selected_track=track or "",
                selected_eligibility=eligibility or "",
                csrf_token=candidate_csrf_token(cand_id, token),
                refreshed=bool(refreshed),
            )

        existing_assessments = repo.list_candidate_assessments(
            cand_id, profile_version=active_profile.version
        )
        if not existing_assessments:
            with Session(repo.engine) as s:
                available_jobs = list(
                    s.exec(select(Job).order_by(col(Job.created_at).desc()).limit(100)).all()
                )
            if available_jobs:
                matcher = CandidateMatchingService(repo)
                matcher.assess_candidate_jobs(
                    candidate_id=cand_id,
                    jobs=available_jobs,
                    profile_version=active_profile.version,
                )

        stats = repo.get_candidate_assessment_stats(
            cand_id, profile_version=active_profile.version, track=track
        )
        app_counts = repo.get_candidate_application_counts(cand_id)
        norm_elig = eligibility.lower() if eligibility else None
        recommendations = repo.get_candidate_recommendations(
            cand_id,
            profile_version=active_profile.version,
            track=track,
            eligibility=norm_elig,
        )
        eligible_recs = [
            r
            for r in recommendations
            if r.get("eligibility_status") in ("ELIGIBLE", "LIKELY_ELIGIBLE")
        ]
        verification_recs = [
            r for r in recommendations if r.get("eligibility_status") == "NEEDS_VERIFICATION"
        ]
        ineligible_recs = [
            r for r in recommendations if r.get("eligibility_status") == "NOT_ELIGIBLE"
        ]
        c_csrf = candidate_csrf_token(cand_id, token)

        return render(
            request,
            "candidate_dashboard.html",
            "candidate_dashboard",
            candidate=candidate,
            active_profile=active_profile,
            stats=stats,
            app_counts=app_counts,
            recommendations=recommendations,
            eligible_recs=eligible_recs,
            verification_recs=verification_recs,
            ineligible_recs=ineligible_recs,
            selected_track=track or "",
            selected_eligibility=norm_elig or "",
            csrf_token=c_csrf,
            refreshed=bool(refreshed),
        )

    @app.post("/candidate/refresh")
    async def candidate_refresh(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin and origin != "null":
            orig_host = urlsplit(origin).hostname
            if orig_host and orig_host not in {h.strip("[]") for h in hosts}:
                raise HTTPException(status_code=403)

        cand_id = get_session_candidate_id(request, token)
        if not cand_id:
            raise HTTPException(status_code=401, detail="Candidate session required.")

        body = (await request.body())[:8192].decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        sent_token = (request.headers.get("x-jobradar-token") or fields.get("token", "")).strip()
        if not (
            validate_candidate_csrf(cand_id, sent_token, token)
            or validate_onboarding_page_csrf(sent_token, token)
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token.")

        active_profile = repo.get_active_profile(cand_id)
        if active_profile is None or active_profile.review_status != "confirmed":
            raise HTTPException(
                status_code=400, detail="No active confirmed profile found to evaluate."
            )

        with Session(repo.engine) as s:
            available_jobs = list(
                s.exec(select(Job).order_by(col(Job.created_at).desc()).limit(100)).all()
            )

        if available_jobs:
            matcher = CandidateMatchingService(repo)
            matcher.assess_candidate_jobs(
                candidate_id=cand_id,
                jobs=available_jobs,
                profile_version=active_profile.version,
            )

        if (
            request.headers.get("x-jobradar-token")
            or request.headers.get("accept") == "application/json"
        ):
            return JSONResponse({"ok": True, "refreshed": True, "version": active_profile.version})

        return RedirectResponse("/candidate/dashboard?refreshed=1", status_code=303)

    @app.get("/candidate/jobs/{job_id}", response_class=HTMLResponse)
    def candidate_job_detail(request: Request, job_id: str) -> HTMLResponse:
        cand_id = get_session_candidate_id(request, token)
        if not cand_id:
            raise HTTPException(status_code=401, detail="Candidate session required.")

        candidate = repo.get_candidate(cand_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Candidate not found.")

        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        active_profile = repo.get_active_profile(cand_id)
        assessments: list[CandidateJobAssessment] = []
        if active_profile:
            assessments = repo.list_candidate_assessments(
                cand_id, job_id=job_id, profile_version=active_profile.version
            )
            if not assessments and active_profile.review_status == "confirmed":
                matcher = CandidateMatchingService(repo)
                assessments = matcher.assess_candidate_job(
                    candidate_id=cand_id, job=job, profile_version=active_profile.version
                )

        application = repo.get_candidate_application(cand_id, job_id)
        history = repo.list_candidate_application_history(cand_id, job_id=job_id)

        job_data: dict[str, Any] = {}
        if job.data_json:
            try:
                job_data = json.loads(job.data_json)
            except Exception:
                job_data = {}

        c_csrf = candidate_csrf_token(cand_id, token)

        return render(
            request,
            "candidate_job.html",
            "candidate_dashboard",
            candidate=candidate,
            active_profile=active_profile,
            job=job,
            job_data=job_data,
            assessments=assessments,
            application=application,
            history=history,
            csrf_token=c_csrf,
        )

    @app.post("/candidate/jobs/{job_id}/status")
    async def candidate_job_status_update(request: Request, job_id: str) -> Response:
        origin = request.headers.get("origin")
        if origin and origin != "null":
            orig_host = urlsplit(origin).hostname
            if orig_host and orig_host not in {h.strip("[]") for h in hosts}:
                raise HTTPException(status_code=403)

        cand_id = get_session_candidate_id(request, token)
        if not cand_id:
            raise HTTPException(status_code=401, detail="Candidate session required.")

        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        body = (await request.body())[:8192].decode("utf-8", "replace")
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        sent_token = (request.headers.get("x-jobradar-token") or fields.get("token", "")).strip()
        if not (
            validate_candidate_csrf(cand_id, sent_token, token)
            or validate_onboarding_page_csrf(sent_token, token)
        ):
            raise HTTPException(status_code=403, detail="Invalid CSRF token.")

        status = fields.get("status", "").strip().lower()
        notes = fields.get("notes", "").strip()

        allowed_statuses = {s.value for s in CandidateApplicationStatus}
        if status not in allowed_statuses:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{status}'. Must be one of: {sorted(allowed_statuses)}",
            )

        app_record = repo.save_candidate_application(
            candidate_id=cand_id,
            job_id=job_id,
            status=status,
            notes=notes,
        )

        if (
            request.headers.get("x-jobradar-token")
            or request.headers.get("accept") == "application/json"
        ):
            return JSONResponse(
                {
                    "ok": True,
                    "candidate_id": cand_id,
                    "job_id": job_id,
                    "status": app_record.status,
                    "applied_at": app_record.applied_at,
                    "notes": app_record.notes,
                }
            )

        next_url = safe_next(fields.get("next") or f"/candidate/jobs/{job_id}")
        return RedirectResponse(next_url, status_code=303)

    @app.get("/candidate/applications", response_class=HTMLResponse)
    def candidate_applications(request: Request) -> Response:
        cand_id = get_session_candidate_id(request, token)
        if not cand_id:
            return RedirectResponse("/onboarding", status_code=303)

        candidate = repo.get_candidate(cand_id)
        if candidate is None:
            return RedirectResponse("/onboarding", status_code=303)

        active_profile = repo.get_active_profile(cand_id)
        applications = repo.list_candidate_applications(cand_id)
        app_counts = repo.get_candidate_application_counts(cand_id)

        job_ids = [a.job_id for a in applications]
        jobs_map: dict[str, Job] = {}
        if job_ids:
            with Session(repo.engine) as s:
                stmt = select(Job).where(col(Job.id).in_(job_ids))
                jobs_map = {j.id: j for j in s.exec(stmt).all()}

        columns: dict[str, list[dict[str, Any]]] = {
            "saved": [],
            "applied": [],
            "interviewing": [],
            "offered": [],
            "rejected": [],
            "skipped": [],
        }

        for app_rec in applications:
            job = jobs_map.get(app_rec.job_id)
            job_data: dict[str, Any] = {}
            if job and job.data_json:
                try:
                    job_data = json.loads(job.data_json)
                except Exception:
                    job_data = {}

            item = {
                "application": app_rec,
                "job": job,
                "title": (
                    job_data.get("role")
                    or job_data.get("title")
                    or (job.role if job else "")
                    or "Software Engineer"
                ),
                "company": (job_data.get("company") or (job.company if job else "") or "Company"),
                "location": job_data.get("location") or "India / Remote",
                "apply_url": (
                    job_data.get("apply_url") or (job.canonical_url if job else "") or "#"
                ),
            }
            if app_rec.status in columns:
                columns[app_rec.status].append(item)

        c_csrf = candidate_csrf_token(cand_id, token)

        return render(
            request,
            "candidate_applications.html",
            "candidate_apps",
            candidate=candidate,
            active_profile=active_profile,
            columns=columns,
            app_counts=app_counts,
            csrf_token=c_csrf,
        )

    @app.get("/api/search")
    def api_search(q: str = "") -> JSONResponse:
        return JSONResponse({"jobs": views.search(repo, q)})

    return app


def your_profile(settings: Settings) -> dict[str, Any] | None:
    """profile.yaml, for Blip's greeting and the skills you have; None when it can't be read."""
    try:
        return load_profile(settings.config.matching.profile_path)
    except ConfigError:
        return None
