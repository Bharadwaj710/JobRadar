from datetime import date
from typing import Any, Literal

import pytest

from jobradar.pipeline.eligibility import EligibilityState
from jobradar.pipeline.schemas import JobPosting
from jobradar.pipeline.technical_match import assess_job
from jobradar.profile import TRACK_NAMES
from jobradar.providers.base import JobView
from jobradar.providers.skills_rule import SkillsRule


def fact(
    value: Any,
    category: str,
    source: str,
    *refs: str,
    status: str = "resume_supported",
) -> dict[str, Any]:
    return {
        "value": value,
        "status": status,
        "category": category,
        "sources": [source],
        "evidence_refs": list(refs),
        "resume_safe": status not in ("unverified", "rejected"),
    }


PROFILE: dict[str, Any] = {
    "basics": {"location": "Visakhapatnam, Andhra Pradesh, India"},
    "education": [
        {
            "degree": "B.Tech",
            "branch": "Computer Science and Engineering",
            "graduation_year": 2027,
        }
    ],
    "experience": [
        {"title": "AI Backend Engineer Intern", "company": "Example AI"},
        {"title": "Full-Stack Developer Intern", "company": "Example Web"},
        {"title": "Software Test Engineer Intern", "company": "Example Test"},
    ],
    "projects": [
        {"name": "Nexus AI", "tags": ["python", "fastapi", "rag"]},
        {"name": "Converge", "tags": ["react", "nodejs", "expressjs"]},
        {"name": "Automation Lab", "tags": ["pytest", "api-testing"]},
    ],
    "preferences": {"work_modes": ["Remote", "Hybrid", "Onsite in India"]},
    "evidence": {
        "sources": {
            "overall_resume": {
                "kind": "resume",
                "file": r"D:\My-Resumes\Resume_bharadwaj.pdf",
                "view": "overall",
            },
            "ai_resume": {
                "kind": "resume",
                "file": r"D:\My-Resumes\ResumeAI.pdf",
                "view": "ai_backend",
            },
            "full_stack_resume": {
                "kind": "resume",
                "file": r"D:\My-Resumes\ResumeFS.pdf",
                "view": "full_stack",
            },
            "sdet_resume": {
                "kind": "resume",
                "file": r"D:\My-Resumes\Resume_T.pdf",
                "view": "sdet",
            },
        },
        "facts": {
            "education.btech": fact(
                "B.Tech CSE, graduating in 2027", "education", "overall_resume", "education"
            ),
            "experience.ai": fact(
                "AI Backend Engineer Intern at Example AI", "experience", "ai_resume", "ai-intern"
            ),
            "experience.web": fact(
                "Full-Stack Developer Intern at Example Web",
                "experience",
                "full_stack_resume",
                "web-intern",
            ),
            "experience.test": fact(
                "Software Test Engineer Intern at Example Test",
                "experience",
                "sdet_resume",
                "test-intern",
            ),
            "project.ai": fact("Nexus AI", "project", "ai_resume", "ai-project"),
            "project.web": fact("Converge", "project", "full_stack_resume", "web-project"),
            "project.test": fact("Automation Lab", "project", "sdet_resume", "test-project"),
            "skills.python": fact(
                ["Python", "FastAPI"],
                "skill",
                "ai_resume",
                "ai-project",
                "ai-intern",
            ),
            "skills.data": fact(["PostgreSQL", "SQL"], "skill", "ai_resume", "ai-project"),
            "skills.frontend": fact(
                ["React", "Next.js"], "skill", "full_stack_resume", "web-project"
            ),
            "skills.javascript_backend": fact(
                ["JavaScript", "Node.js", "Express.js"],
                "skill",
                "full_stack_resume",
                "web-project",
                "web-intern",
            ),
            "skills.testing": fact(
                ["Pytest", "API testing", "Test automation"],
                "skill",
                "sdet_resume",
                "test-project",
                "test-intern",
            ),
            "skills.git": fact("Git", "skill", "overall_resume", "web-project"),
            "skills.claim_only": fact("Django", "skill", "overall_resume"),
            "skill.java": fact(
                "Java",
                "skill",
                "overall_resume",
                "ai-project",
                status="unverified",
            ),
        },
    },
    "career_tracks": {
        "ai_backend": {
            "label": "AI / Backend",
            "resume": "ai_resume",
            "target_roles": ["AI Engineer", "Python Backend Engineer"],
            "fact_refs": [
                "education.btech",
                "experience.ai",
                "project.ai",
                "skills.python",
                "skills.data",
                "skill.java",
            ],
        },
        "full_stack": {
            "label": "Full Stack",
            "resume": "full_stack_resume",
            "target_roles": ["Full Stack Developer"],
            "fact_refs": [
                "education.btech",
                "experience.web",
                "project.web",
                "skills.frontend",
                "skills.javascript_backend",
                "skill.java",
            ],
        },
        "sde": {
            "label": "Software Development",
            "resume": "overall_resume",
            "target_roles": ["Software Engineer", "SDE"],
            "fact_refs": [
                "education.btech",
                "experience.ai",
                "experience.web",
                "project.ai",
                "project.web",
                "skills.python",
                "skills.frontend",
                "skills.javascript_backend",
                "skills.git",
                "skills.claim_only",
                "skill.java",
            ],
        },
        "sdet": {
            "label": "SDET",
            "resume": "sdet_resume",
            "target_roles": ["SDET", "QA Automation Engineer"],
            "fact_refs": [
                "education.btech",
                "experience.test",
                "project.test",
                "skills.testing",
                "skills.python",
                "skill.java",
            ],
        },
    },
}


def evidence(sentence: str, section: str = "Minimum requirements") -> dict[str, str]:
    return {"sentence": sentence, "section": section, "source": "page"}


def skill_group(
    *skills: str, relation: Literal["all", "any"] = "all", sentence: str | None = None
) -> dict[str, Any]:
    wording = sentence or f"Required skills: {' or '.join(skills)}."
    return {"skills": list(skills), "relation": relation, "evidence": evidence(wording)}


def role_track(track: str, strength: str = "strong") -> dict[str, Any]:
    return {
        "track": track,
        "strength": strength,
        "evidence": [evidence(f"The responsibilities support the {track} track.", "Role")],
    }


def posting(
    *,
    tracks: list[tuple[str, str]] | None = None,
    required: list[dict[str, Any]] | None = None,
    preferred: list[dict[str, Any]] | None = None,
    role: str = "Software Engineer Intern",
    batch_years: list[int] | None = None,
    include_basics: bool = True,
    requirements_extra: dict[str, Any] | None = None,
) -> JobPosting:
    requirements: dict[str, Any] = {
        "required_skills": required or [],
        "preferred_skills": preferred or [],
    }
    if include_basics:
        requirements.update(
            {
                "experience": [
                    {
                        "need": "required",
                        "experience_type": "professional",
                        "minimum_years": 0,
                        "maximum_years": 1,
                        "evidence": evidence("0-1 years of professional experience."),
                    }
                ],
                "experience_status": "known",
            }
        )
    if requirements_extra:
        requirements.update(requirements_extra)
    return JobPosting.model_validate(
        {
            "company": "Example",
            "role": role,
            "employment_type": "internship",
            "batch_years": [2027] if batch_years is None else batch_years,
            "degrees": ["B.Tech", "BE"] if include_basics else [],
            "locations": ["Bengaluru, India"] if include_basics else [],
            "work_mode": "onsite" if include_basics else "unknown",
            "role_tracks": [role_track(*item) for item in (tracks or [("sde", "strong")])],
            "requirements": requirements,
        }
    )


AS_OF = date(2026, 11, 17)


def test_ai_backend_matching_uses_approved_project_and_internship_evidence() -> None:
    job = posting(
        tracks=[("ai_backend", "strong"), ("sde", "possible")],
        required=[skill_group("Python", "FastAPI")],
        preferred=[skill_group("PostgreSQL", sentence="PostgreSQL is preferred.")],
        role="AI Backend Engineer Intern",
    )
    result = assess_job(job, PROFILE, AS_OF)
    match = result.tracks["ai_backend"]
    assert match.status == "scored" and match.score == 100
    assert match.matched_skills == ["Python", "FastAPI", "PostgreSQL"]
    assert match.relevant_projects == ["Nexus AI"]
    assert match.relevant_internships == ["AI Backend Engineer Intern at Example AI"]
    assert match.recommended_resume == "ResumeAI.pdf"
    assert result.eligibility.state == EligibilityState.ELIGIBLE


def test_react_node_full_stack_matching_keeps_technologies_distinct() -> None:
    job = posting(
        tracks=[("full_stack", "strong"), ("sde", "possible")],
        required=[skill_group("React", "Node.js", "Express.js", "JavaScript")],
        role="Full Stack Developer Intern",
    )
    match = assess_job(job, PROFILE, AS_OF).tracks["full_stack"]
    assert match.status == "scored"
    assert match.matched_skills == ["React", "Node.js", "Express.js", "JavaScript"]
    assert match.missing_required_skills == []
    assert match.relevant_projects == ["Converge"]
    assert match.recommended_resume == "ResumeFS.pdf"


def test_react_only_frontend_does_not_assume_node_or_express() -> None:
    job = posting(
        tracks=[("full_stack", "possible")],
        required=[skill_group("React")],
        role="Frontend Developer Intern",
    )
    match = assess_job(job, PROFILE, AS_OF).tracks["full_stack"]
    assert match.matched_skills == ["React"]
    assert "Node.js" not in match.matched_skills and "Express.js" not in match.matched_skills


def test_node_express_backend_does_not_assume_react() -> None:
    job = posting(
        tracks=[("full_stack", "possible"), ("sde", "possible")],
        required=[skill_group("Node.js", "Express.js")],
        role="Node.js Backend Developer Intern",
    )
    match = assess_job(job, PROFILE, AS_OF).tracks["full_stack"]
    assert match.matched_skills == ["Node.js", "Express.js"]
    assert "React" not in match.matched_skills


def test_general_sde_matches_without_ai_keywords() -> None:
    job = posting(
        tracks=[("sde", "strong")],
        required=[skill_group("Git")],
        role="Graduate Software Engineer",
    )
    result = assess_job(job, PROFILE, AS_OF)
    assert result.tracks["sde"].status == "scored"
    assert result.tracks["sde"].components["role_alignment"].value == 1
    assert result.tracks["ai_backend"].components["role_alignment"].value == 0
    assert result.tracks["ai_backend"].status == "unscored"
    assert (
        result.tracks["ai_backend"].unscored_reason
        == "the J4-B role taxonomy does not support this track"
    )


def test_sdet_automation_matches_testing_evidence() -> None:
    job = posting(
        tracks=[("sdet", "strong")],
        required=[skill_group("Pytest", "API testing", "Test automation")],
        role="SDET Automation Intern",
    )
    match = assess_job(job, PROFILE, AS_OF).tracks["sdet"]
    assert match.status == "scored" and match.score == 100
    assert match.relevant_projects == ["Automation Lab"]
    assert match.relevant_internships == ["Software Test Engineer Intern at Example Test"]
    assert match.recommended_resume == "Resume_T.pdf"


def test_one_job_can_have_multiple_independent_track_results() -> None:
    job = posting(
        tracks=[("ai_backend", "strong"), ("sde", "strong")],
        required=[skill_group("Python", "FastAPI")],
        role="Applied AI Software Engineer Intern",
    )
    result = assess_job(job, PROFILE, AS_OF)
    assert set(result.tracks) == set(TRACK_NAMES)
    assert result.tracks["ai_backend"].score is not None
    assert result.tracks["sde"].score is not None
    assert result.tracks["ai_backend"].track != result.tracks["sde"].track


def test_alternative_skill_group_needs_only_one_approved_option() -> None:
    job = posting(
        tracks=[("sde", "strong")],
        required=[skill_group("Python", "Java", relation="any")],
    )
    match = assess_job(job, PROFILE, AS_OF).tracks["sde"]
    assert match.components["required_skills"].value == 1
    assert match.matched_skills == ["Python"]
    assert match.missing_required_skills == []


def test_missing_required_skill_is_reported_without_fabrication() -> None:
    job = posting(tracks=[("sde", "strong")], required=[skill_group("Python", "Kafka")])
    match = assess_job(job, PROFILE, AS_OF).tracks["sde"]
    assert match.components["required_skills"].value == 0.5
    assert match.matched_skills == ["Python"]
    assert match.missing_required_skills == ["Kafka"]


def test_preferred_only_skill_is_visible_but_cannot_unlock_a_score() -> None:
    job = posting(
        tracks=[("ai_backend", "strong")],
        required=[],
        preferred=[skill_group("FastAPI", sentence="FastAPI is preferred.")],
    )
    match = assess_job(job, PROFILE, AS_OF).tracks["ai_backend"]
    assert match.matched_skills == ["FastAPI"]
    assert match.components["preferred_skills"].value == 1
    assert match.status == "unscored"
    assert match.unscored_reason == "required technical skills are not stated"


@pytest.mark.parametrize("skill", ["Java", "Django"])
def test_unverified_or_unsupported_candidate_skill_cannot_match(skill: str) -> None:
    job = posting(tracks=[("sde", "strong")], required=[skill_group(skill)])
    match = assess_job(job, PROFILE, AS_OF).tracks["sde"]
    assert match.matched_skills == []
    assert match.missing_required_skills == [skill]
    assert match.components["required_skills"].value == 0
    assert "unverified" not in match.evidence_quality


def test_insufficient_evidence_coverage_stays_unscored() -> None:
    job = posting(
        tracks=[("ai_backend", "strong")],
        required=[skill_group("Python")],
        batch_years=[],
        include_basics=False,
    )
    match = assess_job(job, PROFILE, AS_OF).tracks["ai_backend"]
    assert match.evidence_coverage == 0.65
    assert match.score is None and match.status == "unscored"
    assert match.unscored_reason == "evidence coverage 65% is below the required 70%"


def test_each_track_recommends_its_configured_resume() -> None:
    result = assess_job(
        posting(tracks=[("sde", "strong")], required=[skill_group("Git")]), PROFILE, AS_OF
    )
    assert {name: item.recommended_resume for name, item in result.tracks.items()} == {
        "ai_backend": "ResumeAI.pdf",
        "full_stack": "ResumeFS.pdf",
        "sde": "Resume_bharadwaj.pdf",
        "sdet": "Resume_T.pdf",
    }


def test_not_eligible_does_not_erase_a_defensible_technical_score() -> None:
    job = posting(
        tracks=[("ai_backend", "strong")],
        required=[skill_group("Python", "FastAPI")],
        batch_years=[2025],
        role="AI Backend Engineer",
    )
    result = assess_job(job, PROFILE, AS_OF)
    match = result.tracks["ai_backend"]
    assert result.eligibility.state == EligibilityState.NOT_ELIGIBLE
    assert match.score is not None and match.status == "scored"
    assert "eligibility remains separately NOT_ELIGIBLE" in match.explanation


def test_stripe_context_does_not_become_mandatory_skills_or_a_score() -> None:
    stripe = JobPosting.model_validate(
        {
            "company": "Stripe",
            "role": "Software Engineer, Intern",
            "employment_type": "internship",
            "locations": ["Bengaluru, India"],
            "work_mode": "onsite",
            "role_tracks": [role_track("sde", "possible")],
            "skills_required": [],
            "requirements": {
                "employer_technologies": [
                    skill_group(
                        "Ruby",
                        "Java",
                        "JavaScript",
                        "Go",
                        "Scala",
                        sentence="We work mostly in Ruby, Java, JavaScript, Go, and Scala.",
                    )
                ]
            },
        }
    )
    result = assess_job(stripe, PROFILE, AS_OF)
    match = result.tracks["sde"]
    assert result.eligibility.state == EligibilityState.NEEDS_VERIFICATION
    assert match.status == "unscored" and match.score is None
    assert match.unscored_reason == "required technical skills are not stated"
    assert match.matched_skills == [] and match.missing_required_skills == []


@pytest.mark.asyncio
async def test_legacy_matching_provider_remains_compatible() -> None:
    legacy = {
        "basics": {"location": "India"},
        "education": [{"degree": "B.Tech", "graduation_year": 2027}],
        "skills": ["Python"],
        "projects": [],
    }
    job = JobPosting(skills_required=["Python"])
    assessment = assess_job(job, legacy, AS_OF)
    assert set(assessment.tracks) == set(TRACK_NAMES)
    assert all(item.status == "unscored" for item in assessment.tracks.values())

    answers = await SkillsRule().match(JobView(job, "", ""), legacy)
    assert answers.skills_coverage == 1
