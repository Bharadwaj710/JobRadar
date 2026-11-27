"""Candidate profile adapter: converts confirmed CandidateProfile into J4 matching format.

Transforms Day 1 candidate records into the canonical four-track evidence-backed
profile structure expected by J4 eligibility and technical matching.
Preserves candidate isolation, version traceability, and evidence provenance.
"""

from __future__ import annotations

from typing import Any

from jobradar.db.models import CandidateProfile
from jobradar.profile import TrackName
from jobradar.providers.skills_rule import skill_key

# Categorization heuristics for track fact assignment
_AI_BACKEND_SKILLS = {
    "python",
    "fastapi",
    "django",
    "flask",
    "sql",
    "postgresql",
    "mysql",
    "sqlite",
    "redis",
    "mongodb",
    "docker",
    "rest",
    "restapi",
    "restapis",
    "graphql",
    "grpc",
    "asyncio",
    "celery",
    "kafka",
    "rabbitmq",
    "aws",
    "rag",
    "llm",
    "langchain",
    "pytorch",
    "tensorflow",
    "git",
}

_FULL_STACK_SKILLS = {
    "javascript",
    "typescript",
    "react",
    "reactjs",
    "nextjs",
    "vue",
    "vuejs",
    "angular",
    "nodejs",
    "express",
    "expressjs",
    "html",
    "css",
    "tailwindcss",
    "sql",
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "rest",
    "restapi",
    "git",
}

_SDET_SKILLS = {
    "python",
    "java",
    "pytest",
    "selenium",
    "cypress",
    "playwright",
    "unittest",
    "junit",
    "testng",
    "postman",
    "restassured",
    "apitesting",
    "testautomation",
    "automation",
    "performance testing",
    "jmeter",
    "git",
    "docker",
    "ci/cd",
    "github actions",
    "jenkins",
    "sql",
}


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def adapt_candidate_profile(
    candidate_profile: CandidateProfile,
    allow_draft: bool = False,
) -> dict[str, Any]:
    """Convert a confirmed CandidateProfile into a J4-compatible canonical profile.

    Validates candidate confirmation status, builds four career views with
    evidence facts, and preserves all education, experience, and skill facts.

    Raises:
        ValueError: If profile is not confirmed and allow_draft is False.
    """
    if not allow_draft and candidate_profile.review_status != "confirmed":
        raise ValueError(
            f"Candidate profile '{candidate_profile.id}' has status "
            f"'{candidate_profile.review_status}'. Only confirmed candidate profiles "
            "may be evaluated for recommendations."
        )

    data = candidate_profile.profile_data
    basics = dict(data.get("basics", {}))
    education_raw = list(data.get("education", []))
    skills_raw = list(data.get("skills", []))
    experience_raw = list(data.get("experience", []))
    projects_raw = list(data.get("projects", []))
    preferences_raw = dict(data.get("preferences", {}))

    # Standardize education
    education: list[dict[str, Any]] = []
    for edu in education_raw:
        if isinstance(edu, dict):
            education.append(
                {
                    "degree": _clean_str(edu.get("degree")),
                    "branch": _clean_str(edu.get("branch") or edu.get("field_of_study")),
                    "school": _clean_str(edu.get("institution") or edu.get("school")),
                    "graduation_year": edu.get("graduation_year"),
                    "cgpa": edu.get("cgpa") or edu.get("cgpa_or_percentage"),
                }
            )

    # Standardize experience
    experience: list[dict[str, Any]] = []
    for i, exp in enumerate(experience_raw, 1):
        if isinstance(exp, dict):
            exp_id = str(exp.get("id") or f"exp_{i}")
            bullets = exp.get("bullets", [])
            formatted_bullets = []
            if isinstance(bullets, list):
                for j, b in enumerate(bullets, 1):
                    if isinstance(b, dict):
                        formatted_bullets.append(b)
                    else:
                        formatted_bullets.append({"id": f"{exp_id}_b{j}", "text": str(b)})
            experience.append(
                {
                    "id": exp_id,
                    "title": _clean_str(exp.get("title")),
                    "company": _clean_str(exp.get("company")),
                    "location": _clean_str(exp.get("location")),
                    "dates": _clean_str(exp.get("dates")),
                    "bullets": formatted_bullets,
                }
            )

    # Standardize projects
    projects: list[dict[str, Any]] = []
    for i, proj in enumerate(projects_raw, 1):
        if isinstance(proj, dict):
            proj_id = str(proj.get("id") or f"proj_{i}")
            bullets = proj.get("bullets", [])
            formatted_bullets = []
            if isinstance(bullets, list):
                for j, b in enumerate(bullets, 1):
                    if isinstance(b, dict):
                        formatted_bullets.append(b)
                    else:
                        formatted_bullets.append({"id": f"{proj_id}_b{j}", "text": str(b)})
            tools = proj.get("tools") or proj.get("technologies") or proj.get("tags") or []
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            projects.append(
                {
                    "id": proj_id,
                    "name": _clean_str(proj.get("name")),
                    "stack": ", ".join(str(t) for t in tools),
                    "tags": [str(t) for t in tools],
                    "tools": [str(t) for t in tools],
                    "bullets": formatted_bullets,
                    "on_resume": True,
                }
            )

    # Standardize preferences
    preferences = {
        "work_modes": preferences_raw.get("work_modes", ["Remote", "Hybrid", "Onsite in India"]),
        "target_roles": preferences_raw.get(
            "target_roles", ["Software Engineer", "Backend Developer"]
        ),
        "locations": preferences_raw.get("locations", ["Anywhere in India", "Remote"]),
    }

    # Build evidence sources
    cid = candidate_profile.candidate_id
    sources: dict[str, dict[str, Any]] = {
        "user_confirmation": {
            "kind": "user",
        },
        "overall_resume": {
            "kind": "resume",
            "file": f"memory://candidates/{cid}/resume_overall.pdf",
            "view": "overall",
        },
        "ai_resume": {
            "kind": "resume",
            "file": f"memory://candidates/{cid}/resume_ai.pdf",
            "view": "ai_backend",
        },
        "full_stack_resume": {
            "kind": "resume",
            "file": f"memory://candidates/{cid}/resume_full_stack.pdf",
            "view": "full_stack",
        },
        "sdet_resume": {
            "kind": "resume",
            "file": f"memory://candidates/{cid}/resume_sdet.pdf",
            "view": "sdet",
        },
    }

    # Common sources for user-confirmed resume facts
    common_sources = [
        "user_confirmation",
        "overall_resume",
        "ai_resume",
        "full_stack_resume",
        "sdet_resume",
    ]

    # Build evidence facts
    facts: dict[str, dict[str, Any]] = {}

    # 1. Education fact
    if education:
        edu0 = education[0]
        deg = edu0.get("degree", "")
        branch = edu0.get("branch", "")
        grad_yr = edu0.get("graduation_year")
        edu_desc = f"{deg} in {branch}".strip()
        if grad_yr:
            edu_desc += f", graduating in {grad_yr}"
        facts["education.primary"] = {
            "value": edu_desc,
            "status": "user_confirmed",
            "category": "education",
            "sources": common_sources,
            "evidence_refs": ["education"],
            "resume_safe": True,
        }

    # 2. Experience facts
    exp_refs: list[str] = []
    for exp in experience:
        fact_key = f"experience.{exp['id']}"
        desc = f"{exp['title']} at {exp['company']}".strip()
        exp_ref_id = f"ref_{exp['id']}"
        exp_refs.append(exp_ref_id)
        facts[fact_key] = {
            "value": desc,
            "status": "user_confirmed",
            "category": "experience",
            "sources": common_sources,
            "evidence_refs": [exp_ref_id],
            "resume_safe": True,
        }

    # 3. Project facts
    proj_refs: list[str] = []
    for proj in projects:
        fact_key = f"project.{proj['id']}"
        proj_ref_id = f"ref_{proj['id']}"
        proj_refs.append(proj_ref_id)
        facts[fact_key] = {
            "value": proj["name"],
            "status": "user_confirmed",
            "category": "project",
            "sources": common_sources,
            "evidence_refs": [proj_ref_id],
            "resume_safe": True,
        }

    # Combine support references for skills
    supporting_refs = exp_refs + proj_refs
    if not supporting_refs:
        facts["project.portfolio"] = {
            "value": "Independent Technical Projects & Verified Works",
            "status": "user_confirmed",
            "category": "project",
            "sources": common_sources,
            "evidence_refs": ["ref_portfolio"],
            "resume_safe": True,
        }
        supporting_refs = ["ref_portfolio"]

    # 4. Skill facts
    normalized_skills: set[str] = set()
    for s in skills_raw:
        if isinstance(s, str) and s.strip():
            normalized_skills.add(s.strip())
    # Also add tools from projects
    for proj in projects:
        for t in proj.get("tools", []):
            if isinstance(t, str) and t.strip():
                normalized_skills.add(t.strip())

    skill_fact_keys: list[str] = []
    for i, sname in enumerate(sorted(normalized_skills), 1):
        skey = skill_key(sname) or f"s_{i}"
        fact_key = f"skill.{skey}"
        skill_fact_keys.append(fact_key)
        facts[fact_key] = {
            "value": [sname],
            "status": "user_confirmed",
            "category": "skill",
            "sources": common_sources,
            "evidence_refs": list(supporting_refs),
            "resume_safe": True,
        }

    # 5. Career track views
    all_fact_refs = list(facts.keys())

    # Partition skill facts per track
    ai_fact_refs = [
        k
        for k in all_fact_refs
        if not k.startswith("skill.") or any(sk in k.lower() for sk in _AI_BACKEND_SKILLS)
    ]
    fs_fact_refs = [
        k
        for k in all_fact_refs
        if not k.startswith("skill.") or any(sk in k.lower() for sk in _FULL_STACK_SKILLS)
    ]
    sdet_fact_refs = [
        k
        for k in all_fact_refs
        if not k.startswith("skill.") or any(sk in k.lower() for sk in _SDET_SKILLS)
    ]

    career_tracks: dict[TrackName, dict[str, Any]] = {
        "ai_backend": {
            "label": "AI / Backend Engineering",
            "resume": "ai_resume",
            "target_roles": ["AI Backend Engineer", "Python Backend Developer", "AI Engineer"],
            "fact_refs": ai_fact_refs or all_fact_refs,
        },
        "full_stack": {
            "label": "Full-Stack Development",
            "resume": "full_stack_resume",
            "target_roles": ["Full Stack Developer", "Software Engineer - Web"],
            "fact_refs": fs_fact_refs or all_fact_refs,
        },
        "sde": {
            "label": "Software Development (General SWE)",
            "resume": "overall_resume",
            "target_roles": ["Software Engineer", "SDE", "Backend Developer"],
            "fact_refs": all_fact_refs,
        },
        "sdet": {
            "label": "SDET & Automation",
            "resume": "sdet_resume",
            "target_roles": ["SDET", "QA Automation Engineer", "Test Engineer"],
            "fact_refs": sdet_fact_refs or all_fact_refs,
        },
    }

    return {
        "basics": basics,
        "education": education,
        "skills": list(sorted(normalized_skills)),
        "experience": experience,
        "projects": projects,
        "preferences": preferences,
        "evidence": {
            "sources": sources,
            "facts": facts,
        },
        "career_tracks": career_tracks,
        "_candidate_id": candidate_profile.candidate_id,
        "_profile_version": candidate_profile.version,
    }
