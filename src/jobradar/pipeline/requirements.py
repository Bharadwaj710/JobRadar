"""Deterministic role taxonomy and evidence-backed requirements extraction.

The rules are deliberately conservative. A technology becomes required only when the job
asks the candidate to use or know it; a company's stack stays context. Every structured
conclusion keeps the exact sentence and section that caused it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from jobradar.pipeline.schemas import (
    ExperienceRequirement,
    GeneralQualification,
    JobRequirements,
    RequirementEvidence,
    RoleTrack,
    SkillRequirement,
    StudentRequirement,
)
from jobradar.pipeline.values import experience_in_text, parse_experience

_SENTENCE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9])")
_BULLET = re.compile(r"^\s*(?:(?:[-\u2013\u2014\u2022\u00b7*])|(?:\d+[.)]))\s*")

_KNOWN_SECTIONS = {
    "requirements",
    "minimum requirements",
    "minimum qualifications",
    "basic qualifications",
    "required skills",
    "required qualifications",
    "qualifications",
    "preferred requirements",
    "preferred qualifications",
    "preferred skills",
    "nice to have",
    "good to have",
    "responsibilities",
    "what you will do",
    "what you'll do",
    "what you\u2019ll do",
    "your role",
    "about the role",
    "who you are",
    "what we are looking for",
    "tech stack",
    "technology stack",
    "our stack",
    "experience",
}
_PREFERRED_SECTION = re.compile(r"preferred|nice to have|good to have", re.I)
_REQUIRED_SECTION = re.compile(
    r"requirements?|qualifications?|required|what we are looking for", re.I
)
_RESPONSIBILITY_SECTION = re.compile(r"responsibilities|what you|your role|about the role", re.I)
_CONTEXT_SECTION = re.compile(r"tech(?:nology)? stack|our stack", re.I)

_EXPLICIT_REQUIRED = re.compile(
    r"\b(required|must|need(?:ed)?|minimum|at least|proficien\w*|strong (?:knowledge|skills?)|"
    r"hands-on|working knowledge|experience with|knowledge of)\b",
    re.I,
)
_HARD_REQUIRED = re.compile(r"\b(required|must|minimum|at least|need(?:ed)? to)\b", re.I)
_PREFERRED = re.compile(
    r"\b(preferred|preferably|plus|nice to have|good to have|desirable|advantage|bonus|"
    r"would be beneficial|familiarity|exposure)\b",
    re.I,
)
_STRONG_PREFERRED = re.compile(
    r"\b(preferred|preferably|plus|nice to have|good to have|desirable|advantage|bonus|"
    r"would be beneficial)\b",
    re.I,
)
_RESPONSIBILITY = re.compile(
    r"\b(you will|you(?:'|\u2019)ll|responsible for|design|develop|build|implement|maintain|"
    r"debug|test|automate|deliver|ship|create|write)\b",
    re.I,
)
_EMPLOYER_CONTEXT = re.compile(
    r"\b(we (?:primarily |mostly |mainly )?(?:use|build with)|"
    r"we work (?:primarily |mostly |mainly )?in|our (?:technology )?"
    r"stack|the (?:technology )?stack (?:is|includes)|built (?:on|with)|technologies we use)\b",
    re.I,
)
_COMPANY_PROSE = re.compile(r"^(we|our|the company)\b", re.I)
_CANDIDATE_PROSE = re.compile(r"\b(you|your|candidate|applicant)\b", re.I)
_GENERAL_QUALIFICATION = re.compile(
    r"\b(ability to learn|learn unfamiliar|communication|collaborat\w*|teamwork|"
    r"problem[ -]solving|analytical|attention to detail|programming fundamentals|"
    r"computer science fundamentals|degree|bachelor|master|classwork|side projects?)\b",
    re.I,
)

_FRESHER = re.compile(
    r"\b(fresh(?:er| graduate)s? (?:are )?(?:welcome|eligible)|recent graduates? "
    r"(?:are )?(?:welcome|eligible)|entry[ -]level candidates? (?:are )?welcome)\b",
    re.I,
)
_NO_EXPERIENCE = re.compile(
    r"\b(no|zero) (?:prior |professional |work )?experience required\b", re.I
)
_CURRENT_STUDENT = re.compile(
    r"\b(currently (?:enrolled|pursuing)|current (?:student|undergraduate|graduate student))\b",
    re.I,
)
_DEGREE_IN_PROGRESS = re.compile(
    r"\b(pursuing|working towards) (?:a |an )?(?:bachelor|master|degree|b\.?tech|b\.?e)\b",
    re.I,
)
_INTERNSHIP_EXPERIENCE = re.compile(r"\binternship experience\b", re.I)
_PROFESSIONAL = re.compile(r"\b(professional|industry|commercial|work) experience\b", re.I)
_ACADEMIC = re.compile(r"\b(academic|classwork|coursework|college|university) projects?\b", re.I)

_ALTERNATIVE = re.compile(r"\b(or|either|one of|one or more|any one|at least one)\b", re.I)
_WORK_YEARS = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:(?:-|\u2013|\u2014|to)\s*\d+(?:\.\d+)?\s*)?\+?\s*"
    r"years?\s+of\s+(?:building|developing|designing|operating|maintaining|leading|"
    r"delivering|working|engineering)\b",
    re.I,
)


@dataclass(frozen=True)
class _Technology:
    name: str
    pattern: re.Pattern[str]


def _tech(name: str, pattern: str, flags: int = re.IGNORECASE) -> _Technology:
    return _Technology(name, re.compile(pattern, flags))


# Ordered longest/specific first. React, Node.js and Express.js intentionally stay separate.
_TECHNOLOGIES = [
    _tech("Sentence Transformers", r"\bsentence[ -]transformers?\b"),
    _tech("GitHub Actions", r"\bgithub actions?\b"),
    _tech("Google Cloud", r"\b(?:google cloud(?: platform)?|GCP)\b"),
    _tech("Machine learning", r"\bmachine learning\b|\bML\b"),
    _tech("Deep learning", r"\bdeep learning\b"),
    _tech("Generative AI", r"\bgenerative AI\b|\bgenAI\b"),
    _tech("Test automation", r"\b(?:test|testing|qa) automation\b|\bautomated testing\b"),
    _tech("API testing", r"\bAPI testing\b"),
    _tech("REST APIs", r"\bREST(?:ful)? APIs?\b"),
    _tech("MERN", r"\bMERN\b"),
    _tech("scikit-learn", r"\bscikit[ -]learn\b|\bsklearn\b"),
    _tech("JavaScript", r"\bJavaScript\b|\bECMAScript\b|\bES6\+?\b"),
    _tech("TypeScript", r"\bTypeScript\b"),
    _tech("PostgreSQL", r"\bPostgreSQL\b|\bPostgres\b"),
    _tech("MongoDB", r"\bMongoDB\b"),
    _tech("Node.js", r"\bNode(?:\.js|JS)\b"),
    _tech("Express.js", r"\bExpress(?:\.js|JS)\b"),
    _tech("Next.js", r"\bNext(?:\.js|JS)\b"),
    _tech("React", r"\b(?:React|react(?:\.js|js))\b", 0),
    _tech("FastAPI", r"\bFastAPI\b"),
    _tech("ChromaDB", r"\bChromaDB\b"),
    _tech("pgvector", r"\bpgvector\b"),
    _tech("WebSockets", r"\bWebSockets?\b"),
    _tech("Socket.IO", r"\bSocket\.IO\b"),
    _tech("Spring Boot", r"\bSpring Boot\b"),
    _tech("TensorFlow", r"\bTensorFlow\b"),
    _tech("PyTorch", r"\bPyTorch\b"),
    _tech("Playwright", r"\bPlaywright\b"),
    _tech("Selenium", r"\bSelenium\b"),
    _tech("Cypress", r"\bCypress\b"),
    _tech("pytest", r"\bpytest\b"),
    _tech("JUnit", r"\bJUnit\b"),
    _tech("Kubernetes", r"\bKubernetes\b|\bK8s\b"),
    _tech("Docker", r"\bDocker\b"),
    _tech("Terraform", r"\bTerraform\b"),
    _tech("GraphQL", r"\bGraphQL\b"),
    _tech("Kafka", r"\bKafka\b"),
    _tech("Redis", r"\bRedis\b"),
    _tech("Supabase", r"\bSupabase\b"),
    _tech("MySQL", r"\bMySQL\b"),
    _tech("SQLite", r"\bSQLite\b"),
    _tech("SQL", r"\bSQL\b"),
    _tech("Python", r"\bPython\b"),
    _tech("Ruby", r"\bRuby\b"),
    _tech("Scala", r"\bScala\b"),
    _tech("Java", r"\bJava\b"),
    _tech("C++", r"(?<!\w)C\+\+(?!\w)", 0),
    _tech("C#", r"(?<!\w)C#(?!\w)", 0),
    _tech("Go", r"\b(?:Go|Golang)\b", 0),
    _tech("PHP", r"\bPHP\b"),
    _tech("Flask", r"\bFlask\b"),
    _tech("Django", r"\bDjango\b"),
    _tech("Angular", r"\bAngular\b"),
    _tech("Vue", r"\bVue(?:\.js|JS)?\b", 0),
    _tech("Linux", r"\bLinux\b"),
    _tech("AWS", r"\bAWS\b|\bAmazon Web Services\b"),
    _tech("Azure", r"\bAzure\b"),
    _tech("Git", r"\bGit\b"),
    _tech("OpenCV", r"\bOpenCV\b"),
    _tech("LLMs", r"\bLLMs?\b|\blarge language models?\b"),
    _tech("NLP", r"\bNLP\b|\bnatural language processing\b"),
    _tech("RAG", r"\bRAG\b|\bretrieval[ -]augmented generation\b", 0),
    _tech("Embeddings", r"\bembeddings?\b"),
]

_AI_TITLE = re.compile(
    r"\b(ai engineer|applied ai|artificial intelligence|llm|nlp|rag|machine learning|"
    r"ml engineer|python backend|fastapi backend)\b",
    re.I,
)
_FULL_STACK_TITLE = re.compile(
    r"\b(full[ -]?stack|mern|front[ -]?end|web developer|react developer|"
    r"next(?:\.js)? developer|node(?:\.js)? developer|express(?:\.js)? developer)\b",
    re.I,
)
_SDE_TITLE = re.compile(
    r"\b(sde|swe|software engineer|software developer|software development engineer|"
    r"graduate engineer|associate engineer|application developer|product engineer)\b",
    re.I,
)
_SDET_TITLE = re.compile(
    r"\b(sdet|qa automation|test automation|api test(?:ing)?|software test engineer|"
    r"qa engineer|test engineer|tester)\b",
    re.I,
)
_MANUAL_QA = re.compile(r"\bmanual (?:qa|test(?:ing)?)\b", re.I)

_AI_SUPPORT = re.compile(
    r"\b(LLMs?|RAG|NLP|machine learning|ML models?|embeddings?|semantic search|"
    r"Sentence Transformers|FastAPI|Python backend)\b",
    re.I,
)
_PYTHON_BACKEND = re.compile(
    r"(?=.*\b(?:Python|FastAPI)\b)(?=.*\b(?:backend|APIs?|services?|microservices?)\b)",
    re.I,
)
_FULL_STACK_SUPPORT = re.compile(
    r"\b(full[ -]?stack|MERN|React|Next\.js|Node\.js|Express\.js|front[ -]?end|web UI)\b",
    re.I,
)
_NODE_EXPRESS = re.compile(r"(?=.*\bNode\.js\b)(?=.*\bExpress\.js\b)", re.I)
_SDE_SUPPORT = re.compile(
    r"\b(design|develop|build|implement|maintain|debug|test|ship|deliver|write)\w*\b.*"
    r"\b(software|systems?|applications?|services?|features?|code|products?|APIs?)\b|"
    r"\bsoftware development lifecycle\b",
    re.I,
)
_SDET_SUPPORT = re.compile(
    r"\b(test automation|automated testing|API testing|Selenium|Playwright|Cypress|pytest|"
    r"JUnit|regression testing|integration testing)\b",
    re.I,
)


@dataclass(frozen=True)
class _Unit:
    sentence: str
    section: str | None
    source: Literal["page", "post"]

    def evidence(self) -> RequirementEvidence:
        return RequirementEvidence(sentence=self.sentence, section=self.section, source=self.source)


def _clean_line(raw: str) -> str:
    return " ".join(_BULLET.sub("", raw).split()).strip()


def _section_name(value: str) -> str:
    """Normalize headings without treating parenthetical guidance as description text."""

    return re.sub(r"\s*\([^)]*\)\s*$", "", value.rstrip(":").casefold().strip())


def _units(text: str | None, source: Literal["page", "post"]) -> list[_Unit]:
    section: str | None = None
    units: list[_Unit] = []
    for raw in (text or "").splitlines():
        line = _clean_line(raw)
        if not line:
            continue
        prefix, separator, tail = line.partition(":")
        normalized = _section_name(prefix)
        if normalized in _KNOWN_SECTIONS:
            section = prefix.strip()
            if not separator or not tail.strip():
                continue
        elif _section_name(line) in _KNOWN_SECTIONS:
            section = line.rstrip(":").strip()
            continue
        for sentence in _SENTENCE.split(line):
            if cleaned := " ".join(sentence.split()).strip():
                units.append(_Unit(cleaned[:800], section, source))
    return units


def _unique_skills(sentence: str) -> list[str]:
    names: list[str] = []
    for _, name in sorted(
        (
            (match.start(), technology.name)
            for technology in _TECHNOLOGIES
            if (match := technology.pattern.search(sentence))
        ),
        key=lambda item: item[0],
    ):
        if name.casefold() not in {existing.casefold() for existing in names}:
            names.append(name)
    return names


def _relation(sentence: str, skills: list[str]) -> Literal["all", "any"]:
    if len(skills) < 2:
        return "all"
    if _ALTERNATIVE.search(sentence):
        return "any"
    positions = [sentence.casefold().find(skill.casefold()) for skill in skills]
    positions = [position for position in positions if position >= 0]
    if len(positions) < 2:
        return "any" if _ALTERNATIVE.search(sentence) else "all"
    span = sentence[min(positions) : max(positions) + max(len(skill) for skill in skills)]
    return "any" if _ALTERNATIVE.search(span) else "all"


def _is_context(unit: _Unit) -> bool:
    return bool(
        _EMPLOYER_CONTEXT.search(unit.sentence)
        or (unit.section and _CONTEXT_SECTION.search(unit.section))
    )


def _is_technology_context(unit: _Unit) -> bool:
    if _is_context(unit):
        return True
    return bool(
        _COMPANY_PROSE.search(unit.sentence)
        and not _CANDIDATE_PROSE.search(unit.sentence)
        and not _HARD_REQUIRED.search(unit.sentence)
    )


def _is_preferred(unit: _Unit) -> bool:
    if _HARD_REQUIRED.search(unit.sentence):
        return False
    if _STRONG_PREFERRED.search(unit.sentence):
        return True
    if unit.section and _PREFERRED_SECTION.search(unit.section):
        return True
    if unit.section and _REQUIRED_SECTION.search(unit.section):
        return False
    return bool(_PREFERRED.search(unit.sentence))


def _need(unit: _Unit) -> Literal["required", "preferred"]:
    if _HARD_REQUIRED.search(unit.sentence):
        return "required"
    if _is_preferred(unit):
        return "preferred"
    if _EXPLICIT_REQUIRED.search(unit.sentence):
        return "required"
    if unit.section and (
        _REQUIRED_SECTION.search(unit.section) or _RESPONSIBILITY_SECTION.search(unit.section)
    ):
        return "required"
    return "required" if _RESPONSIBILITY.search(unit.sentence) else "preferred"


def _skill_requirement(unit: _Unit, skills: list[str]) -> SkillRequirement:
    return SkillRequirement(
        skills=skills,
        relation=_relation(unit.sentence, skills),
        evidence=unit.evidence(),
    )


def _experience_type(
    sentence: str,
) -> Literal["professional", "internship", "academic", "unspecified"]:
    if _INTERNSHIP_EXPERIENCE.search(sentence):
        return "internship"
    if _ACADEMIC.search(sentence):
        return "academic"
    if _PROFESSIONAL.search(sentence):
        return "professional"
    if _WORK_YEARS.search(sentence):
        return "professional"
    return "unspecified"


def _experience(
    unit: _Unit, skills: list[str]
) -> tuple[ExperienceRequirement | None, list[StudentRequirement]]:
    sentence = unit.sentence
    preferred = _is_preferred(unit)
    need: Literal["required", "preferred"] = "preferred" if preferred else "required"
    students: list[StudentRequirement] = []
    if _FRESHER.search(sentence):
        students.append(
            StudentRequirement(kind="fresher_welcome", required=False, evidence=unit.evidence())
        )
    if _NO_EXPERIENCE.search(sentence):
        students.append(
            StudentRequirement(
                kind="no_experience_required", required=False, evidence=unit.evidence()
            )
        )
    if _CURRENT_STUDENT.search(sentence):
        students.append(
            StudentRequirement(
                kind="current_student", required=not preferred, evidence=unit.evidence()
            )
        )
    if _DEGREE_IN_PROGRESS.search(sentence):
        students.append(
            StudentRequirement(
                kind="degree_in_progress", required=not preferred, evidence=unit.evidence()
            )
        )
    if _INTERNSHIP_EXPERIENCE.search(sentence):
        students.append(
            StudentRequirement(
                kind="internship_experience", required=not preferred, evidence=unit.evidence()
            )
        )

    lo, hi = experience_in_text(sentence)
    if (
        lo is None
        and hi is None
        and (
            re.search(r"\bexperience\b", sentence, re.I)
            and (preferred or (unit.section and "experience" in unit.section.casefold()))
        )
    ):
        lo, hi = parse_experience(sentence)
    if (
        lo is None
        and hi is None
        and unit.section
        and _REQUIRED_SECTION.search(unit.section)
        and _WORK_YEARS.search(sentence)
    ):
        lo, hi = parse_experience(sentence)
    if _NO_EXPERIENCE.search(sentence) or _FRESHER.search(sentence):
        lo = 0.0
    subject = (
        ", ".join(skills) if skills and re.search(r"\bexperience with\b", sentence, re.I) else None
    )
    has_experience = (
        lo is not None
        or hi is not None
        or _INTERNSHIP_EXPERIENCE.search(sentence)
        or subject is not None
    )
    if not has_experience:
        return None, students
    return (
        ExperienceRequirement(
            need=need,
            experience_type=_experience_type(sentence),
            minimum_years=lo,
            maximum_years=hi,
            subject=subject,
            evidence=unit.evidence(),
        ),
        students,
    )


def _supporting_unit(units: list[_Unit], *patterns: re.Pattern[str]) -> _Unit | None:
    for unit in units:
        if (
            not _is_context(unit)
            and not _COMPANY_PROSE.search(unit.sentence)
            and any(pattern.search(unit.sentence) for pattern in patterns)
        ):
            return unit
    return None


def _track(
    track: Literal["ai_backend", "full_stack", "sde", "sdet"],
    role: str,
    title_pattern: re.Pattern[str],
    support: _Unit | None,
    manual_only: bool = False,
) -> RoleTrack | None:
    title_matches = bool(title_pattern.search(role))
    if not title_matches and support is None:
        return None
    evidence = [RequirementEvidence(sentence=role, section="title", source="title")]
    if support is not None:
        evidence.append(support.evidence())
    strength: Literal["strong", "possible"] = (
        "strong"
        if support is not None and _need(support) == "required" and not manual_only
        else "possible"
    )
    return RoleTrack(track=track, strength=strength, evidence=evidence)


def classify_role(role: str | None, units: list[_Unit]) -> list[RoleTrack]:
    """Classify one posting into zero or more tracks without assigning a match score."""
    title = " ".join((role or "").split())
    if not title:
        return []
    ai = _supporting_unit(units, _AI_SUPPORT, _PYTHON_BACKEND)
    full_stack = _supporting_unit(units, _FULL_STACK_SUPPORT, _NODE_EXPRESS)
    sde = _supporting_unit(units, _SDE_SUPPORT)
    sdet = _supporting_unit(units, _SDET_SUPPORT)
    manual = bool(_MANUAL_QA.search(title) or any(_MANUAL_QA.search(u.sentence) for u in units))
    tracks = [
        _track("ai_backend", title, _AI_TITLE, ai),
        _track("full_stack", title, _FULL_STACK_TITLE, full_stack),
        _track("sde", title, _SDE_TITLE, sde),
        _track("sdet", title, _SDET_TITLE, sdet, manual_only=manual and sdet is None),
    ]
    return [track for track in tracks if track is not None]


def _experience_fields(requirements: JobRequirements) -> dict[str, float]:
    numeric = [
        item
        for item in requirements.experience
        if item.need == "required" and item.minimum_years is not None
    ]
    if not numeric:
        return {}
    minimum = max(item.minimum_years or 0.0 for item in numeric)
    maxima = [item.maximum_years for item in numeric if item.maximum_years is not None]
    maximum = min(maxima) if maxima else None
    fresher = any(
        item.kind in ("fresher_welcome", "no_experience_required")
        for item in requirements.student_requirements
    )
    if (maximum is not None and minimum > maximum) or (fresher and minimum > 0):
        requirements.experience_status = "conflicting"
        return {}
    requirements.experience_status = "known"
    fields = {"experience_min": minimum}
    if maximum is not None:
        fields["experience_max"] = maximum
    return fields


def analyze_requirements(
    role: str | None, page_text: str | None, post_text: str | None
) -> dict[str, Any]:
    """Additive JobPosting fields derived only from the supplied title and text."""
    units = _units(page_text, "page") + _units(post_text, "post")
    required: list[SkillRequirement] = []
    preferred: list[SkillRequirement] = []
    context: list[SkillRequirement] = []
    qualifications: list[GeneralQualification] = []
    experience: list[ExperienceRequirement] = []
    students: list[StudentRequirement] = []

    for unit in units:
        skills = _unique_skills(unit.sentence)
        if skills:
            requirement = _skill_requirement(unit, skills)
            if _is_technology_context(unit):
                context.append(requirement)
            elif _need(unit) == "required":
                required.append(requirement)
            elif _PREFERRED.search(unit.sentence) or (
                unit.section and _PREFERRED_SECTION.search(unit.section)
            ):
                preferred.append(requirement)
        exp, student = _experience(unit, skills)
        if exp is not None:
            experience.append(exp)
        students.extend(student)
        if _GENERAL_QUALIFICATION.search(unit.sentence) and not _is_context(unit):
            qualifications.append(
                GeneralQualification(
                    qualification=unit.sentence[:300],
                    need="preferred" if _is_preferred(unit) else "required",
                    evidence=unit.evidence(),
                )
            )

    requirements = JobRequirements(
        required_skills=required,
        preferred_skills=preferred,
        employer_technologies=context,
        general_qualifications=qualifications,
        experience=experience,
        student_requirements=students,
    )
    fields: dict[str, Any] = {}
    if tracks := classify_role(role, units):
        fields["role_tracks"] = [track.model_dump(mode="json") for track in tracks]
    if any(
        (
            required,
            preferred,
            context,
            qualifications,
            experience,
            students,
        )
    ):
        fields.update(_experience_fields(requirements))
        fields["requirements"] = requirements.model_dump(mode="json")

    # A flat list cannot express alternatives. Keep those only in structured requirements
    # instead of making every option look mandatory to the legacy skills-rule matcher.
    fields["skills_required"] = [
        skill for item in required if item.relation == "all" for skill in item.skills
    ]
    fields["skills_preferred"] = [
        skill for item in preferred if item.relation == "all" for skill in item.skills
    ]
    fields["skills_required"] = list(dict.fromkeys(fields["skills_required"]))
    fields["skills_preferred"] = list(dict.fromkeys(fields["skills_preferred"]))
    if not fields["skills_required"]:
        fields.pop("skills_required")
    if not fields["skills_preferred"]:
        fields.pop("skills_preferred")
    return fields
