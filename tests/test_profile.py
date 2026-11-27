from copy import deepcopy
from typing import Any

import pytest

from jobradar.config import ConfigError, FiltersConfig, MatchingConfig
from jobradar.profile import (
    TRACK_NAMES,
    candidate_views,
    compact_profile,
    load_profile,
    profile_extensions,
    profile_version,
    skill_names,
)

PROFILE: dict[str, Any] = {
    "basics": {"name": "Asha", "email": "asha@example.com", "phone": "+91-90000-00000"},
    "summary": "About me",
    "education": [{"degree": "B.Tech", "branch": "Electrical Engineering",
                   "years": "Nov 2022 - Jun 2026", "cgpa": 7.5}],
    "skills": {"languages": ["Python", "SQL"], "backend": ["FastAPI", "Python"]},
    "experience": [{"id": "x", "title": "Intern", "company": "Acme", "dates": "2025",
                    "bullets": [{"id": "b1", "text": "Built an API", "tags": ["python"]}]}],
    "projects": [{"id": "p", "name": "JobRadar", "tags": ["llm"],
                  "bullets": [{"id": "p1", "text": "Agent"}]}],
    "preferences": {"target_roles": ["Backend Engineer"], "work_modes": ["remote"]},
    "answers": {"expected_ctc": "6 LPA"},
}  # fmt: skip

EXTENDED: dict[str, Any] = PROFILE | {
    "evidence": {
        "sources": {
            "overall": {"kind": "resume", "file": "Resume_overall.pdf", "view": "overall"},
            "ai": {"kind": "resume", "file": "Resume_ai.pdf", "view": "ai_backend"},
            "full_stack": {
                "kind": "resume",
                "file": "Resume_full_stack.pdf",
                "view": "full_stack",
            },
            "sdet": {"kind": "resume", "file": "Resume_sdet.pdf", "view": "sdet"},
            "repository": {"kind": "repository", "file": "tests"},
            "user": {"kind": "user"},
        },
        "facts": {
            "skill.python": {
                "value": "Python",
                "status": "resume_supported",
                "category": "skill",
                "sources": ["overall", "ai"],
                "evidence_refs": ["internship"],
                "resume_safe": True,
            },
            "skill.pytest": {
                "value": "Pytest",
                "status": "independently_verified",
                "category": "skill",
                "sources": ["sdet", "repository"],
                "resume_safe": True,
            },
            "metric.accuracy": {
                "value": "84.6%",
                "status": "unverified",
                "category": "metric",
                "sources": ["ai", "sdet"],
                "resume_safe": False,
            },
            "skill.java": {
                "value": "Java",
                "status": "unverified",
                "category": "skill",
                "sources": ["full_stack", "sdet"],
                "resume_safe": False,
            },
            "preference.remote": {
                "value": "Remote",
                "status": "user_confirmed",
                "category": "preference",
                "sources": ["user"],
                "resume_safe": False,
            },
        },
    },
    "career_tracks": {
        "ai_backend": {
            "label": "AI and Python backend",
            "resume": "ai",
            "target_roles": ["AI Engineer"],
            "fact_refs": ["skill.python", "metric.accuracy", "skill.java"],
        },
        "full_stack": {
            "label": "Full-stack development",
            "resume": "full_stack",
            "target_roles": ["Full Stack Engineer"],
            "fact_refs": ["skill.python", "skill.java"],
        },
        "sde": {
            "label": "General software engineering",
            "resume": "overall",
            "target_roles": ["Software Engineer"],
            "fact_refs": ["skill.python", "preference.remote"],
        },
        "sdet": {
            "label": "SDET and QA automation",
            "resume": "sdet",
            "target_roles": ["SDET"],
            "fact_refs": ["skill.python", "skill.pytest", "metric.accuracy"],
        },
    },
}


def compact(**filters: object) -> dict[str, Any]:
    return compact_profile(
        PROFILE, FiltersConfig.model_validate(filters), MatchingConfig(branch_strict=True)
    )


def test_compact_profile_keeps_contact_details_out() -> None:
    view = compact(batch_years=[2025, 2026])
    text = str(view)
    for private in ("Asha", "asha@example.com", "90000", "About me", "6 LPA"):
        assert private not in text
    assert view["education"] == [
        {"degree": "B.Tech", "branch": "Electrical Engineering", "graduation_year": 2026,
         "cgpa": 7.5}
    ]  # fmt: skip
    assert view["skills"] == ["Python", "SQL", "FastAPI"]
    assert view["preferences"]["locations"] == ["anywhere in India", "remote"]
    assert view["rules"] == {
        "batch_years": [2025, 2026], "max_experience_years": None, "branch_strict": True,
    }  # fmt: skip


def test_version_follows_what_matching_sees() -> None:
    assert profile_version(compact()) == profile_version(compact())
    assert profile_version(compact()) != profile_version(compact(locations=["Pune"]))


def test_skill_names_flat_or_grouped() -> None:
    assert skill_names({"skills": ["Go", "Go", "Rust"]}) == ["Go", "Rust"]
    assert skill_names({}) == []


def test_load_profile_errors(tmp_path: object) -> None:
    from pathlib import Path

    folder = Path(str(tmp_path))
    with pytest.raises(ConfigError, match="not found"):
        load_profile(folder / "missing.yaml")
    (folder / "bad.yaml").write_text("- a list\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_profile(folder / "bad.yaml")


def test_legacy_profile_keeps_compact_behavior_and_has_no_views() -> None:
    assert candidate_views(PROFILE) == {}
    assert compact(batch_years=[2025, 2026]) == {
        "education": [
            {
                "degree": "B.Tech",
                "branch": "Electrical Engineering",
                "graduation_year": 2026,
                "cgpa": 7.5,
            },
        ],
        "skills": ["Python", "SQL", "FastAPI"],
        "experience": [{"title": "Intern", "dates": "2025", "bullets": ["Built an API"]}],
        "projects": [{"name": "JobRadar", "tags": ["llm"], "bullets": ["Agent"]}],
        "preferences": {
            "target_roles": ["Backend Engineer"],
            "work_modes": ["remote"],
            "locations": ["anywhere in India", "remote"],
        },
        "rules": {
            "batch_years": [2025, 2026],
            "max_experience_years": None,
            "branch_strict": True,
        },
    }


def test_four_candidate_views_use_references_without_copying_facts() -> None:
    extensions = profile_extensions(EXTENDED)
    assert extensions is not None
    views = candidate_views(EXTENDED)
    assert tuple(views) == TRACK_NAMES
    assert views["ai_backend"]["resume"] == "ai"
    assert views["sdet"]["target_roles"] == ["SDET"]
    assert views["ai_backend"]["facts"]["skill.python"]["value"] == "Python"
    assert "metric.accuracy" not in views["ai_backend"]["facts"]
    assert "skill.java" not in views["full_stack"]["facts"]


def test_resume_views_only_include_approved_resume_safe_facts() -> None:
    matching = candidate_views(EXTENDED, "matching")
    resume = candidate_views(EXTENDED, "resume")
    assert "preference.remote" in matching["sde"]["facts"]
    assert "preference.remote" not in resume["sde"]["facts"]
    assert set(resume["sdet"]["facts"]) == {"skill.python", "skill.pytest"}
    assert "metric.accuracy" not in resume["sdet"]["facts"]


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (
            ("evidence", "facts", "metric.accuracy", "resume_safe"),
            True,
            "cannot be resume_safe",
        ),
        (
            ("evidence", "facts", "skill.python", "sources"),
            ["missing"],
            "unknown sources",
        ),
        (
            ("evidence", "facts", "skill.python", "sources"),
            ["user"],
            "resume_supported but has no resume source",
        ),
        (
            ("evidence", "facts", "preference.remote", "sources"),
            ["overall"],
            "user_confirmed but has no user source",
        ),
        (("career_tracks", "sdet", "resume"), "missing", "unknown source"),
        (("career_tracks", "sdet", "fact_refs"), ["missing"], "unknown fact_refs"),
    ],
)
def test_invalid_evidence_is_rejected(path: tuple[str, ...], value: Any, message: str) -> None:
    profile = deepcopy(EXTENDED)
    node: dict[str, Any] = profile
    for key in path[:-1]:
        child = node[key]
        assert isinstance(child, dict)
        node = child
    node[path[-1]] = value
    with pytest.raises(ConfigError, match=message):
        profile_extensions(profile)


def test_repeated_resume_claims_are_not_independent_verification() -> None:
    profile = deepcopy(EXTENDED)
    fact = profile["evidence"]["facts"]["metric.accuracy"]
    fact["status"] = "independently_verified"
    with pytest.raises(ConfigError, match="repeated resume claims are not independent"):
        profile_extensions(profile)


def test_rejected_facts_never_enter_candidate_views() -> None:
    profile = deepcopy(EXTENDED)
    fact = profile["evidence"]["facts"]["skill.python"]
    fact["status"] = "rejected"
    fact["resume_safe"] = False
    for purpose in ("matching", "resume"):
        assert all(
            "skill.python" not in view["facts"]
            for view in candidate_views(profile, purpose).values()
        )


def test_all_four_tracks_are_required_once_extensions_are_enabled() -> None:
    profile = deepcopy(EXTENDED)
    del profile["career_tracks"]["sdet"]
    with pytest.raises(ConfigError, match="missing sdet"):
        profile_extensions(profile)
