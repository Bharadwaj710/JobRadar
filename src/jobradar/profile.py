"""config/profile.yaml, and the compact view of it that matching sends to a provider.

Only what matching needs leaves this machine: education, skills, experience and project
bullets, preferences and filters. `basics` (name, email, phone, links), `summary` and
`answers` (expected CTC, …) are never included.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from jobradar.config import ConfigError, FiltersConfig, MatchingConfig
from jobradar.pipeline.dedupe import sha1

TrackName = Literal["ai_backend", "full_stack", "sde", "sdet"]
EvidenceStatus = Literal[
    "resume_supported",
    "user_confirmed",
    "independently_verified",
    "unverified",
    "rejected",
]
EvidenceCategory = Literal[
    "education",
    "skill",
    "experience",
    "project",
    "metric",
    "preference",
    "public_profile",
]
EvidencePurpose = Literal["matching", "resume"]

TRACK_NAMES: tuple[TrackName, ...] = ("ai_backend", "full_stack", "sde", "sdet")
APPROVED_EVIDENCE = frozenset({"resume_supported", "user_confirmed", "independently_verified"})
INDEPENDENT_SOURCE_KINDS = frozenset({"document", "repository", "test"})


class _ExtensionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSource(_ExtensionModel):
    kind: Literal["resume", "user", "document", "repository", "test"]
    file: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{64}$")
    view: Literal["overall", "ai_backend", "full_stack", "sde", "sdet"] | None = None

    @model_validator(mode="after")
    def _resume_has_a_file(self) -> EvidenceSource:
        if self.kind == "resume" and not self.file:
            raise ValueError("a resume evidence source requires file")
        return self


class EvidenceFact(_ExtensionModel):
    value: Any
    status: EvidenceStatus
    category: EvidenceCategory
    sources: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    resume_safe: bool = False
    note: str | None = None

    @model_validator(mode="after")
    def _unsafe_status_is_not_resume_safe(self) -> EvidenceFact:
        if self.status in ("unverified", "rejected") and self.resume_safe:
            raise ValueError(f"{self.status} evidence cannot be resume_safe")
        return self


class EvidenceSection(_ExtensionModel):
    sources: dict[str, EvidenceSource] = Field(min_length=1)
    facts: dict[str, EvidenceFact] = Field(default_factory=dict)


class CareerTrack(_ExtensionModel):
    label: str = Field(min_length=1, max_length=80)
    resume: str = Field(min_length=1)
    target_roles: list[str] = Field(min_length=1)
    fact_refs: list[str] = Field(default_factory=list)


def _validation_message(error: ValidationError) -> str:
    parts = []
    for issue in error.errors(include_url=False, include_input=False):
        place = ".".join(str(p) for p in issue["loc"])
        parts.append(f"{place}: {issue['msg']}" if place else str(issue["msg"]))
    return "; ".join(parts)


def profile_extensions(
    profile: dict[str, Any],
) -> tuple[EvidenceSection, dict[TrackName, CareerTrack]] | None:
    """Validate the optional evidence-backed four-view profile extension.

    Profiles without either section are legacy profiles and remain valid. Once one section is
    present, both are required so a fact can never be selected without its evidence record.
    """
    evidence_raw = profile.get("evidence")
    tracks_raw = profile.get("career_tracks")
    if evidence_raw is None and tracks_raw is None:
        return None
    if evidence_raw is None or tracks_raw is None:
        raise ConfigError("profile evidence and career_tracks must be configured together")
    try:
        evidence = EvidenceSection.model_validate(evidence_raw)
        if not isinstance(tracks_raw, dict):
            raise ConfigError("profile career_tracks must be a mapping")
        tracks = {
            str(name): CareerTrack.model_validate(value) for name, value in tracks_raw.items()
        }
    except ValidationError as e:
        raise ConfigError("profile evidence is invalid: " + _validation_message(e)) from None

    expected = set(TRACK_NAMES)
    found = set(tracks)
    if found != expected:
        missing, extra = sorted(expected - found), sorted(found - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise ConfigError(
            "profile career_tracks must define exactly four views: " + "; ".join(details)
        )

    for fact_id, fact in evidence.facts.items():
        unknown = sorted(set(fact.sources) - set(evidence.sources))
        if unknown:
            raise ConfigError(f"profile evidence fact {fact_id!r} has unknown sources: {unknown}")
        source_kinds = {evidence.sources[source].kind for source in fact.sources}
        if fact.status == "resume_supported" and "resume" not in source_kinds:
            raise ConfigError(
                f"profile evidence fact {fact_id!r} is resume_supported but has no resume source"
            )
        if fact.status == "user_confirmed" and "user" not in source_kinds:
            raise ConfigError(
                f"profile evidence fact {fact_id!r} is user_confirmed but has no user source"
            )
        if fact.status == "independently_verified" and not (
            source_kinds & INDEPENDENT_SOURCE_KINDS
        ):
            raise ConfigError(
                f"profile evidence fact {fact_id!r} is independently_verified but has no "
                "independent source; repeated resume claims are not independent verification"
            )
    typed: dict[TrackName, CareerTrack] = {}
    for name in TRACK_NAMES:
        track = tracks[name]
        source = evidence.sources.get(track.resume)
        if source is None:
            raise ConfigError(
                f"profile career_tracks.{name}.resume has unknown source {track.resume!r}"
            )
        if source.kind != "resume":
            raise ConfigError(f"profile career_tracks.{name}.resume must refer to a resume source")
        unknown = sorted(set(track.fact_refs) - set(evidence.facts))
        if unknown:
            raise ConfigError(f"profile career_tracks.{name} has unknown fact_refs: {unknown}")
        typed[name] = track
    return evidence, typed


def evidence_is_approved(fact: EvidenceFact, purpose: EvidencePurpose = "matching") -> bool:
    """Whether a fact may enter a derived view for matching or resume generation."""
    if fact.status not in APPROVED_EVIDENCE:
        return False
    return purpose == "matching" or fact.resume_safe


def candidate_views(
    profile: dict[str, Any], purpose: EvidencePurpose = "matching"
) -> dict[TrackName, dict[str, Any]]:
    """Four evidence-backed views; an empty mapping for an unchanged legacy profile."""
    extensions = profile_extensions(profile)
    if extensions is None:
        return {}
    evidence, tracks = extensions
    views: dict[TrackName, dict[str, Any]] = {}
    for name in TRACK_NAMES:
        track = tracks[name]
        facts = {
            fact_id: evidence.facts[fact_id].model_dump(mode="json")
            for fact_id in track.fact_refs
            if evidence_is_approved(evidence.facts[fact_id], purpose)
        }
        views[name] = {
            "track": name,
            "label": track.label,
            "resume": track.resume,
            "resume_source": evidence.sources[track.resume].model_dump(mode="json"),
            "target_roles": list(track.target_roles),
            "facts": facts,
        }
    return views


def load_profile(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Copy profile.example.yaml to {path} and fill it in; "
            "matching needs it."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping (basics:, education:, skills:, ...).")
    profile_extensions(raw)
    return raw


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _bullets(item: dict[str, Any]) -> list[str]:
    return [
        t.replace("**", "")  # **bold** marks are for the resume only
        for b in _list(item.get("bullets"))
        if isinstance(b, dict) and (t := _str(b.get("text")))
    ]


def resume_problems(profile: dict[str, Any]) -> list[str]:
    """What stops profile.yaml from being used for resumes: missing name, missing or repeated
    ids on experience, projects and bullets."""
    problems = []
    basics = profile.get("basics")
    if not isinstance(basics, dict) or not _str(basics.get("name")):
        problems.append("basics.name is missing")
    seen: set[str] = set()
    for section in ("experience", "projects"):
        for n, item in enumerate(_list(profile.get(section)), 1):
            if not isinstance(item, dict):
                problems.append(f"{section} #{n} is not a mapping")
                continue
            where = f"{section} #{n}"
            ids = [(where, item.get("id"))] + [
                (f"{where} bullet #{m}", b.get("id") if isinstance(b, dict) else None)
                for m, b in enumerate(_list(item.get("bullets")), 1)
            ]
            for place, value in ids:
                if not _str(value):
                    problems.append(f"{place} needs an id")
                elif str(value) in seen:
                    problems.append(f"id {value!r} is used twice")
                else:
                    seen.add(str(value))
    return problems


def skill_names(profile: dict[str, Any]) -> list[str]:
    """Every skill listed under `skills:`, whether grouped ({languages: [...]}) or flat."""
    skills = profile.get("skills")
    groups = skills.values() if isinstance(skills, dict) else [skills]
    names: list[str] = []
    for group in groups:
        for name in _list(group):
            if (text := _str(name)) and text not in names:
                names.append(text)
    return names


def skill_warnings(profile: dict[str, Any]) -> list[str]:
    """Skills YAML cut in two: in `[a (b, c), d]` the comma inside brackets ends the item,
    so "a (b" and "c)" become two skills that match nothing."""
    return [
        f"skills: {name!r} looks cut at a comma inside brackets; put the whole item in "
        f'quotes, e.g. "Machine Learning (feature engineering, model training)"'
        for name in skill_names(profile)
        if name.count("(") != name.count(")")
    ]


def _graduation_year(education: dict[str, Any]) -> int | None:
    if isinstance(year := education.get("graduation_year"), int):
        return year
    years = re.findall(r"\b(20\d\d)\b", str(education.get("years", "")))
    return int(years[-1]) if years else None


def compact_profile(
    profile: dict[str, Any], filters: FiltersConfig, matching: MatchingConfig
) -> dict[str, Any]:
    """What a match provider sees. Keep this free of contact details."""
    education = [
        {
            "degree": _str(e.get("degree")),
            "branch": _str(e.get("branch")),
            "graduation_year": _graduation_year(e),
            "cgpa": e.get("cgpa"),
        }
        for e in _list(profile.get("education"))
        if isinstance(e, dict)
    ]
    experience = [
        {"title": _str(x.get("title")), "dates": _str(x.get("dates")), "bullets": _bullets(x)}
        for x in _list(profile.get("experience"))
        if isinstance(x, dict)
    ]
    projects = [
        {"name": _str(p.get("name")), "tags": _list(p.get("tags")), "bullets": _bullets(p)}
        for p in _list(profile.get("projects"))
        if isinstance(p, dict)
    ]
    prefs = profile.get("preferences")
    prefs = prefs if isinstance(prefs, dict) else {}
    return {
        "education": education,
        "skills": skill_names(profile),
        "experience": experience,
        "projects": projects,
        "preferences": {
            "target_roles": _list(prefs.get("target_roles")),
            "work_modes": _list(prefs.get("work_modes")),
            "locations": filters.locations or ["anywhere in India", "remote"],
        },
        "rules": {
            "batch_years": filters.batch_years,
            "max_experience_years": filters.max_experience_years,
            "branch_strict": matching.branch_strict,
        },
    }


def profile_version(compact: dict[str, Any]) -> str:
    """Changes whenever anything matching sees changes; jobs are then matched again."""
    return sha1(json.dumps(compact, sort_keys=True, ensure_ascii=False))[:16]


_ITEMS = r"(?P<items>[^\]\n]*)\](?P<tail>[ \t]*(?:#.*)?)$"
_ADDITIONAL = re.compile(r"^(?P<lead>[ \t]+additional:[ \t]*\[)" + _ITEMS, re.MULTILINE)
_FLAT = re.compile(r"^(?P<lead>skills:[ \t]*\[)" + _ITEMS, re.MULTILINE)
_PLAIN_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .+#/&()-]*$")


def add_skill(path: Path, name: str, now: str) -> str:
    """Add one skill to profile.yaml without touching anything else: to the one-line
    `additional: [...]` list under `skills:` (or a one-line flat `skills: [...]`). Keeps a
    backup first (profile.yaml.bak-<now>) and puts it back if the result doesn't read as the
    same profile plus the skill. Returns the backup's name; raises ConfigError otherwise."""
    from jobradar.providers.skills_rule import profile_skill_keys, skill_key

    name = " ".join(name.split())[:60]
    if not name:
        raise ConfigError("no skill name given")
    before = load_profile(path)
    if skill_key(name) in profile_skill_keys(before):
        raise ConfigError(f"{name} is already in your profile")
    text = path.read_text(encoding="utf-8")
    item = name if _PLAIN_ITEM.match(name) and ": " not in name else json.dumps(name)
    for pattern in (_ADDITIONAL, _FLAT):
        found = pattern.search(text)
        if found:
            items = found.group("items").rstrip()
            joined = f"{items}, {item}" if items.strip() else item
            new = (text[: found.start()] + found.group("lead") + joined + "]"
                   + found.group("tail") + text[found.end():])  # fmt: skip
            break
    else:
        raise ConfigError(
            "couldn't find a one-line `additional: [...]` list under skills: in profile.yaml; "
            "add the skill there yourself"
        )
    backup = path.with_name(f"{path.name}.bak-{now}")
    backup.write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    try:
        after = load_profile(path)
        same = {k: v for k, v in after.items() if k != "skills"} == {
            k: v for k, v in before.items() if k != "skills"
        }
        if not same or skill_key(name) not in profile_skill_keys(after):
            raise ConfigError("the edited profile didn't read back as expected")
    except ConfigError:
        path.write_text(text, encoding="utf-8")  # put it back exactly as it was
        backup.unlink(missing_ok=True)
        raise
    return backup.name
