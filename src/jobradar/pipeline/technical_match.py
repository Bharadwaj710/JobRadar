"""Pure four-track matching over approved candidate evidence and extracted requirements.

This module does not replace the legacy matching provider and never reads or writes the
database.  It keeps one shared eligibility result beside four independent technical-fit
assessments.  Missing evidence stays unknown; it is never converted to a zero or a positive
claim merely to produce a score.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, Field

from jobradar.pipeline.eligibility import (
    EligibilityCheck,
    EligibilityResult,
    candidate_eligibility,
    evaluate_eligibility,
)
from jobradar.pipeline.schemas import JobPosting, SkillRequirement
from jobradar.profile import TRACK_NAMES, TrackName, candidate_views, profile_version
from jobradar.providers.skills_rule import skill_key, technical

ComponentName = Literal[
    "role_alignment",
    "required_skills",
    "preferred_skills",
    "project_internship_relevance",
    "education_graduation_fit",
    "experience_seniority_fit",
    "location_work_mode",
    "compensation",
]
AssessmentStatus = Literal["scored", "unscored"]

TRACK_WEIGHTS: dict[ComponentName, int] = {
    "role_alignment": 25,
    "required_skills": 25,
    "preferred_skills": 5,
    "project_internship_relevance": 15,
    "education_graduation_fit": 10,
    "experience_seniority_fit": 10,
    "location_work_mode": 5,
    "compensation": 5,
}
MIN_EVIDENCE_COVERAGE = 0.70
TRACK_ENGINE_VERSION = "j4-c2-v1"


class FitComponent(BaseModel):
    value: float | None = Field(default=None, ge=0, le=1)
    weight: int = Field(ge=0)
    reason: str
    evidence: list[str] = Field(default_factory=list)


class TrackAssessment(BaseModel):
    track: TrackName
    status: AssessmentStatus
    score: int | None = Field(default=None, ge=0, le=100)
    unscored_reason: str | None = None
    evidence_coverage: float = Field(ge=0, le=1)
    matched_skills: list[str] = Field(default_factory=list)
    missing_required_skills: list[str] = Field(default_factory=list)
    relevant_projects: list[str] = Field(default_factory=list)
    relevant_internships: list[str] = Field(default_factory=list)
    relevant_experience: list[str] = Field(default_factory=list)
    evidence_quality: dict[str, int] = Field(default_factory=dict)
    explanation: str
    recommended_resume: str | None = None
    components: dict[ComponentName, FitComponent]


class FourTrackAssessment(BaseModel):
    """One eligibility result and four technical views for one unchanged job record."""

    eligibility: EligibilityResult
    tracks: dict[TrackName, TrackAssessment]


def track_profile_version(profile: dict[str, Any], as_of: date) -> str | None:
    """Version only the approved candidate data seen by J4 matching.

    Legacy profiles have no four evidence-backed views and keep the pre-J4 behaviour. Candidate
    eligibility is included instead of the full profile so contact details and resume-only
    metadata cannot affect or enter the matching history key.
    """

    views = candidate_views(profile, "matching")
    if not views:
        return None
    candidate = candidate_eligibility(profile, as_of)
    return profile_version(
        {
            "engine": TRACK_ENGINE_VERSION,
            "weights": TRACK_WEIGHTS,
            "candidate": candidate.model_dump(mode="json"),
            "views": views,
        }
    )


@dataclass(frozen=True)
class _Support:
    fact_id: str
    category: str
    label: str
    status: str


@dataclass
class _SkillProof:
    displays: list[str] = field(default_factory=list)
    supports: dict[str, _Support] = field(default_factory=dict)
    fact_statuses: dict[str, str] = field(default_factory=dict)


@dataclass
class _TrackEvidence:
    skills: dict[str, _SkillProof] = field(default_factory=dict)


@dataclass(frozen=True)
class _SkillObligation:
    skills: tuple[str, ...]
    relation: Literal["all", "any"]
    evidence: str


@dataclass
class _SkillMatch:
    value: float | None
    total: int = 0
    matched_obligations: int = 0
    matched_skills: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    proof_keys: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return []


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _track_evidence(view: dict[str, Any]) -> _TrackEvidence:
    """Candidate skills with approved project or experience evidence in this track.

    `candidate_views()` has already removed unverified and rejected facts.  A skill fact must
    additionally point at an approved project/experience fact in the same view; a bare resume
    skills-section claim therefore cannot become a demonstrated skill.
    """

    raw_facts = view.get("facts")
    facts = raw_facts if isinstance(raw_facts, dict) else {}
    supports_by_ref: dict[str, list[_Support]] = {}
    for fact_id, raw in facts.items():
        if not isinstance(raw, dict) or raw.get("category") not in ("project", "experience"):
            continue
        values = _strings(raw.get("value"))
        if not values:
            continue
        support = _Support(
            fact_id=str(fact_id),
            category=str(raw["category"]),
            label=values[0],
            status=str(raw.get("status") or "unknown"),
        )
        for reference in _strings(raw.get("evidence_refs")):
            supports_by_ref.setdefault(reference, []).append(support)

    result = _TrackEvidence()
    for fact_id, raw in facts.items():
        if not isinstance(raw, dict) or raw.get("category") != "skill":
            continue
        references = _strings(raw.get("evidence_refs"))
        supports = {
            support.fact_id: support
            for reference in references
            for support in supports_by_ref.get(reference, [])
        }
        if not supports:
            continue
        status = str(raw.get("status") or "unknown")
        for display in technical(_strings(raw.get("value"))):
            key = skill_key(display)
            if not key:
                continue
            proof = result.skills.setdefault(key, _SkillProof())
            if display not in proof.displays:
                proof.displays.append(display)
            proof.supports.update(supports)
            proof.fact_statuses[str(fact_id)] = status
            for support in supports.values():
                proof.fact_statuses[support.fact_id] = support.status
    return result


def _requirement_evidence(item: SkillRequirement, fallback: str) -> str:
    return item.evidence.sentence or fallback


def _obligations(posting: JobPosting, preferred: bool) -> list[_SkillObligation]:
    structured = []
    if posting.requirements is not None:
        structured = (
            posting.requirements.preferred_skills
            if preferred
            else posting.requirements.required_skills
        )
    obligations: list[_SkillObligation] = []
    for item in structured:
        skills = tuple(technical(list(item.skills)))
        if not skills:
            continue
        sentence = _requirement_evidence(item, "structured skill requirement")
        if item.relation == "any":
            obligations.append(_SkillObligation(skills, "any", sentence))
        else:
            obligations.extend(_SkillObligation((skill,), "all", sentence) for skill in skills)
    if obligations:
        return obligations

    legacy = posting.skills_preferred if preferred else posting.skills_required
    source = "job.skills_preferred" if preferred else "job.skills_required"
    return [_SkillObligation((skill,), "all", source) for skill in technical(list(legacy))]


def _match_skills(obligations: list[_SkillObligation], evidence: _TrackEvidence) -> _SkillMatch:
    if not obligations:
        return _SkillMatch(value=None)
    matched: list[str] = []
    missing: list[str] = []
    proof_keys: set[str] = set()
    matched_obligations = 0
    source_evidence: list[str] = []
    for obligation in obligations:
        available = [skill for skill in obligation.skills if skill_key(skill) in evidence.skills]
        if available:
            matched_obligations += 1
            matched.extend(available)
            proof_keys.update(skill_key(skill) for skill in available)
        elif obligation.relation == "any":
            missing.append(" or ".join(obligation.skills))
        else:
            missing.extend(obligation.skills)
        source_evidence.append(obligation.evidence)
    total = len(obligations)
    return _SkillMatch(
        value=round(matched_obligations / total, 4),
        total=total,
        matched_obligations=matched_obligations,
        matched_skills=_unique(matched),
        missing=_unique(missing),
        proof_keys=proof_keys,
        evidence=_unique(source_evidence),
    )


def _role_component(posting: JobPosting, track: TrackName) -> FitComponent:
    weight = TRACK_WEIGHTS["role_alignment"]
    if not posting.role_tracks:
        return FitComponent(
            value=None,
            weight=weight,
            reason="the J4-B role taxonomy has no classification evidence",
        )
    matches = [item for item in posting.role_tracks if item.track == track]
    evidence = _unique([item.sentence for role in posting.role_tracks for item in role.evidence])
    if not matches:
        classified = ", ".join(item.track for item in posting.role_tracks)
        return FitComponent(
            value=0,
            weight=weight,
            reason=f"the posting is classified for {classified}, not {track}",
            evidence=evidence,
        )
    strongest = max(matches, key=lambda item: item.strength == "strong")
    value = 1.0 if strongest.strength == "strong" else 0.5
    return FitComponent(
        value=value,
        weight=weight,
        reason=f"J4-B classifies {track} alignment as {strongest.strength}",
        evidence=_unique([item.sentence for match in matches for item in match.evidence]),
    )


def _skill_component(match: _SkillMatch, preferred: bool) -> FitComponent:
    name: ComponentName = "preferred_skills" if preferred else "required_skills"
    kind = "preferred" if preferred else "required"
    if match.value is None:
        return FitComponent(
            value=None,
            weight=TRACK_WEIGHTS[name],
            reason=f"the posting states no {kind} technical skills",
        )
    return FitComponent(
        value=match.value,
        weight=TRACK_WEIGHTS[name],
        reason=f"matched {match.matched_obligations} of {match.total} {kind} skill obligations",
        evidence=match.evidence,
    )


def _project_component(required: _SkillMatch, preferred: _SkillMatch) -> FitComponent:
    total = required.total + preferred.total
    if not total:
        return FitComponent(
            value=None,
            weight=TRACK_WEIGHTS["project_internship_relevance"],
            reason="no technical requirements are stated for evidence-based project relevance",
        )
    supported = required.matched_obligations + preferred.matched_obligations
    return FitComponent(
        value=round(supported / total, 4),
        weight=TRACK_WEIGHTS["project_internship_relevance"],
        reason=(
            "approved project or experience evidence supports "
            f"{supported} of {total} skill obligations"
        ),
        evidence=_unique(required.evidence + preferred.evidence),
    )


def _check_text(check: EligibilityCheck) -> list[str]:
    values = []
    for item in check.evidence:
        values.append(item.sentence or f"{item.source}={item.value}")
    return values


def _eligibility_component(
    result: EligibilityResult,
    name: ComponentName,
    dimensions: frozenset[str],
) -> FitComponent:
    checks = [check for check in result.checks if check.dimension in dimensions]
    evidence = _unique([value for check in checks for value in _check_text(check)])
    if not checks:
        return FitComponent(
            value=None,
            weight=TRACK_WEIGHTS[name],
            reason="the posting states no evidence for this dimension",
        )
    if any(check.critical and check.status == "not_met" for check in checks):
        return FitComponent(
            value=0,
            weight=TRACK_WEIGHTS[name],
            reason="a mandatory eligibility check is not met",
            evidence=evidence,
        )
    if any(check.status == "unknown" for check in checks):
        return FitComponent(
            value=None,
            weight=TRACK_WEIGHTS[name],
            reason="eligibility evidence for this dimension is incomplete",
            evidence=evidence,
        )
    if any(not check.critical and check.status == "not_met" for check in checks):
        return FitComponent(
            value=0.5,
            weight=TRACK_WEIGHTS[name],
            reason="mandatory checks pass but a preferred condition is not met",
            evidence=evidence,
        )
    return FitComponent(
        value=1,
        weight=TRACK_WEIGHTS[name],
        reason="all stated checks for this dimension are met",
        evidence=evidence,
    )


def _compensation_component(posting: JobPosting) -> FitComponent:
    evidence = [posting.salary_text] if posting.salary_text else []
    return FitComponent(
        value=None,
        weight=TRACK_WEIGHTS["compensation"],
        reason=(
            "candidate compensation preference is not an approved comparable fact"
            if posting.salary_text
            else (
                "the posting and approved candidate profile do not provide comparable "
                "compensation evidence"
            )
        ),
        evidence=evidence,
    )


def _resume_name(view: dict[str, Any]) -> str | None:
    source = view.get("resume_source")
    if not isinstance(source, dict) or not source.get("file"):
        return None
    return PureWindowsPath(str(source["file"])).name


def _relevant_evidence(
    evidence: _TrackEvidence, proof_keys: set[str]
) -> tuple[list[str], list[str], list[str], dict[str, int]]:
    projects: list[str] = []
    internships: list[str] = []
    experience: list[str] = []
    used_facts: dict[str, str] = {}
    for key in sorted(proof_keys):
        proof = evidence.skills.get(key)
        if proof is None:
            continue
        used_facts.update(proof.fact_statuses)
        for support in proof.supports.values():
            if support.category == "project":
                projects.append(support.label)
            elif "intern" in support.label.casefold():
                internships.append(support.label)
            else:
                experience.append(support.label)
    quality = dict(sorted(Counter(used_facts.values()).items()))
    return _unique(projects), _unique(internships), _unique(experience), quality


def _score(components: dict[ComponentName, FitComponent]) -> tuple[int | None, float, str | None]:
    known = [component for component in components.values() if component.value is not None]
    known_weight = sum(component.weight for component in known)
    coverage = round(known_weight / sum(TRACK_WEIGHTS.values()), 4)
    role = components["role_alignment"]
    required = components["required_skills"]
    if role.value is None:
        return None, coverage, "role alignment lacks J4-B taxonomy evidence"
    if role.value == 0:
        return None, coverage, "the J4-B role taxonomy does not support this track"
    if required.value is None:
        return None, coverage, "required technical skills are not stated"
    if coverage < MIN_EVIDENCE_COVERAGE:
        return (
            None,
            coverage,
            f"evidence coverage {coverage:.0%} is below the required {MIN_EVIDENCE_COVERAGE:.0%}",
        )
    weighted = sum(component.weight * (component.value or 0) for component in known)
    return round(100 * weighted / known_weight), coverage, None


def _legacy_assessment(track: TrackName) -> TrackAssessment:
    components = {
        name: FitComponent(
            value=None,
            weight=weight,
            reason="the legacy profile has no evidence-backed career view",
        )
        for name, weight in TRACK_WEIGHTS.items()
    }
    return TrackAssessment(
        track=track,
        status="unscored",
        score=None,
        unscored_reason="the profile has no J4-A evidence-backed career views",
        evidence_coverage=0,
        explanation=f"{track} is unscored because only the legacy profile is available",
        components=components,
    )


def _failed_assessment(
    track: TrackName,
    view: dict[str, Any],
    eligibility: EligibilityResult,
    error: Exception,
) -> TrackAssessment:
    """Keep another track's deterministic failure from discarding valid assessments."""

    failure = f"track assessment failed safely ({type(error).__name__})"
    components = {
        name: FitComponent(value=None, weight=weight, reason=failure)
        for name, weight in TRACK_WEIGHTS.items()
    }
    return TrackAssessment(
        track=track,
        status="unscored",
        score=None,
        unscored_reason=failure,
        evidence_coverage=0,
        explanation=(
            f"{track}: unscored after an isolated deterministic assessment failure; "
            f"eligibility remains separately {eligibility.state.value}"
        ),
        recommended_resume=_resume_name(view),
        components=components,
    )


def _assess_track(
    posting: JobPosting,
    eligibility: EligibilityResult,
    track: TrackName,
    view: dict[str, Any],
) -> TrackAssessment:
    evidence = _track_evidence(view)
    required = _match_skills(_obligations(posting, preferred=False), evidence)
    preferred = _match_skills(_obligations(posting, preferred=True), evidence)
    components: dict[ComponentName, FitComponent] = {
        "role_alignment": _role_component(posting, track),
        "required_skills": _skill_component(required, preferred=False),
        "preferred_skills": _skill_component(preferred, preferred=True),
        "project_internship_relevance": _project_component(required, preferred),
        "education_graduation_fit": _eligibility_component(
            eligibility,
            "education_graduation_fit",
            frozenset(
                {"graduation_year", "degree", "degree_branch", "student_status", "completed_degree"}
            ),
        ),
        "experience_seniority_fit": _eligibility_component(
            eligibility,
            "experience_seniority_fit",
            frozenset({"experience", "joining_date", "internship_duration"}),
        ),
        "location_work_mode": _eligibility_component(
            eligibility,
            "location_work_mode",
            frozenset({"location", "work_authorization"}),
        ),
        "compensation": _compensation_component(posting),
    }
    score, coverage, unscored = _score(components)
    proof_keys = required.proof_keys | preferred.proof_keys
    projects, internships, experience_items, quality = _relevant_evidence(evidence, proof_keys)
    matched = _unique(required.matched_skills + preferred.matched_skills)
    fit = f"score {score}" if score is not None else f"unscored: {unscored}"
    explanation = (
        f"{track}: {fit}; evidence coverage {coverage:.0%}; "
        f"required skills {required.matched_obligations}/{required.total or 0}; "
        f"eligibility remains separately {eligibility.state.value}"
    )
    return TrackAssessment(
        track=track,
        status="scored" if score is not None else "unscored",
        score=score,
        unscored_reason=unscored,
        evidence_coverage=coverage,
        matched_skills=matched,
        missing_required_skills=required.missing,
        relevant_projects=projects,
        relevant_internships=internships,
        relevant_experience=experience_items,
        evidence_quality=quality,
        explanation=explanation,
        recommended_resume=_resume_name(view),
        components=components,
    )


def evaluate_technical_fit(
    posting: JobPosting,
    profile: dict[str, Any],
    eligibility: EligibilityResult,
) -> FourTrackAssessment:
    """Assess all four views without changing eligibility, priority, scores or persistence."""

    views = candidate_views(profile, "matching")
    tracks: dict[TrackName, TrackAssessment] = {}
    for track in TRACK_NAMES:
        view = views.get(track)
        if view is None:
            tracks[track] = _legacy_assessment(track)
            continue
        try:
            tracks[track] = _assess_track(posting, eligibility, track, view)
        except Exception as error:
            tracks[track] = _failed_assessment(track, view, eligibility, error)
    return FourTrackAssessment(eligibility=eligibility, tracks=tracks)


def assess_job(
    posting: JobPosting,
    profile: dict[str, Any],
    as_of: date,
) -> FourTrackAssessment:
    """Convenience wrapper that calls the J4-C1 engine once, then performs technical fit."""

    candidate = candidate_eligibility(profile, as_of)
    eligibility = evaluate_eligibility(posting, candidate)
    return evaluate_technical_fit(posting, profile, eligibility)
