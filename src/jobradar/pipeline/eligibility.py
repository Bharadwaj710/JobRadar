"""Pure fresher eligibility evaluation, separate from technical fit and priority.

The engine consumes the canonical candidate profile and one extracted JobPosting. It never
reads or writes the database. A mandatory confirmed failure is NOT_ELIGIBLE; missing critical
evidence stays NEEDS_VERIFICATION instead of being guessed.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobradar.pipeline.schemas import JobPosting, RequirementEvidence
from jobradar.pipeline.values import parse_batch_years, parse_degrees
from jobradar.profile import evidence_is_approved, profile_extensions

CheckStatus = Literal["met", "not_met", "unknown"]


class EligibilityState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    LIKELY_ELIGIBLE = "LIKELY_ELIGIBLE"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class CandidateEligibility(BaseModel):
    graduation_year: int | None = None
    degree: str | None = None
    branch: str | None = None
    current_undergraduate: bool | None = None
    professional_experience_years: float | None = Field(default=None, ge=0)
    internship_experience: bool | None = None
    academic_project_experience: bool | None = None
    location: str | None = None
    location_preferences: list[str] = Field(default_factory=list)
    work_authorizations: list[str] = Field(default_factory=list)
    available_full_time_from: date | None = None
    maximum_internship_months: float | None = Field(default=None, ge=0)
    evidence: list[str] = Field(default_factory=list)


class EligibilityEvidence(BaseModel):
    source: str
    value: str
    sentence: str | None = None
    section: str | None = None


class EligibilityCheck(BaseModel):
    dimension: str
    status: CheckStatus
    critical: bool
    reason: str
    evidence: list[EligibilityEvidence] = Field(default_factory=list)


class EligibilityResult(BaseModel):
    state: EligibilityState
    checks: list[EligibilityCheck]
    blockers: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


def _category_is_approved(profile: dict[str, Any], category: str) -> bool:
    extensions = profile_extensions(profile)
    if extensions is None:
        return True
    evidence, _ = extensions
    facts = [fact for fact in evidence.facts.values() if fact.category == category]
    return any(evidence_is_approved(fact) for fact in facts)


def _graduation_year(education: dict[str, Any]) -> int | None:
    value = education.get("graduation_year")
    if isinstance(value, int):
        return value
    years = re.findall(r"\b(20\d\d)\b", str(education.get("years") or ""))
    return int(years[-1]) if years else None


def candidate_eligibility(profile: dict[str, Any], as_of: date) -> CandidateEligibility:
    """Eligibility facts from an approved canonical profile; legacy profiles still work."""
    education = profile.get("education")
    first = education[0] if isinstance(education, list) and education else {}
    first = first if isinstance(first, dict) else {}
    education_ok = _category_is_approved(profile, "education")
    experience_ok = _category_is_approved(profile, "experience")

    graduation_year = _graduation_year(first) if education_ok else None
    if graduation_year is None:
        current: bool | None = None
    elif graduation_year > as_of.year:
        current = True
    elif graduation_year < as_of.year:
        current = False
    else:
        current = None  # graduation month is not known

    experience = profile.get("experience")
    entries = [item for item in experience or [] if isinstance(item, dict)]
    internship = any("intern" in str(item.get("title") or "").casefold() for item in entries)
    non_internship = [
        item for item in entries if "intern" not in str(item.get("title") or "").casefold()
    ]
    professional_years: float | None
    if not experience_ok or not entries:
        professional_years = None
        internship_value: bool | None = None
    elif non_internship:
        professional_years = None  # titles alone do not establish a duration
        internship_value = internship
    else:
        professional_years = 0.0
        internship_value = internship

    projects = profile.get("projects")
    approved_projects = _category_is_approved(profile, "project")
    has_projects = approved_projects and any(
        isinstance(item, dict) and item.get("on_resume") is not False for item in projects or []
    )
    basics = profile.get("basics")
    basics = basics if isinstance(basics, dict) else {}
    preferences = profile.get("preferences")
    preferences = preferences if isinstance(preferences, dict) else {}

    evidence: list[str] = []
    if graduation_year is not None:
        evidence.append(f"profile.education.graduation_year={graduation_year}")
    if first.get("degree"):
        evidence.append(f"profile.education.degree={first['degree']}")
    if first.get("branch"):
        evidence.append(f"profile.education.branch={first['branch']}")
    if entries and not non_internship:
        evidence.append("profile.experience contains only internships")

    return CandidateEligibility(
        graduation_year=graduation_year,
        degree=str(first.get("degree")) if education_ok and first.get("degree") else None,
        branch=str(first.get("branch")) if education_ok and first.get("branch") else None,
        current_undergraduate=current,
        professional_experience_years=professional_years,
        internship_experience=internship_value,
        academic_project_experience=has_projects if approved_projects else None,
        location=str(basics.get("location")) if basics.get("location") else None,
        location_preferences=[str(item) for item in preferences.get("locations") or []],
        evidence=evidence,
    )


def _field_evidence(source: str, value: Any) -> EligibilityEvidence:
    return EligibilityEvidence(source=source, value=str(value))


def _sentence_evidence(evidence: RequirementEvidence) -> EligibilityEvidence:
    return EligibilityEvidence(
        source=f"job.requirements:{evidence.source}",
        value=evidence.sentence,
        sentence=evidence.sentence,
        section=evidence.section,
    )


def _all_requirement_evidence(posting: JobPosting) -> list[tuple[RequirementEvidence, bool]]:
    requirements = posting.requirements
    if requirements is None:
        return []
    found: list[tuple[RequirementEvidence, bool]] = []
    found.extend(
        (item.evidence, item.need == "required") for item in requirements.general_qualifications
    )
    found.extend((item.evidence, item.need == "required") for item in requirements.experience)
    found.extend((item.evidence, item.required) for item in requirements.student_requirements)
    found.extend((item.evidence, True) for item in requirements.required_skills)
    found.extend((item.evidence, False) for item in requirements.preferred_skills)
    return found


def _degree_key(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.casefold())


def _degree_matches(candidate: str, allowed: list[str]) -> bool:
    candidate_key = _degree_key(candidate)
    keys = {_degree_key(value) for value in allowed}
    if "anydegree" in keys:
        return True
    if candidate_key in keys:
        return True
    return candidate_key in {"btech", "be"} and bool(keys & {"btech", "be"})


_BRANCH_WORDS = re.compile(
    r"\b(computer science|cse|information technology|\bit\b|electronics|electrical|mechanical|"
    r"civil|chemical|related (?:field|branch|discipline)|any (?:branch|engineering))\b",
    re.I,
)
_CSE_OK = re.compile(
    r"\b(computer science|cse|information technology|\bit\b|related (?:field|branch|discipline)|"
    r"any (?:branch|engineering))\b",
    re.I,
)
_COMPLETED_DEGREE = re.compile(
    r"\b(must have completed|degree (?:must be )?completed|already graduated|"
    r"must be (?:a )?graduate|completed (?:a )?(?:bachelor|degree))\b",
    re.I,
)
_JOINING = re.compile(
    r"\b(?:join(?:ing)?|start(?:ing)?)\s+(?:date|on|by|from|in)\b|"
    r"\b(?:available|availability)\s+(?:to|for)\s+(?:join|start|begin|work)\b|"
    r"\b(?:can|could|must|should)\s+(?:join|start|begin)\b",
    re.I,
)
_IMMEDIATE = re.compile(r"\b(immediate(?:ly)?|available now)\b", re.I)
_INTERNSHIP_DURATION = re.compile(
    r"\b(?:internship|duration)\b[^.\n]{0,60}?\b(\d+(?:\.\d+)?)\s*(months?|weeks?)\b|"
    r"\b(\d+(?:\.\d+)?)\s*(months?|weeks?)\b[^.\n]{0,40}\binternship\b",
    re.I,
)
_WORK_AUTH = re.compile(
    r"\b(work authori[sz]ation|authori[sz]ed to work|without sponsorship|visa sponsorship|"
    r"citizen(?:ship)?|permanent resident)\b",
    re.I,
)
_INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|ncr|gurgaon|gurugram|"
    r"noida|chennai|kolkata|visakhapatnam|andhra pradesh|karnataka|telangana)\b",
    re.I,
)
_GLOBAL = re.compile(r"\b(global|worldwide|anywhere|all countries)\b", re.I)
_OUTSIDE_INDIA = re.compile(
    r"\b(united states|usa|u\.s\.?|canada|united kingdom|uk|europe|emea|australia|"
    r"singapore|germany|france)\b",
    re.I,
)


def _graduation_check(posting: JobPosting, candidate: CandidateEligibility) -> EligibilityCheck:
    years = list(posting.batch_years)
    evidence: list[EligibilityEvidence] = []
    if years:
        evidence.append(_field_evidence("job.batch_years", years))
    else:
        for item, mandatory in _all_requirement_evidence(posting):
            if mandatory and re.search(
                r"\b(batch|graduat|class of|passing year)\b", item.sentence, re.I
            ):
                years.extend(parse_batch_years(item.sentence))
                evidence.append(_sentence_evidence(item))
    years = sorted(set(years))
    if candidate.graduation_year is None:
        return EligibilityCheck(
            dimension="graduation_year",
            status="unknown",
            critical=True,
            reason="candidate graduation year is not approved or stated",
            evidence=evidence,
        )
    if not years:
        return EligibilityCheck(
            dimension="graduation_year",
            status="unknown",
            critical=True,
            reason=(
                f"the posting does not confirm whether {candidate.graduation_year} "
                "graduates qualify"
            ),
            evidence=[_field_evidence("candidate.graduation_year", candidate.graduation_year)],
        )
    if candidate.graduation_year not in years:
        return EligibilityCheck(
            dimension="graduation_year",
            status="not_met",
            critical=True,
            reason=(
                f"posting limits eligibility to {years}; candidate graduates in "
                f"{candidate.graduation_year}"
            ),
            evidence=[
                *evidence,
                _field_evidence("candidate.graduation_year", candidate.graduation_year),
            ],
        )
    return EligibilityCheck(
        dimension="graduation_year",
        status="met",
        critical=True,
        reason=f"posting explicitly includes the {candidate.graduation_year} graduation year",
        evidence=[
            *evidence,
            _field_evidence("candidate.graduation_year", candidate.graduation_year),
        ],
    )


def _degree_checks(posting: JobPosting, candidate: CandidateEligibility) -> list[EligibilityCheck]:
    checks: list[EligibilityCheck] = []
    if posting.degrees:
        degree_status: CheckStatus
        if candidate.degree is None:
            degree_status = "unknown"
        else:
            degree_status = (
                "met" if _degree_matches(candidate.degree, posting.degrees) else "not_met"
            )
        checks.append(
            EligibilityCheck(
                dimension="degree",
                status=degree_status,
                critical=True,
                reason=f"required degree is {', '.join(posting.degrees)}",
                evidence=[
                    _field_evidence("job.degrees", posting.degrees),
                    _field_evidence("candidate.degree", candidate.degree or "unknown"),
                ],
            )
        )

    requirements = posting.requirements
    if requirements is None:
        return checks
    for qualification in requirements.general_qualifications:
        sentence = qualification.evidence.sentence
        degrees = parse_degrees(sentence)
        branch_stated = bool(_BRANCH_WORDS.search(sentence))
        if not degrees and not branch_stated:
            continue
        critical = qualification.need == "required"
        degree_ok = (
            True
            if not degrees
            else bool(candidate.degree and _degree_matches(candidate.degree, degrees))
        )
        branch_ok = True
        if branch_stated:
            branch_ok = bool(candidate.branch and _CSE_OK.search(sentence))
        qualification_status: CheckStatus = "met" if degree_ok and branch_ok else "not_met"
        if (degrees and candidate.degree is None) or (branch_stated and candidate.branch is None):
            qualification_status = "unknown"
        checks.append(
            EligibilityCheck(
                dimension="degree_branch",
                status=qualification_status,
                critical=critical,
                reason="required degree or branch evidence from the posting"
                if critical
                else "preferred degree or branch evidence from the posting",
                evidence=[_sentence_evidence(qualification.evidence)],
            )
        )
    return checks


def _student_checks(posting: JobPosting, candidate: CandidateEligibility) -> list[EligibilityCheck]:
    checks: list[EligibilityCheck] = []
    requirements = posting.requirements
    if requirements is None:
        return checks
    for item in requirements.student_requirements:
        if item.kind not in ("current_student", "degree_in_progress"):
            continue
        if candidate.current_undergraduate is None:
            status: CheckStatus = "unknown"
        else:
            status = "met" if candidate.current_undergraduate else "not_met"
        checks.append(
            EligibilityCheck(
                dimension="student_status",
                status=status,
                critical=item.required,
                reason="posting requires a current student or degree in progress",
                evidence=[_sentence_evidence(item.evidence)],
            )
        )
    for evidence, mandatory in _all_requirement_evidence(posting):
        if mandatory and _COMPLETED_DEGREE.search(evidence.sentence):
            status = (
                "unknown"
                if candidate.current_undergraduate is None
                else ("not_met" if candidate.current_undergraduate else "met")
            )
            checks.append(
                EligibilityCheck(
                    dimension="completed_degree",
                    status=status,
                    critical=True,
                    reason="posting requires the degree to be completed",
                    evidence=[_sentence_evidence(evidence)],
                )
            )
    return checks


def _experience_checks(
    posting: JobPosting, candidate: CandidateEligibility
) -> list[EligibilityCheck]:
    requirements = posting.requirements
    checks: list[EligibilityCheck] = []
    if requirements and requirements.experience_status == "conflicting":
        return [
            EligibilityCheck(
                dimension="experience",
                status="unknown",
                critical=True,
                reason="posting contains conflicting mandatory experience requirements",
                evidence=[_sentence_evidence(item.evidence) for item in requirements.experience],
            )
        ]
    items = requirements.experience if requirements else []
    for item in items:
        critical = item.need == "required"
        if item.experience_type == "internship":
            if not candidate.internship_experience:
                status: CheckStatus = (
                    "unknown" if candidate.internship_experience is None else "not_met"
                )
            elif item.minimum_years:
                status = "unknown"  # an internship exists, but its duration is not inferred
            else:
                status = "met"
        elif item.experience_type == "academic":
            if candidate.academic_project_experience is None:
                status = "unknown"
            else:
                status = "met" if candidate.academic_project_experience else "not_met"
        elif item.minimum_years is None:
            status = "met" if item.subject is None else "unknown"
        elif candidate.professional_experience_years is None:
            status = "unknown"
        else:
            status = (
                "met"
                if candidate.professional_experience_years >= item.minimum_years
                else "not_met"
            )
        checks.append(
            EligibilityCheck(
                dimension="experience",
                status=status,
                critical=critical,
                reason=(
                    f"{item.need} {item.experience_type} experience"
                    + (
                        f" of {item.minimum_years:g}+ years"
                        if item.minimum_years is not None
                        else ""
                    )
                ),
                evidence=[_sentence_evidence(item.evidence)],
            )
        )

    if not items and posting.experience_min is not None:
        if candidate.professional_experience_years is None:
            status = "unknown"
        else:
            status = (
                "met"
                if candidate.professional_experience_years >= posting.experience_min
                else "not_met"
            )
        checks.append(
            EligibilityCheck(
                dimension="experience",
                status=status,
                critical=True,
                reason=f"posting requires {posting.experience_min:g}+ years of experience",
                evidence=[_field_evidence("job.experience_min", posting.experience_min)],
            )
        )
    return checks


def _joining_checks(posting: JobPosting, candidate: CandidateEligibility) -> list[EligibilityCheck]:
    checks: list[EligibilityCheck] = []
    for evidence, mandatory in _all_requirement_evidence(posting):
        sentence = evidence.sentence
        if not mandatory or not _JOINING.search(sentence):
            continue
        years = parse_batch_years(sentence)
        if _IMMEDIATE.search(sentence):
            status: CheckStatus = "unknown"
            reason = (
                "immediate availability is required but candidate availability is not confirmed"
            )
        elif years and posting.employment_type == "full_time" and candidate.graduation_year:
            start_year = min(years)
            if start_year < candidate.graduation_year:
                status = "not_met"
                reason = "required full-time joining is before the candidate's graduation year"
            elif start_year == candidate.graduation_year:
                status = "unknown"
                reason = "joining and graduation are in the same year; exact dates are missing"
            else:
                status = "met"
                reason = "required joining year is after the candidate's graduation year"
        elif candidate.available_full_time_from is None:
            status = "unknown"
            reason = "joining availability is not confirmed"
        else:
            status = "met"
            reason = "candidate availability is recorded"
        checks.append(
            EligibilityCheck(
                dimension="joining_date",
                status=status,
                critical=True,
                reason=reason,
                evidence=[_sentence_evidence(evidence)],
            )
        )
    return checks


def _duration_checks(
    posting: JobPosting, candidate: CandidateEligibility
) -> list[EligibilityCheck]:
    checks: list[EligibilityCheck] = []
    for evidence, mandatory in _all_requirement_evidence(posting):
        if not mandatory or not (match := _INTERNSHIP_DURATION.search(evidence.sentence)):
            continue
        number = float(match.group(1) or match.group(3))
        unit = (match.group(2) or match.group(4) or "months").casefold()
        months = round(number / 4.345, 2) if unit.startswith("week") else number
        if candidate.maximum_internship_months is None:
            status: CheckStatus = "unknown"
            reason = f"candidate availability for a {months:g}-month internship is not confirmed"
        else:
            status = "met" if candidate.maximum_internship_months >= months else "not_met"
            reason = f"posting requires a {months:g}-month internship"
        checks.append(
            EligibilityCheck(
                dimension="internship_duration",
                status=status,
                critical=True,
                reason=reason,
                evidence=[_sentence_evidence(evidence)],
            )
        )
    return checks


def _location_check(posting: JobPosting, candidate: CandidateEligibility) -> EligibilityCheck:
    locations = " | ".join(posting.locations)
    evidence = [_field_evidence("job.locations", posting.locations)] if posting.locations else []
    candidate_in_india = bool(
        (candidate.location and _INDIA.search(candidate.location))
        or any(_INDIA.search(loc) for loc in candidate.location_preferences)
    )
    if posting.work_mode == "remote":
        if _INDIA.search(locations) and candidate_in_india:
            status: CheckStatus = "met"
            reason = "remote role is explicitly open in India"
        elif _GLOBAL.search(locations):
            status = "met"
            reason = "remote role is explicitly worldwide"
        elif _OUTSIDE_INDIA.search(locations) and not _INDIA.search(locations):
            status = "not_met"
            reason = "remote role is restricted outside India"
        else:
            status = "unknown"
            reason = "remote eligibility for candidates in India is not stated"
    elif _INDIA.search(locations) and candidate_in_india:
        status = "met"
        reason = "posting location and candidate location are in India"
    elif not posting.locations:
        status = "unknown"
        reason = "posting location is not stated"
    elif _OUTSIDE_INDIA.search(locations):
        status = "unknown"
        reason = "role is outside India and work authorization is not known"
    else:
        status = "unknown"
        reason = "posting location could not be evaluated safely"
    return EligibilityCheck(
        dimension="location",
        status=status,
        critical=True,
        reason=reason,
        evidence=[
            *evidence,
            _field_evidence("candidate.location", candidate.location or "unknown"),
        ],
    )


def _authorization_checks(
    posting: JobPosting, candidate: CandidateEligibility
) -> list[EligibilityCheck]:
    checks: list[EligibilityCheck] = []
    for evidence, mandatory in _all_requirement_evidence(posting):
        if not mandatory or not _WORK_AUTH.search(evidence.sentence):
            continue
        status: CheckStatus = "met" if candidate.work_authorizations else "unknown"
        checks.append(
            EligibilityCheck(
                dimension="work_authorization",
                status=status,
                critical=True,
                reason=(
                    "candidate work authorization is recorded"
                    if status == "met"
                    else (
                        "posting requires work authorization but candidate authorization is unknown"
                    )
                ),
                evidence=[_sentence_evidence(evidence)],
            )
        )
    return checks


def evaluate_eligibility(posting: JobPosting, candidate: CandidateEligibility) -> EligibilityResult:
    """Evaluate eligibility without changing technical fit or application priority."""
    checks = [
        _graduation_check(posting, candidate),
        *_degree_checks(posting, candidate),
        *_student_checks(posting, candidate),
        *_experience_checks(posting, candidate),
        *_joining_checks(posting, candidate),
        *_duration_checks(posting, candidate),
        _location_check(posting, candidate),
        *_authorization_checks(posting, candidate),
    ]
    blockers = [check.reason for check in checks if check.critical and check.status == "not_met"]
    unresolved = [check.reason for check in checks if check.status == "unknown"]
    if blockers:
        state = EligibilityState.NOT_ELIGIBLE
    elif any(check.critical and check.status == "unknown" for check in checks):
        state = EligibilityState.NEEDS_VERIFICATION
    elif any(not check.critical and check.status != "met" for check in checks):
        state = EligibilityState.LIKELY_ELIGIBLE
    else:
        state = EligibilityState.ELIGIBLE
    return EligibilityResult(state=state, checks=checks, blockers=blockers, unresolved=unresolved)
