from typing import Any

import pytest

from jobradar.pipeline.requirements import analyze_requirements


def tracks(result: dict[str, Any]) -> dict[str, str]:
    return {item["track"]: item["strength"] for item in result.get("role_tracks", [])}


def requirements(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("requirements")
    assert isinstance(value, dict)
    return value


def test_python_fastapi_internship_is_ai_backend_and_sde() -> None:
    result = analyze_requirements(
        "Software Engineer Intern",
        """Responsibilities:
        Build backend APIs using Python and FastAPI.
        Minimum requirements:
        Currently pursuing a B.Tech degree. No experience required.""",
        None,
    )
    assert tracks(result) == {"ai_backend": "strong", "sde": "strong"}
    assert result["skills_required"] == ["Python", "FastAPI"]
    assert result["experience_min"] == 0
    student = {item["kind"] for item in requirements(result)["student_requirements"]}
    assert {"current_student", "degree_in_progress", "no_experience_required"} <= student


def test_react_node_full_stack_internship_is_multilabel() -> None:
    result = analyze_requirements(
        "Full Stack Developer Intern",
        "Responsibilities:\nDevelop product features using React, Node.js, and Express.js.",
        None,
    )
    assert tracks(result) == {"full_stack": "strong", "sde": "strong"}
    assert result["skills_required"] == ["React", "Node.js", "Express.js"]


def test_react_only_frontend_does_not_invent_node_or_express() -> None:
    result = analyze_requirements(
        "Frontend Developer", "Responsibilities:\nBuild user interfaces in React.", None
    )
    assert tracks(result)["full_stack"] == "strong"
    assert result["skills_required"] == ["React"]
    assert "Node.js" not in str(result) and "Express.js" not in str(result)


def test_node_express_backend_does_not_invent_react() -> None:
    result = analyze_requirements(
        "Backend Developer",
        "Responsibilities:\nBuild backend services with Node.js and Express.js.",
        None,
    )
    assert tracks(result) == {"full_stack": "strong", "sde": "strong"}
    assert result["skills_required"] == ["Node.js", "Express.js"]
    assert "React" not in str(result)


def test_general_sde_needs_no_ai_or_full_stack_keywords() -> None:
    result = analyze_requirements(
        "Graduate Engineer",
        "Responsibilities:\nDesign and develop software systems and product features.",
        None,
    )
    assert tracks(result) == {"sde": "strong"}
    assert "skills_required" not in result


def test_sdet_api_automation_is_strong() -> None:
    result = analyze_requirements(
        "SDET",
        "Required skills: API testing, test automation, and Playwright.",
        None,
    )
    assert tracks(result) == {"sdet": "strong"}
    assert result["skills_required"] == ["API testing", "Test automation", "Playwright"]


def test_manual_only_qa_is_not_strong_sdet() -> None:
    result = analyze_requirements(
        "Manual QA Engineer",
        "Responsibilities:\nPerform manual testing and document defects.",
        None,
    )
    assert tracks(result) == {"sdet": "possible"}
    assert "skills_required" not in result


@pytest.mark.parametrize(
    ("role", "text", "track"),
    [
        (
            "Senior AI Engineer",
            "Requirements:\nPython, RAG, and LLMs are required. "
            "5+ years of professional experience required.",
            "ai_backend",
        ),
        (
            "Senior MERN Developer",
            "Requirements:\nBuild applications with React, Node.js, Express.js, and MongoDB. "
            "5+ years of professional experience required.",
            "full_stack",
        ),
    ],
)
def test_senior_roles_are_classified_and_keep_experience(role: str, text: str, track: str) -> None:
    result = analyze_requirements(role, text, None)
    assert tracks(result)[track] == "strong"
    assert result["experience_min"] == 5
    exp = requirements(result)["experience"]
    assert exp[0]["need"] == "required" and exp[0]["experience_type"] == "professional"


def test_required_preferred_and_evidence_section() -> None:
    result = analyze_requirements(
        "Python Backend Engineer",
        "Requirements:\nRequired: Python and PostgreSQL.\nPreferred qualifications:\nFastAPI preferred.",
        None,
    )
    assert result["skills_required"] == ["Python", "PostgreSQL"]
    assert result["skills_preferred"] == ["FastAPI"]
    req = requirements(result)
    required = req["required_skills"][0]
    preferred = req["preferred_skills"][0]
    assert required["evidence"] == {
        "sentence": "Required: Python and PostgreSQL.",
        "section": "Requirements",
        "source": "page",
    }
    assert preferred["evidence"]["section"] == "Preferred qualifications"


def test_employer_stack_stays_context() -> None:
    result = analyze_requirements(
        "Software Engineer", "About the role:\nWe primarily use Ruby and Go.", None
    )
    context = requirements(result)["employer_technologies"]
    assert context[0]["skills"] == ["Ruby", "Go"]
    assert "skills_required" not in result and "skills_preferred" not in result


def test_alternative_skill_group_is_not_flattened_into_mandatory_skills() -> None:
    result = analyze_requirements("Software Engineer", "Required skills: Python or Java.", None)
    group = requirements(result)["required_skills"][0]
    assert group["skills"] == ["Python", "Java"] and group["relation"] == "any"
    assert "skills_required" not in result


def test_one_or_more_languages_is_an_alternative_group() -> None:
    result = analyze_requirements(
        "Software Engineer Intern",
        "About You:\nProficiency in one or more languages like Java, C++, Scala, Python.",
        None,
    )
    group = requirements(result)["required_skills"][0]
    assert group["skills"] == ["Java", "C++", "Scala", "Python"]
    assert group["relation"] == "any"
    assert "skills_required" not in result


def test_years_of_building_in_requirements_is_professional_experience() -> None:
    result = analyze_requirements(
        "Backend Engineer",
        "What we are looking for:\n4–6 years of building and operating production backend systems.",
        None,
    )
    assert (result["experience_min"], result["experience_max"]) == (4, 6)
    item = requirements(result)["experience"][0]
    assert item["need"] == "required"
    assert item["experience_type"] == "professional"
    assert item["evidence"]["section"] == "What we are looking for"


def test_good_to_have_parenthetical_is_preferred() -> None:
    result = analyze_requirements(
        "Backend Engineer",
        "Good to have (not required):\nProduction experience in Python with FastAPI.",
        None,
    )
    assert result["skills_preferred"] == ["Python", "FastAPI"]
    assert "skills_required" not in result
    preferred = requirements(result)["preferred_skills"][0]
    assert preferred["evidence"]["section"] == "Good to have (not required)"


def test_mandatory_and_preferred_experience_are_separate() -> None:
    result = analyze_requirements(
        "Software Engineer",
        """Minimum requirements:
        3+ years of professional experience required.
        Preferred qualifications:
        5+ years of professional experience preferred.""",
        None,
    )
    assert result["experience_min"] == 3
    exp = requirements(result)["experience"]
    assert [(item["need"], item["minimum_years"]) for item in exp] == [
        ("required", 3.0),
        ("preferred", 5.0),
    ]


def test_internship_and_professional_experience_are_distinct() -> None:
    result = analyze_requirements(
        "Associate Engineer",
        "Internship experience preferred. 1+ years of professional experience required.",
        None,
    )
    exp = requirements(result)["experience"]
    assert [(item["need"], item["experience_type"]) for item in exp] == [
        ("preferred", "internship"),
        ("required", "professional"),
    ]
    assert result["experience_min"] == 1


def test_conflicting_experience_remains_unknown() -> None:
    result = analyze_requirements(
        "Software Engineer",
        "Requirements:\n0-1 years of professional experience.\n"
        "3+ years of professional experience required.",
        None,
    )
    assert requirements(result)["experience_status"] == "conflicting"
    assert "experience_min" not in result and "experience_max" not in result


def test_general_qualifications_are_not_skills() -> None:
    result = analyze_requirements(
        "Graduate Engineer",
        "Minimum requirements:\nAbility to learn unfamiliar systems.\n"
        "Strong written communication skills.",
        None,
    )
    qualifications = requirements(result)["general_qualifications"]
    assert [item["qualification"] for item in qualifications] == [
        "Ability to learn unfamiliar systems.",
        "Strong written communication skills.",
    ]
    assert "skills_required" not in result


def test_missing_structured_skills_stays_empty() -> None:
    result = analyze_requirements(
        "Software Engineer",
        "Responsibilities:\nDesign and maintain reliable software services.",
        None,
    )
    assert tracks(result) == {"sde": "strong"}
    assert "skills_required" not in result and "skills_preferred" not in result


def test_stripe_internship_stack_is_context_not_mandatory() -> None:
    result = analyze_requirements(
        "Software Engineer, Intern",
        """Who you are
        Minimum requirements
        Experience and familiarity with programming, either through side projects or classwork.
        Ability to learn unfamiliar systems and form an understanding of those systems.
        At least 1 year of university education, or equivalent work experience.
        We work mostly in Ruby, Java, Javascript, Go, and Scala.""",
        None,
    )
    req = requirements(result)
    assert req["employer_technologies"][0]["skills"] == [
        "Ruby",
        "Java",
        "JavaScript",
        "Go",
        "Scala",
    ]
    assert "skills_required" not in result
    assert "experience_min" not in result
    assert req["general_qualifications"]
