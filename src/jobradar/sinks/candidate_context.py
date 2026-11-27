"""Candidate context resolution for notification sinks and background workers.

Ensures sinks bind safely to the workspace owner or explicit candidate identity,
enforcing candidate isolation and confirmed profile verification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jobradar.db.repo import OwnerResolutionError

if TYPE_CHECKING:
    from jobradar.config import Settings
    from jobradar.db.models import Candidate, CandidateProfile
    from jobradar.db.repo import Repo


def resolve_sink_candidate(
    repo: Repo,
    settings: Settings,
    candidate_id: str | None = None,
    profile_data: dict[str, Any] | None = None,
) -> tuple[Candidate | None, CandidateProfile | None]:
    """Resolve the candidate and active confirmed profile for notifications.

    Returns (Candidate, CandidateProfile) if verified and confirmed, or (None, None).
    """
    if candidate_id:
        cand = repo.get_candidate(candidate_id)
        if cand:
            active_profile = repo.get_active_profile(candidate_id)
            if active_profile and active_profile.review_status == "confirmed":
                return cand, active_profile
            return cand, None
        return None, None

    try:
        cand = repo.resolve_owner_candidate_from_settings(settings, profile_data)
        active_profile = repo.get_active_profile(cand.id)
        if active_profile and active_profile.review_status == "confirmed":
            return cand, active_profile
        return cand, None
    except OwnerResolutionError:
        return None, None
