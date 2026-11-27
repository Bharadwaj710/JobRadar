"""Evidence-backed lead enrichment and application priority.

Technical fit and fresher eligibility stay in their J4 modules. This module records current
vacancy verification, public contact routes, unresolved application conditions, and truthful
drafts without submitting or sending anything.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from jobradar.pipeline.schemas import TrackName

VacancyStatus = Literal["active", "expired", "unknown"]
EligibilityStatus = Literal["ELIGIBLE", "LIKELY_ELIGIBLE", "NEEDS_VERIFICATION", "NOT_ELIGIBLE"]
ApplicationPriority = Literal["apply_now", "review", "low", "exclude"]
ContactKind = Literal[
    "recruiter",
    "talent_acquisition",
    "hiring_manager",
    "engineering_manager",
    "founder",
    "official_route",
]
ContactConfidence = Literal["high", "medium", "low"]


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    parts = urlsplit(value)
    return value if parts.scheme in ("http", "https") and "." in (parts.hostname or "") else None


class LeadContact(BaseModel):
    """A public professional contact or official company route, with its evidence."""

    kind: ContactKind
    position: str = Field(min_length=1, max_length=160)
    company: str = Field(min_length=1, max_length=160)
    name: str | None = Field(default=None, max_length=160)
    professional_profile: str | None = None
    public_work_email: str | None = Field(default=None, max_length=254)
    official_route: str | None = None
    source_url: str
    verified_on: date
    confidence: ContactConfidence

    @field_validator("professional_profile", "official_route", "source_url", mode="before")
    @classmethod
    def _url(cls, value: Any) -> str | None:
        return _http_url(value)

    @field_validator("public_work_email", mode="before")
    @classmethod
    def _email(cls, value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        return value if "@" in value and "." in value.rsplit("@", 1)[-1] else None


class LeadEnrichment(BaseModel):
    """The current verified lead record; unknown facts remain empty and are explained."""

    primary_track: TrackName
    technical_score: int | None = Field(default=None, ge=0, le=100)
    vacancy_status: VacancyStatus
    eligibility_status: EligibilityStatus
    application_priority: ApplicationPriority
    official_application_url: str
    official_source_url: str
    last_verified_at: datetime
    publication_date: date | None = None
    location: str | None = Field(default=None, max_length=300)
    work_mode: str = Field(default="unknown", max_length=40)
    compensation: str | None = Field(default=None, max_length=300)
    internship_duration: str | None = Field(default=None, max_length=300)
    mandatory_experience: list[str] = Field(default_factory=list)
    preferred_experience: list[str] = Field(default_factory=list)
    eligibility_evidence: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    priority_reasons: list[str] = Field(default_factory=list)
    recommended_resume: str | None = Field(default=None, max_length=200)
    contacts: list[LeadContact] = Field(default_factory=list)
    application_strategy: str = Field(default="", max_length=3000)
    outreach_draft: str | None = Field(default=None, max_length=5000)
    cover_letter_draft: str | None = Field(default=None, max_length=8000)

    @field_validator("official_application_url", "official_source_url", mode="before")
    @classmethod
    def _url(cls, value: Any) -> str | None:
        return _http_url(value)


def prioritize_lead(
    vacancy: VacancyStatus,
    eligibility: EligibilityStatus,
    technical_score: int | None,
    has_official_application_url: bool,
) -> tuple[ApplicationPriority, list[str]]:
    """Set application priority without turning missing evidence into a positive signal."""
    reasons: list[str] = []
    if vacancy == "expired":
        return "exclude", ["the official source no longer lists the vacancy"]
    if eligibility == "NOT_ELIGIBLE":
        return "exclude", ["verified mandatory eligibility is not met"]
    if vacancy == "unknown":
        reasons.append("current vacancy status could not be verified")
    if eligibility == "NEEDS_VERIFICATION":
        reasons.append("a critical eligibility condition still needs verification")
    if not has_official_application_url:
        reasons.append("no verified official application URL is available")
    if technical_score is None:
        reasons.append("technical fit is unscored")
    if reasons:
        return "review", reasons
    if technical_score is not None and technical_score >= 70:
        return "apply_now", ["active, sufficiently evidenced, and technically strong"]
    if technical_score is not None and technical_score >= 50:
        return "low", ["active and eligible, but technical fit is moderate"]
    return "low", ["active and eligible, but technical fit is weak or unavailable"]
