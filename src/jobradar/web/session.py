"""Candidate session signing, cookie management, and session-bound CSRF validation.

Enforces candidate isolation to prevent Insecure Direct Object References (IDOR).
Uses HMAC-SHA256 signatures with constant-time verification.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from jobradar.config import Settings
    from jobradar.db.models import CandidateProfile
    from jobradar.db.repo import Repo

CANDIDATE_SESSION_COOKIE = "jobradar_candidate_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days


def sign_candidate_session(candidate_id: str, secret: str) -> str:
    """Produce a cryptographically signed session token for candidate_id."""
    signature = hmac.new(
        secret.encode("utf-8"), candidate_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    return f"{candidate_id}.{signature}"


def parse_candidate_session(token: str | None, secret: str) -> str | None:
    """Verify session token signature and extract candidate_id.

    Returns candidate_id if signature is valid, None otherwise.
    """
    if not token or "." not in token:
        return None

    candidate_id, _, signature = token.rpartition(".")
    if not candidate_id or not signature:
        return None

    expected_sig = hmac.new(
        secret.encode("utf-8"), candidate_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]

    if hmac.compare_digest(signature.encode("utf-8"), expected_sig.encode("utf-8")):
        return candidate_id

    return None


def get_session_candidate_id(request: Request, secret: str) -> str | None:
    """Retrieve verified candidate_id from request cookies."""
    token = request.cookies.get(CANDIDATE_SESSION_COOKIE)
    return parse_candidate_session(token, secret)


def set_candidate_cookie(
    response: Response, candidate_id: str, secret: str, secure: bool = False
) -> None:
    """Set the candidate session cookie with security flags."""
    token = sign_candidate_session(candidate_id, secret)
    response.set_cookie(
        key=CANDIDATE_SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_candidate_cookie(response: Response) -> None:
    """Clear candidate session cookie."""
    response.delete_cookie(key=CANDIDATE_SESSION_COOKIE, path="/")


def candidate_csrf_token(candidate_id: str, secret: str) -> str:
    """Generate a CSRF token bound to a specific candidate session."""
    message = f"candidate_csrf:{candidate_id}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()[
        :32
    ]


def validate_candidate_csrf(candidate_id: str, token_sent: str | None, secret: str) -> bool:
    """Validate CSRF token strictly bound to a candidate session."""
    if not token_sent:
        return False

    expected = candidate_csrf_token(candidate_id, secret)
    return hmac.compare_digest(token_sent.strip().encode("utf-8"), expected.strip().encode("utf-8"))


def validate_onboarding_page_csrf(token_sent: str | None, page_token: str) -> bool:
    """Validate onboarding page token for unauthenticated initial onboarding."""
    if not token_sent or not page_token:
        return False
    return hmac.compare_digest(
        token_sent.strip().encode("utf-8"), page_token.strip().encode("utf-8")
    )


def verify_profile_ownership(candidate_id: str, profile: CandidateProfile) -> bool:
    """Ensure that the given candidate owns the profile (IDOR defense)."""
    return profile.candidate_id == candidate_id


@dataclass(frozen=True)
class CandidateContext:
    """Explicit resolved candidate identity for request or task processing.

    Enforces strict candidate isolation:
    - candidate_id: The verified candidate ID.
    - is_owner: Whether this candidate is verified as the workspace owner.
    - source: Provenance of resolution ("session", "owner_settings", "explicit").
    """

    candidate_id: str
    is_owner: bool = False
    source: str = "session"


def resolve_candidate_context(
    request: Request | None,
    secret: str,
    repo: Repo,
    settings: Settings,
    profile: dict[str, Any] | None = None,
    allow_owner_fallback: bool = False,
    strict: bool = True,
) -> CandidateContext | None:
    """Resolve candidate context adhering strictly to identity resolution policy:

    1. If request has a valid, cryptographically verified candidate session cookie:
       - Verify candidate exists in repository.
       - If valid, check if this candidate matches the configured workspace owner.
       - Return CandidateContext(candidate_id, is_owner, source="session").
       - If invalid signature/tampered/nonexistent: fail safely (return None).
    2. If no candidate session and allow_owner_fallback is True:
       - Resolve owner candidate safely via repo.resolve_owner_candidate_from_settings().
       - If owner identity is missing or sources conflict: raise OwnerResolutionError (if strict)
         or return None (if not strict).
    3. Never silently fall back to an arbitrary candidate ID.
    """
    from jobradar.db.repo import OwnerResolutionError

    if request is not None:
        cand_id = get_session_candidate_id(request, secret)
        if cand_id:
            cand = repo.get_candidate(cand_id)
            if cand is not None:
                is_owner = False
                try:
                    owner_id = repo.resolve_owner_candidate_id_from_settings(settings, profile)
                    is_owner = cand_id == owner_id
                except OwnerResolutionError:
                    is_owner = False
                return CandidateContext(candidate_id=cand.id, is_owner=is_owner, source="session")
            return None

    if allow_owner_fallback:
        try:
            owner = repo.resolve_owner_candidate_from_settings(settings, profile)
            return CandidateContext(candidate_id=owner.id, is_owner=True, source="owner_settings")
        except OwnerResolutionError:
            if strict:
                raise
            return None

    return None
