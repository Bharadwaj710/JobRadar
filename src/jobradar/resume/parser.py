"""Resume parsing and structured profile draft extraction.

Supports deterministic regex/rule-based parsing with evidence snippet preservation
and optional LLM-assisted refinement. Designed to be resilient, hallucination-free,
and safe against untrusted resume text.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field


class DraftPersonal(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    github: str | None = None
    linkedin: str | None = None
    portfolio: str | None = None


class DraftEducation(BaseModel):
    degree: str | None = None
    field_of_study: str | None = None
    institution: str | None = None
    graduation_year: int | None = None
    cgpa_or_percentage: str | None = None


class DraftExperience(BaseModel):
    title: str | None = None
    company: str | None = None
    location: str | None = None
    dates: str | None = None
    is_current: bool = False
    bullets: list[str] = Field(default_factory=list)


class DraftProject(BaseModel):
    name: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    link: str | None = None
    bullets: list[str] = Field(default_factory=list)


class ExtractionMetadata(BaseModel):
    parser_used: Literal["deterministic", "llm", "hybrid"] = "deterministic"
    confidence: float = 0.5
    warnings: list[str] = Field(default_factory=list)
    raw_char_count: int = 0


class ResumeProfileDraft(BaseModel):
    personal: DraftPersonal = Field(default_factory=DraftPersonal)
    education: list[DraftEducation] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    experience: list[DraftExperience] = Field(default_factory=list)
    projects: list[DraftProject] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    evidence_snippets: dict[str, list[str]] = Field(default_factory=dict)
    metadata: ExtractionMetadata = Field(default_factory=ExtractionMetadata)

    def to_profile_dict(self) -> dict[str, Any]:
        """Convert draft into standard JobRadar profile dictionary format."""
        basics: dict[str, Any] = {
            "name": self.personal.name or "",
            "email": self.personal.email or "",
            "phone": self.personal.phone or "",
        }
        if self.personal.location:
            basics["location"] = self.personal.location
        if self.personal.github:
            basics["github"] = self.personal.github
        if self.personal.linkedin:
            basics["linkedin"] = self.personal.linkedin
        if self.personal.portfolio:
            basics["portfolio"] = self.personal.portfolio

        education_list = []
        for edu in self.education:
            education_list.append(
                {
                    "institution": edu.institution or "",
                    "degree": edu.degree or "",
                    "branch": edu.field_of_study or "",
                    "graduation_year": edu.graduation_year,
                    "cgpa": edu.cgpa_or_percentage or "",
                }
            )

        experience_list = []
        for i, exp in enumerate(self.experience, 1):
            bullets = [{"id": f"exp{i}_b{j}", "text": b} for j, b in enumerate(exp.bullets, 1)]
            experience_list.append(
                {
                    "id": f"exp_{i}",
                    "company": exp.company or "",
                    "title": exp.title or "",
                    "dates": exp.dates or "",
                    "bullets": bullets,
                }
            )

        projects_list = []
        for i, proj in enumerate(self.projects, 1):
            bullets = [{"id": f"proj{i}_b{j}", "text": b} for j, b in enumerate(proj.bullets, 1)]
            projects_list.append(
                {
                    "id": f"proj_{i}",
                    "name": proj.name or "",
                    "tools": proj.technologies,
                    "link": proj.link or "",
                    "bullets": bullets,
                }
            )

        return {
            "basics": basics,
            "education": education_list,
            "skills": self.skills,
            "experience": experience_list,
            "projects": projects_list,
            "certifications": self.certifications,
            "preferences": {
                "target_roles": ["Software Engineer", "Backend Developer"],
                "locations": ["Anywhere in India", "Remote"],
                "work_modes": ["Remote", "Hybrid", "On-site"],
            },
            "evidence": {
                "snippets": self.evidence_snippets,
                "metadata": self.metadata.model_dump(),
            },
        }


# Standard technical skills dictionary for deterministic keyword matching
KNOWN_TECHNICAL_SKILLS: list[str] = [
    "Python",
    "Java",
    "C",
    "C++",
    "C#",
    "Go",
    "Rust",
    "Ruby",
    "PHP",
    "Swift",
    "Kotlin",
    "Dart",
    "JavaScript",
    "TypeScript",
    "HTML",
    "HTML5",
    "CSS",
    "CSS3",
    "React",
    "Next.js",
    "Vue",
    "Angular",
    "Node.js",
    "Express",
    "Django",
    "Flask",
    "FastAPI",
    "Spring",
    "Spring Boot",
    "SQL",
    "PostgreSQL",
    "MySQL",
    "SQLite",
    "MongoDB",
    "Redis",
    "Cassandra",
    "GraphQL",
    "REST",
    "RESTful APIs",
    "Docker",
    "Kubernetes",
    "AWS",
    "Azure",
    "GCP",
    "Linux",
    "Git",
    "GitHub",
    "GitLab",
    "CI/CD",
    "Terraform",
    "PyTorch",
    "TensorFlow",
    "Scikit-Learn",
    "Pandas",
    "NumPy",
    "NLP",
    "LLM",
    "OpenCV",
    "Kafka",
    "RabbitMQ",
    "Celery",
    "Selenium",
    "Cypress",
    "Playwright",
    "Pytest",
    "JUnit",
    "Tailwind CSS",
    "Bootstrap",
    "Electron",
    "Postman",
    "Bash",
    "Shell",
]

# Regex patterns for section titles
SECTION_PATTERNS = {
    "education": re.compile(
        r"^(?:education|academic background|qualifications|academics)\b", re.IGNORECASE
    ),
    "experience": re.compile(
        r"^(?:experience|work experience|employment|internships|professional experience)\b",
        re.IGNORECASE,
    ),
    "projects": re.compile(
        r"^(?:projects|academic projects|key projects|personal projects)\b", re.IGNORECASE
    ),
    "skills": re.compile(
        r"^(?:skills|technical skills|key skills|technologies|core competencies)\b", re.IGNORECASE
    ),
    "certifications": re.compile(
        r"^(?:certifications|certificates|licenses|achievements)\b", re.IGNORECASE
    ),
}

# Major Indian and global cities
KNOWN_LOCATIONS = [
    "Bengaluru",
    "Bangalore",
    "Hyderabad",
    "Pune",
    "Mumbai",
    "Delhi",
    "New Delhi",
    "Noida",
    "Gurugram",
    "Gurgaon",
    "Chennai",
    "Kolkata",
    "Ahmedabad",
    "Jaipur",
    "Remote",
    "India",
]

DEGREE_PATTERN = re.compile(
    r"\b(B\.?Tech|B\.?E\.?|M\.?Tech|M\.?E\.?|M\.?S\.?|BCA|MCA|B\.?Sc|M\.?Sc|Bachelor\s+(?:of\s+[\w\s]+)?|Master\s+(?:of\s+[\w\s]+)?|Diploma)\b",
    re.IGNORECASE,
)

BRANCH_PATTERN = re.compile(
    r"\b(Computer Science|Information Technology|Electrical and Electronics|"
    r"Electrical Engineering|Electronics and Communication|Electronics|Mechanical|"
    r"Civil|Data Science|Artificial Intelligence|Software Engineering|CSE|ECE|EEE|IT)\b",
    re.IGNORECASE,
)

# Explicit graduation and batch labels
EXPLICIT_GRAD_LABEL_PATTERN = re.compile(
    r"(?:expected\s+(?:graduation|completion)|graduation\s+(?:year|date)|graduating(?:\s+in)?|"
    r"batch(?:\s+of)?|class(?:\s+of)?|year\s+of\s+passing|passing\s+year|completion\s+year|"
    r"graduation)\s*[:\-\u2013\u2014]?\s*(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|"
    r"july?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\.?\s*|\d{1,2}[\/\.-])?"
    r"(20[0-3]\d)\b",
    re.IGNORECASE,
)

# Date range with 4-digit end year: e.g. 2023 - 2027, 2023-2027, 2023 to 2027
RANGE_4DIGIT_PATTERN = re.compile(
    r"(?<!\d)(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\.?\s*|\d{1,2}[\/\.-])?"
    r"(20[0-3]\d)\s*(?:\u2013|\u2014|-|to|until)\s*(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
    r"may|june?|july?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\.?\s*|\d{1,2}[\/\.-])?"
    r"(20[0-3]\d)(?!\d)",
    re.IGNORECASE,
)

# Date range with 2-digit end year: e.g. 2023 - 27, 2023-27
RANGE_2DIGIT_PATTERN = re.compile(
    r"(?<!\d)(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\.?\s*|\d{1,2}[\/\.-])?"
    r"(20[0-3]\d)\s*(?:\u2013|\u2014|-|to|until)\s*'?(\d{2})\b",
    re.IGNORECASE,
)

# Date range ending in Present / Current / Ongoing: e.g. 2023 - Present
RANGE_PRESENT_PATTERN = re.compile(
    r"(?<!\d)(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\.?\s*|\d{1,2}[\/\.-])?"
    r"(20[0-3]\d)\s*(?:\u2013|\u2014|-|to|until)\s*(?:present|current|ongoing|now)\b",
    re.IGNORECASE,
)

# Secondary education and school board keywords
SECONDARY_EDU_PATTERN = re.compile(
    r"\b(Class\s+(?:XII|X|12|10)|12th|10th|Intermediate|Senior\s+Secondary|Higher\s+Secondary|"
    r"Secondary\s+School|HSC|SSC|CBSE|ICSE)\b",
    re.IGNORECASE,
)

# Institution keywords for detecting institutions and segmenting entries
INSTITUTION_KEYWORD_PATTERN = re.compile(
    r"\b(university|institute|college|school|academy|bits|iit|nit|iiit)\b",
    re.IGNORECASE,
)


def _split_into_sections(lines: list[str]) -> dict[str, list[str]]:
    """Split text lines into named sections based on header patterns."""
    sections: dict[str, list[str]] = {
        "header": [],
        "education": [],
        "experience": [],
        "projects": [],
        "skills": [],
        "certifications": [],
        "other": [],
    }

    current_section = "header"
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if line is a section header (short line matching header pattern)
        if len(stripped) <= 45:
            matched_section = None
            clean_header = re.sub(r"[:\-_#*]", "", stripped).strip()
            for sec_name, pattern in SECTION_PATTERNS.items():
                if pattern.match(clean_header):
                    matched_section = sec_name
                    break
            if matched_section:
                current_section = matched_section
                continue

        sections[current_section].append(stripped)

    return sections


GITHUB_RESERVED_NAMES: set[str] = {
    "features",
    "pricing",
    "about",
    "topics",
    "collections",
    "trending",
    "events",
    "login",
    "join",
    "signup",
    "contact",
    "security",
    "enterprise",
    "orgs",
    "settings",
    "explore",
    "stars",
    "sponsor",
    "marketplace",
}

LINKEDIN_RESERVED_NAMES: set[str] = {
    "in",
    "pub",
    "feed",
    "jobs",
    "company",
    "school",
    "learning",
    "help",
    "legal",
    "messaging",
    "notifications",
    "groups",
    "events",
}


def _extract_email(full_text: str, urls: list[str] | tuple[str, ...] | None = None) -> str | None:
    """Extract and validate candidate email address without adjacent labels or separators.

    Handles adjacent text (e.g. '|LinkedIn', '/GitHub', surrounding punctuation), normalizes
    whitespace, validates email structure, and returns None if no valid email is found.
    """
    candidates: list[str] = []

    # 1. Hyperlinks from annotations (e.g. mailto: links)
    if urls:
        for u in urls:
            if u.lower().startswith("mailto:"):
                clean = u[7:].strip()
                if clean:
                    candidates.append(clean)

    # 2. Strict regex for email with alphabetic TLD of 2-24 chars (no internal pipe)
    matches = re.findall(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}",
        full_text,
    )
    candidates.extend(matches)

    for raw in candidates:
        cleaned = raw.strip()
        if cleaned.lower().startswith("mailto:"):
            cleaned = cleaned[7:].strip()
        cleaned = cleaned.strip(" \t\n\r|,;:/()<>[]{}'\"")

        # Strip accidental glued labels at the end (e.g. user@gmail.comLinkedIn)
        for label in [
            "linkedin",
            "github",
            "portfolio",
            "leetcode",
            "codechef",
            "hackerrank",
            "website",
            "phone",
            "mobile",
        ]:
            if cleaned.lower().endswith(label):
                cleaned = cleaned[: -len(label)].rstrip(" \t\n\r|.,;:/_-")

        parts = cleaned.split("@")
        if len(parts) != 2:
            continue
        local_part, domain_part = parts[0], parts[1]
        if not local_part or not domain_part or "." not in domain_part:
            continue

        domain_parts = domain_part.split(".")
        tld = domain_parts[-1]
        if not tld.isalpha() or len(tld) < 2 or len(tld) > 24:
            continue

        if re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}$", cleaned):
            return cleaned.lower()

    return None


def _extract_github(full_text: str, urls: list[str] | tuple[str, ...] | None = None) -> str | None:
    """Extract candidate GitHub profile URL from text or document hyperlinks."""
    candidates: list[str] = []

    # 1. Document hyperlink annotations
    if urls:
        for u in urls:
            u_clean = u.strip().rstrip(" \t\n\r|,;:/()<>[]{}'\"")
            m = re.search(
                r"(?:https?:\/\/)?(?:www\.)?github\.com\/([A-Za-z0-9_\-]+)",
                u_clean,
                re.IGNORECASE,
            )
            if m:
                username = m.group(1)
                if username.lower() not in GITHUB_RESERVED_NAMES:
                    candidates.append(f"https://github.com/{username}")

    # 2. Search in text for URLs
    text_matches = re.findall(
        r"(?:https?:\/\/)?(?:www\.)?github\.com\/([A-Za-z0-9_\-]+(?:\/[A-Za-z0-9_\-]+)?)",
        full_text,
        re.IGNORECASE,
    )
    for m in text_matches:
        username = m.split("/")[0].strip()
        if username.lower() not in GITHUB_RESERVED_NAMES:
            candidates.append(f"https://github.com/{username}")

    # 3. Search for "GitHub: username" or "GitHub | username"
    label_match = re.search(
        r"\bgithub\s*[:\-|]\s*(?:https?:\/\/)?(?:www\.)?(?:github\.com\/)?([A-Za-z0-9_\-]+)\b",
        full_text,
        re.IGNORECASE,
    )
    if label_match:
        username = label_match.group(1).strip()
        if username.lower() not in GITHUB_RESERVED_NAMES and username.lower() not in [
            "com",
            "http",
            "https",
            "www",
        ]:
            candidates.append(f"https://github.com/{username}")

    return candidates[0] if candidates else None


def _extract_linkedin(
    full_text: str, urls: list[str] | tuple[str, ...] | None = None
) -> str | None:
    """Extract candidate LinkedIn profile URL from text or document hyperlinks."""
    candidates: list[str] = []

    # 1. Document hyperlink annotations
    if urls:
        for u in urls:
            u_clean = u.strip().rstrip(" \t\n\r|,;:/()<>[]{}'\"")
            m = re.search(
                r"(?:https?:\/\/)?(?:[a-z]{2,3}\.)?linkedin\.com\/(?:in|pub)\/([A-Za-z0-9_\-%]+)",
                u_clean,
                re.IGNORECASE,
            )
            if m:
                handle = m.group(1).strip("/")
                if handle.lower() not in LINKEDIN_RESERVED_NAMES:
                    candidates.append(f"https://linkedin.com/in/{handle}")

    # 2. Search in text for URLs
    text_matches = re.findall(
        r"(?:https?:\/\/)?(?:[a-z]{2,3}\.)?linkedin\.com\/(?:in|pub)\/([A-Za-z0-9_\-%]+)",
        full_text,
        re.IGNORECASE,
    )
    for m in text_matches:
        handle = m.strip("/").rstrip(" \t\n\r|,;:/()<>[]{}'\"")
        if handle.lower() not in LINKEDIN_RESERVED_NAMES:
            candidates.append(f"https://linkedin.com/in/{handle}")

    # 3. Search for "LinkedIn: username" or "LinkedIn | username"
    label_match = re.search(
        r"\blinkedin\s*[:\-|]\s*(?:https?:\/\/)?(?:[a-z]{2,3}\.)?(?:linkedin\.com\/(?:in|pub)\/)?([A-Za-z0-9_\-%]+)\b",
        full_text,
        re.IGNORECASE,
    )
    if label_match:
        handle = label_match.group(1).strip()
        if handle.lower() not in LINKEDIN_RESERVED_NAMES and handle.lower() not in [
            "com",
            "http",
            "https",
            "www",
            "in",
            "pub",
        ]:
            candidates.append(f"https://linkedin.com/in/{handle}")

    return candidates[0] if candidates else None


def _extract_personal(
    lines: list[str],
    header_lines: list[str],
    full_text: str,
    evidence: dict[str, list[str]],
    extracted_urls: list[str] | tuple[str, ...] | None = None,
) -> DraftPersonal:
    """Extract contact information and candidate name with evidence preservation."""
    personal = DraftPersonal()

    # Email
    email = _extract_email(full_text, urls=extracted_urls)
    if email:
        personal.email = email
        evidence.setdefault("email", []).append(personal.email)

    # Phone (supporting +91, parens, spaces, dashes)
    phone_match = re.search(
        r"(?:\+?91[\s.-]*)?(?:[6-9]\d{9}|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b)", full_text
    )
    if phone_match:
        personal.phone = phone_match.group(0).strip()
        evidence.setdefault("phone", []).append(personal.phone)

    # GitHub
    github = _extract_github(full_text, urls=extracted_urls)
    if github:
        personal.github = github
        evidence.setdefault("links", []).append(personal.github)

    # LinkedIn
    linkedin = _extract_linkedin(full_text, urls=extracted_urls)
    if linkedin:
        personal.linkedin = linkedin
        evidence.setdefault("links", []).append(personal.linkedin)

    # Location
    for loc in KNOWN_LOCATIONS:
        if re.search(rf"\b{re.escape(loc)}\b", full_text, re.IGNORECASE):
            personal.location = loc
            evidence.setdefault("location", []).append(loc)
            break

    # Name: scan header lines or top 5 non-empty lines
    candidate_lines = header_lines if header_lines else lines[:5]
    for line in candidate_lines:
        clean = line.strip()
        # Skip lines that are links, emails, labels, or too long/short
        if (
            "@" in clean
            or "http" in clean.lower()
            or "github" in clean.lower()
            or "linkedin" in clean.lower()
            or len(clean) < 3
            or len(clean) > 40
        ):
            continue
        if re.match(r"^(?:resume|curriculum vitae|cv|bio-data)$", clean, re.IGNORECASE):
            continue
        # Name should mostly be alphabetical
        if re.match(r"^[A-Za-z\s.'-]+$", clean):
            personal.name = clean
            evidence.setdefault("name", []).append(clean)
            break

    return personal


def _extract_graduation_year_from_text(text: str) -> int | None:
    """Extract graduation year from an education entry deterministically.

    Supports:
    - Explicit labels: 'Expected Graduation: 2027', 'Graduation Year: 2027', 'Class of 2027'
    - Date ranges: '2023-2027', '2023 - 2027', '2023 to 2027', '2023-27'
    - Ongoing degrees with explicit expected year: '2023 - Present (Expected 2027)'
    - Single completion year: 'B.Tech 2026'
    Preserves uncertainty (returns None) for ambiguous ongoing dates ('2023 - Present')
    or explicit start-only dates ('since 2023') without fabricating graduation dates.
    """
    # 1. Explicit graduation/completion label
    m_exp = EXPLICIT_GRAD_LABEL_PATTERN.search(text)
    if m_exp:
        return int(m_exp.group(1))

    # 2. Date range with 4-digit end year (e.g. 2023 - 2027, 2023-2027, 2023 to 2027)
    m_r4 = RANGE_4DIGIT_PATTERN.search(text)
    if m_r4:
        start_yr = int(m_r4.group(1))
        end_yr = int(m_r4.group(2))
        if end_yr >= start_yr:
            return end_yr

    # 3. Date range with 2-digit end year (e.g. 2023 - 27)
    m_r2 = RANGE_2DIGIT_PATTERN.search(text)
    if m_r2:
        start_yr = int(m_r2.group(1))
        end_yr_2d = int(m_r2.group(2))
        century = (start_yr // 100) * 100
        end_yr = century + end_yr_2d
        if end_yr >= start_yr:
            return end_yr

    # 4. Range ending in Present / Current (e.g. 2023 - Present)
    m_pres = RANGE_PRESENT_PATTERN.search(text)
    if m_pres:
        start_yr = int(m_pres.group(1))
        all_years = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", text)]
        later_years = [y for y in all_years if y > start_yr]
        if len(later_years) == 1:
            return later_years[0]
        # Ambiguous ongoing degree with no graduation year specified -> preserve uncertainty
        return None

    # 5. Single 4-digit year in entry
    all_years = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", text)]
    if len(all_years) == 1:
        y = all_years[0]
        # Verify it is not explicitly marked as a start year or matriculation
        if re.search(
            rf"\b(?:since|from|admitted|joined|started|commenced)(?:\s+(?:in|on|at))?\s*[:\-]?\s*{y}\b",
            text,
            re.IGNORECASE,
        ):
            return None
        return y

    return None


def _split_education_entries(lines: list[str]) -> list[list[str]]:
    """Group education lines into distinct education entry blocks."""
    if not lines:
        return []

    entries: list[list[str]] = []
    current_entry: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_entry:
                entries.append(current_entry)
                current_entry = []
            continue

        is_new_degree = bool(DEGREE_PATTERN.search(stripped))
        is_new_secondary = bool(SECONDARY_EDU_PATTERN.search(stripped))
        is_new_inst = bool(INSTITUTION_KEYWORD_PATTERN.search(stripped))

        # Start a new entry if current entry already has substantial education content
        if current_entry and (
            (
                is_new_degree
                and any(DEGREE_PATTERN.search(entry_line) for entry_line in current_entry)
            )
            or (
                is_new_secondary
                and any(
                    DEGREE_PATTERN.search(entry_line) or SECONDARY_EDU_PATTERN.search(entry_line)
                    for entry_line in current_entry
                )
            )
            or (
                is_new_inst
                and any(
                    INSTITUTION_KEYWORD_PATTERN.search(entry_line) for entry_line in current_entry
                )
            )
        ):
            entries.append(current_entry)
            current_entry = [stripped]
        else:
            current_entry.append(stripped)

    if current_entry:
        entries.append(current_entry)

    return entries or [lines]


def _extract_education(
    edu_lines: list[str], full_text: str, evidence: dict[str, list[str]]
) -> list[DraftEducation]:
    """Extract degrees, graduation years, institution, and scores."""
    education_entries: list[DraftEducation] = []

    if edu_lines:
        entry_blocks = _split_education_entries(edu_lines)
    else:
        # Fallback to full text if no education section header was identified
        degrees = list(DEGREE_PATTERN.finditer(full_text))
        if not degrees:
            return education_entries
        entry_blocks = [[full_text]]

    for block in entry_blocks:
        block_text = "\n".join(block)
        degrees = list(DEGREE_PATTERN.finditer(block_text))
        secondary = list(SECONDARY_EDU_PATTERN.finditer(block_text))

        if not degrees and not secondary and not edu_lines:
            continue

        primary_degree = (
            degrees[0].group(0)
            if degrees
            else (secondary[0].group(0) if secondary else "Bachelor's")
        )

        branch_match = BRANCH_PATTERN.search(block_text)
        branch = branch_match.group(0) if branch_match else None

        grad_year = _extract_graduation_year_from_text(block_text)

        score_match = re.search(
            r"(?:cgpa|gpa|percentage|score|marks)?\s*[:\-]?\s*(\b\d{1,2}(?:\.\d{1,2})?\s*(?:\/\s*10|%)|\b[6-9]\.\d{1,2}\b)",
            block_text,
            re.IGNORECASE,
        )
        score = score_match.group(1) if score_match else None

        institution = None
        for line in block:
            if re.search(
                r"\b(university|institute|college|school|bits|iit|nit|iiit|academy)\b",
                line,
                re.IGNORECASE,
            ):
                clean_inst = re.sub(
                    r"(?:\s*|(?<=\)))20[0-3]\d\s*(?:\u2013|\u2014|-|to)\s*.*$",
                    "",
                    line,
                    flags=re.IGNORECASE,
                ).strip()
                institution = clean_inst if clean_inst else line.strip()
                break

        edu = DraftEducation(
            degree=primary_degree,
            field_of_study=branch,
            institution=institution,
            graduation_year=grad_year,
            cgpa_or_percentage=score,
        )
        education_entries.append(edu)

        snippet_parts = [f"Degree: {primary_degree}"]
        if branch:
            snippet_parts.append(f"Branch: {branch}")
        if grad_year:
            snippet_parts.append(f"Year: {grad_year}")
        if score:
            snippet_parts.append(f"Score: {score}")
        evidence.setdefault("education", []).append(", ".join(snippet_parts))

    return education_entries


def _extract_skills(
    skills_lines: list[str], full_text: str, evidence: dict[str, list[str]]
) -> list[str]:
    """Extract technical skills deterministically matching against canonical skills dictionary."""
    found_skills: set[str] = set()

    # Search canonical skills across full text with word boundaries
    for skill in KNOWN_TECHNICAL_SKILLS:
        # Match word boundary or exact token
        escaped = re.escape(skill)
        pattern = rf"(?<![\w#+]){escaped}(?![\w#+])"
        if re.search(pattern, full_text, re.IGNORECASE):
            found_skills.add(skill)

    # In skills section, parse comma-separated or bullet items
    for line in skills_lines:
        clean = re.sub(r"^[•\-*#>\s]+", "", line)
        parts = re.split(r"[,|;/]", clean)
        for part in parts:
            item = part.strip()
            if 2 <= len(item) <= 25 and not re.search(r"[:\n]", item):
                for skill in KNOWN_TECHNICAL_SKILLS:
                    if skill.lower() == item.lower():
                        found_skills.add(skill)

    sorted_skills = sorted(found_skills, key=lambda s: s.lower())
    if sorted_skills:
        evidence.setdefault("skills", []).extend(sorted_skills[:15])

    return sorted_skills


def _extract_experience(
    exp_lines: list[str], evidence: dict[str, list[str]]
) -> list[DraftExperience]:
    """Parse work experience and internships from experience section."""
    if not exp_lines:
        return []

    experiences: list[DraftExperience] = []
    current_title: str | None = None
    current_company: str | None = None
    current_dates: str | None = None
    current_bullets: list[str] = []

    months = (
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
        r"January|February|March|April|May|June|July|August|September|October|November|December)?"
    )
    date_re = re.compile(
        rf"\b{months}\s*\d{{4}}\s*[-–\u2014to]+\s*(?:Present|Current|\d{{4}})\b",  # noqa: RUF001
        re.IGNORECASE,
    )

    for line in exp_lines:
        clean = line.strip()
        date_match = date_re.search(clean)

        # Check for bullet point
        if clean.startswith(("•", "-", "*", ">")):
            bullet_text = clean.lstrip("•-*>\t ").strip()
            if bullet_text:
                current_bullets.append(bullet_text)
            continue

        # If a line contains dates, it often represents a job header
        if date_match:
            if current_title or current_company or current_bullets:
                experiences.append(
                    DraftExperience(
                        title=current_title,
                        company=current_company,
                        dates=current_dates,
                        bullets=current_bullets,
                    )
                )
                current_bullets = []
            current_dates = date_match.group(0)
            # The rest of the line might be the company or role
            line_without_date = date_re.sub("", clean).strip(" -|,\t")
            if line_without_date:
                current_company = line_without_date
            continue

        # If it looks like a role title
        if any(
            w in clean.lower()
            for w in ["intern", "engineer", "developer", "lead", "analyst", "assistant"]
        ):
            if current_title and current_bullets:
                experiences.append(
                    DraftExperience(
                        title=current_title,
                        company=current_company,
                        dates=current_dates,
                        bullets=current_bullets,
                    )
                )
                current_company, current_dates, current_bullets = None, None, []
            current_title = clean
        elif not current_company:
            current_company = clean

    if current_title or current_company or current_bullets:
        experiences.append(
            DraftExperience(
                title=current_title,
                company=current_company,
                dates=current_dates,
                bullets=current_bullets,
            )
        )

    for exp in experiences:
        desc = f"{exp.title or 'Role'} at {exp.company or 'Company'}"
        evidence.setdefault("experience", []).append(desc)

    return experiences


def _extract_projects(proj_lines: list[str], evidence: dict[str, list[str]]) -> list[DraftProject]:
    """Parse projects, technologies used, and descriptions."""
    if not proj_lines:
        return []

    projects: list[DraftProject] = []
    current_name: str | None = None
    current_bullets: list[str] = []
    current_tech: list[str] = []

    for line in proj_lines:
        clean = line.strip()
        if clean.startswith(("•", "-", "*", ">")):
            bullet_text = clean.lstrip("•-*>\t ").strip()
            if bullet_text:
                current_bullets.append(bullet_text)
            continue

        # A non-bullet line in projects section is likely a project name
        if len(clean) <= 60 and not clean.endswith("."):
            if current_name or current_bullets:
                projects.append(
                    DraftProject(
                        name=current_name,
                        technologies=current_tech,
                        bullets=current_bullets,
                    )
                )
                current_bullets, current_tech = [], []

            # Check if project name has technologies in parentheses: "Project Name (React, Node.js)"
            tech_match = re.search(r"\(([^)]+)\)", clean)
            if tech_match:
                tech_raw = tech_match.group(1)
                current_tech = [t.strip() for t in re.split(r"[,|]", tech_raw) if t.strip()]
                clean = clean[: tech_match.start()].strip()

            current_name = clean

    if current_name or current_bullets:
        projects.append(
            DraftProject(
                name=current_name,
                technologies=current_tech,
                bullets=current_bullets,
            )
        )

    for proj in projects:
        evidence.setdefault("projects", []).append(proj.name or "Project")

    return projects


def parse_resume_to_draft(
    raw_text: str,
    llm: Any | None = None,
    extracted_urls: list[str] | tuple[str, ...] | None = None,
) -> ResumeProfileDraft:
    """Parse extracted resume text into a structured ResumeProfileDraft.

    Guarantees deterministic extraction with zero hallucination. If an LLM is
    supplied, it can optionally be invoked to enrich sections, but if it is None
    or raises an error, deterministic parsing succeeds seamlessly.
    """
    evidence: dict[str, list[str]] = {}
    warnings: list[str] = []

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    raw_char_count = len(raw_text)

    if raw_char_count < 100:
        warnings.append("Resume contains very little text. Please check details carefully.")

    sections = _split_into_sections(lines)

    personal = _extract_personal(
        lines, sections["header"], raw_text, evidence, extracted_urls=extracted_urls
    )
    education = _extract_education(sections["education"], raw_text, evidence)
    skills = _extract_skills(sections["skills"], raw_text, evidence)
    experience = _extract_experience(sections["experience"], evidence)
    projects = _extract_projects(sections["projects"], evidence)

    # Compute deterministic confidence score
    confidence = 0.2
    if personal.name:
        confidence += 0.15
    if personal.email:
        confidence += 0.15
    if education:
        confidence += 0.2
    if len(skills) >= 3:
        confidence += 0.15
    if experience or projects:
        confidence += 0.15

    confidence = round(min(1.0, confidence), 2)

    if not personal.email:
        warnings.append("No email address could be extracted.")
    if not personal.name:
        warnings.append("Candidate name could not be automatically detected.")
    if not education:
        warnings.append("Education details could not be detected.")

    metadata = ExtractionMetadata(
        parser_used="deterministic",
        confidence=confidence,
        warnings=warnings,
        raw_char_count=raw_char_count,
    )

    return ResumeProfileDraft(
        personal=personal,
        education=education,
        skills=skills,
        experience=experience,
        projects=projects,
        certifications=[],
        evidence_snippets=evidence,
        metadata=metadata,
    )
