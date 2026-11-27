"""Tests for Day 1: Candidate Foundation & Resume-to-Profile Vertical Slice.

Verifies:
- Candidate, Resume, and CandidateProfile data models and repo methods.
- Versioning logic (draft -> confirmed, subsequent updates create active versions).
- Resume validation (PDF and DOCX magic bytes, 5MB limit, corrupt files).
- Text extraction (PDF via pypdf, DOCX via openxml parser, empty/scanned status).
- Deterministic structured profile draft parsing with evidence preservation.
- Candidate session signing, cookie handling, and session-bound CSRF tokens.
- Candidate isolation & IDOR prevention (Candidate A cannot view or confirm Candidate B's profile).
- Web endpoints (/onboarding, /onboarding/upload, /onboarding/review, /onboarding/confirm, /onboarding/manual).
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from jobradar.db.models import Candidate, CandidateProfile
from jobradar.db.repo import Repo
from jobradar.resume.extract import (
    MAX_UPLOAD_SIZE,
    ExtractionError,
    extract_resume_content,
    validate_upload_content,
)
from jobradar.resume.parser import (
    parse_resume_to_draft,
)
from jobradar.web.app import create_app
from jobradar.web.session import (
    CANDIDATE_SESSION_COOKIE,
    candidate_csrf_token,
    parse_candidate_session,
    sign_candidate_session,
    validate_candidate_csrf,
    validate_onboarding_page_csrf,
    verify_profile_ownership,
)
from tests.conftest import FakeClock
from tests.test_digest import settings

SECRET_KEY = "test_dashboard_secret_key"  # noqa: S105


def create_minimal_pdf(text: str) -> bytes:
    """Create a valid in-memory PDF containing text using pypdf."""
    content = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET\n".encode("latin1")
    pdf_structure = (
        b"%PDF-1.4\n"
        b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
        b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
        b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj <</Length "
        + str(len(content)).encode("ascii")
        + b">> stream\n"
        + content
        + b"endstream\nendobj\n"
        b"5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n"
        b"trailer <</Size 6 /Root 1 0 R>>\nstartxref\n9\n%%EOF\n"
    )
    return pdf_structure


def create_minimal_docx(text: str) -> bytes:
    """Create a valid in-memory DOCX archive with word/document.xml."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
            "</w:body>"
            "</w:document>"
        )
        z.writestr("word/document.xml", doc_xml.encode("utf-8"))
        z.writestr("[Content_Types].xml", b"<Types></Types>")
    return buf.getvalue()


# ==============================================================================
# 1. Candidate & Profile Database Operations
# ==============================================================================


def test_candidate_crud(repo: Repo) -> None:
    candidate = repo.create_candidate(name="Jane Doe", email="jane@example.com")
    assert candidate.id.startswith("cand_")
    assert candidate.name == "Jane Doe"
    assert candidate.email == "jane@example.com"

    fetched = repo.get_candidate(candidate.id)
    assert fetched is not None
    assert fetched.email == "jane@example.com"

    all_cands = repo.list_candidates()
    assert any(c.id == candidate.id for c in all_cands)


def test_resume_persistence(repo: Repo) -> None:
    candidate = repo.create_candidate(name="Alex Smith", email="alex@example.com")
    resume = repo.save_resume(
        candidate_id=candidate.id,
        filename="alex_resume.pdf",
        file_type="pdf",
        raw_text="Education: B.Tech in Computer Science 2026",
        extraction_status="extracted",
    )
    assert resume.id.startswith("res_")
    assert resume.candidate_id == candidate.id
    assert resume.file_type == "pdf"

    resumes = repo.get_candidate_resumes(candidate.id)
    assert len(resumes) == 1
    assert resumes[0].filename == "alex_resume.pdf"


def test_profile_versioning_and_confirmation(repo: Repo) -> None:
    candidate = repo.create_candidate(name="Bob Test", email="bob@example.com")
    draft_data = {"basics": {"name": "Bob Test", "email": "bob@example.com"}, "skills": ["Python"]}

    # Initial draft: version 1, draft status
    p1 = repo.save_profile_draft(candidate.id, resume_id=None, profile_data=draft_data)
    assert p1.version == 1
    assert p1.status == "draft"

    # Confirm profile: version 1 becomes confirmed and active
    confirmed_p1 = repo.confirm_profile(p1.id, {**draft_data, "skills": ["Python", "FastAPI"]})
    assert confirmed_p1.id == p1.id
    assert confirmed_p1.version == 1
    assert confirmed_p1.status == "confirmed"
    assert confirmed_p1.is_active is True
    assert confirmed_p1.confirmed_at is not None

    active = repo.get_active_profile(candidate.id)
    assert active is not None
    assert active.id == confirmed_p1.id
    assert active.profile_data["skills"] == ["Python", "FastAPI"]

    # Re-confirming creates version 2, bumps active version, deactivates version 1
    p2 = repo.confirm_profile(p1.id, {**draft_data, "skills": ["Python", "FastAPI", "Docker"]})
    assert p2.id != p1.id
    assert p2.version == 2
    assert p2.status == "confirmed"
    assert p2.is_active is True

    # Check that p1 is no longer active
    old_p1 = repo.get_profile(p1.id)
    assert old_p1 is not None
    assert old_p1.is_active is False

    # Active profile is now version 2
    active_now = repo.get_active_profile(candidate.id)
    assert active_now is not None
    assert active_now.id == p2.id
    assert active_now.version == 2


# ==============================================================================
# 2. File Validation and Extraction
# ==============================================================================


def test_upload_validation_rejects_empty() -> None:
    with pytest.raises(ExtractionError, match="empty"):
        validate_upload_content(b"", "resume.pdf")


def test_upload_validation_rejects_oversized() -> None:
    oversized = b"%PDF-" + b"0" * (MAX_UPLOAD_SIZE + 10)
    with pytest.raises(ExtractionError, match="exceeds the 5 MB limit"):
        validate_upload_content(oversized, "huge.pdf")


def test_upload_validation_rejects_wrong_extension() -> None:
    with pytest.raises(ExtractionError, match="Unsupported file type"):
        validate_upload_content(b"some content", "resume.exe")


def test_upload_validation_rejects_invalid_pdf_signature() -> None:
    with pytest.raises(ExtractionError, match="valid PDF signature"):
        validate_upload_content(b"NOT_A_PDF_CONTENT", "resume.pdf")


def test_upload_validation_rejects_invalid_docx_signature() -> None:
    with pytest.raises(ExtractionError, match="valid OpenXML ZIP archive"):
        validate_upload_content(b"NOT_A_ZIP_CONTENT", "resume.docx")


def test_upload_validation_rejects_docx_without_document_xml() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("test.txt", "hello")
    with pytest.raises(ExtractionError, match="Corrupted DOCX"):
        validate_upload_content(buf.getvalue(), "corrupt.docx")


def test_docx_extraction_success() -> None:
    sample_text = (
        "Alice Smith\n"
        "Email: alice@example.com\n"
        "Education: B.Tech Computer Science 2026\n"
        "Skills: Python, FastAPI, Docker, PostgreSQL\n"
        "Projects: Job Recommendation System using Python and FastAPI\n"
    )
    docx_bytes = create_minimal_docx(sample_text)
    extracted = extract_resume_content(docx_bytes, "Alice_Resume.docx")
    assert extracted.status == "extracted"
    assert extracted.file_type == "docx"
    assert "alice@example.com" in extracted.raw_text
    assert "FastAPI" in extracted.raw_text


def test_scanned_or_empty_text_detected() -> None:
    # Under 50 characters of readable text
    tiny_docx = create_minimal_docx("Too short")
    extracted = extract_resume_content(tiny_docx, "tiny.docx")
    assert extracted.status == "empty_or_scanned"


# ==============================================================================
# 3. Deterministic Profile Draft Parsing & Evidence Preservation
# ==============================================================================


def test_parse_resume_to_draft_deterministic() -> None:
    raw_resume = """
    Rohan Sharma
    rohan.sharma@example.com | +91 9876543210 | Bengaluru, India
    https://github.com/rohansharma | https://linkedin.com/in/rohansharma

    EDUCATION
    National Institute of Technology Karnataka
    B.Tech in Computer Science and Engineering
    Graduation: 2026 | CGPA: 8.8/10

    SKILLS
    Python, Java, TypeScript, FastAPI, React, PostgreSQL, Docker, Git

    EXPERIENCE
    Software Engineering Intern - Cloud Corp
    June 2025 - August 2025
    - Developed REST APIs using FastAPI and PostgreSQL
    - Improved query response times by 35% with Redis caching

    PROJECTS
    JobRadar Search Engine (Python, FastAPI, SQLite)
    - Automated job ingestion and resume tailoring pipeline
    - Achieved 100% test coverage across critical backend modules
    """

    draft = parse_resume_to_draft(raw_resume)

    # Personal
    assert draft.personal.name == "Rohan Sharma"
    assert draft.personal.email == "rohan.sharma@example.com"
    assert draft.personal.phone == "+91 9876543210"
    assert draft.personal.location == "Bengaluru"
    assert draft.personal.github == "https://github.com/rohansharma"
    assert draft.personal.linkedin == "https://linkedin.com/in/rohansharma"

    # Education
    assert len(draft.education) == 1
    edu = draft.education[0]
    assert edu.degree is not None and "B.Tech" in edu.degree
    assert edu.field_of_study is not None and "Computer Science" in edu.field_of_study
    assert edu.graduation_year == 2026
    assert edu.cgpa_or_percentage == "8.8/10"

    # Skills
    assert "Python" in draft.skills
    assert "FastAPI" in draft.skills
    assert "PostgreSQL" in draft.skills
    assert "Docker" in draft.skills

    # Experience
    assert len(draft.experience) >= 1
    exp = draft.experience[0]
    assert "Intern" in (exp.title or "")
    assert len(exp.bullets) >= 1

    # Projects
    assert len(draft.projects) >= 1
    proj = draft.projects[0]
    assert "JobRadar" in (proj.name or "")

    # Evidence preservation
    assert "email" in draft.evidence_snippets
    assert "skills" in draft.evidence_snippets
    assert "education" in draft.evidence_snippets

    # Profile dictionary format conversion
    pdict = draft.to_profile_dict()
    assert pdict["basics"]["name"] == "Rohan Sharma"
    assert pdict["basics"]["email"] == "rohan.sharma@example.com"
    assert len(pdict["education"]) == 1
    assert pdict["education"][0]["graduation_year"] == 2026
    assert "Python" in pdict["skills"]


def test_parse_resume_handles_missing_fields_without_hallucination() -> None:
    minimal_text = "Just some unformatted random lines without any contact or degree details."
    draft = parse_resume_to_draft(minimal_text)
    assert draft.personal.email is None
    assert draft.personal.phone is None
    assert len(draft.education) == 0
    assert len(draft.metadata.warnings) > 0


def test_email_extraction_resilient_to_adjacent_labels_and_punctuation() -> None:
    """Verify email extraction strips adjacent labels and surrounding punctuation cleanly."""
    # 1. Email followed by |LinkedIn without space
    r1 = "+91 9381609981|bharadwajflasmup@gmail.com|LinkedIn |GitHub"
    d1 = parse_resume_to_draft(r1)
    assert d1.personal.email == "bharadwajflasmup@gmail.com"

    # 2. Email followed by GitHub text
    r2 = "Contact: test.candidate@example.org|GitHub"
    d2 = parse_resume_to_draft(r2)
    assert d2.personal.email == "test.candidate@example.org"

    # 3. Email with surrounding punctuation (angle brackets, trailing periods, parens, semicolons)
    r3 = "Reach me at <developer.ops@sub.domain.co.in>."
    d3 = parse_resume_to_draft(r3)
    assert d3.personal.email == "developer.ops@sub.domain.co.in"

    r4 = "Email: (user.one@company.io); Phone: 1234567890"
    d4 = parse_resume_to_draft(r4)
    assert d4.personal.email == "user.one@company.io"

    # 4. Glued trailing label without separator
    r5 = "myemail@gmail.comLinkedIn"
    d5 = parse_resume_to_draft(r5)
    assert d5.personal.email == "myemail@gmail.com"

    # 5. Invalid email -> returns None (does not guess)
    r6 = "Invalid email: user@not-a-domain or plain text"
    d6 = parse_resume_to_draft(r6)
    assert d6.personal.email is None


def test_social_urls_separate_and_same_line_formats() -> None:
    """Verify GitHub and LinkedIn extraction across separate lines, same line, and direct formats."""
    # 1. On separate lines with labels
    r1 = """
    Jane Doe
    jane@example.com
    GitHub: https://github.com/janedoe
    LinkedIn: https://linkedin.com/in/janedoe
    """
    d1 = parse_resume_to_draft(r1)
    assert d1.personal.github == "https://github.com/janedoe"
    assert d1.personal.linkedin == "https://linkedin.com/in/janedoe"

    # 2. On the same line with pipe separator
    r2 = "jane@example.com | https://github.com/janedoe | https://linkedin.com/in/janedoe"
    d2 = parse_resume_to_draft(r2)
    assert d2.personal.github == "https://github.com/janedoe"
    assert d2.personal.linkedin == "https://linkedin.com/in/janedoe"

    # 3. Direct URLs without https scheme
    r3 = "github.com/janedoe | linkedin.com/in/janedoe"
    d3 = parse_resume_to_draft(r3)
    assert d3.personal.github == "https://github.com/janedoe"
    assert d3.personal.linkedin == "https://linkedin.com/in/janedoe"

    # 4. Trailing slashes and query parameters cleaned
    r4 = "https://github.com/janedoe/ | https://www.linkedin.com/in/janedoe/?trk=public_profile"
    d4 = parse_resume_to_draft(r4)
    assert d4.personal.github == "https://github.com/janedoe"
    assert d4.personal.linkedin == "https://linkedin.com/in/janedoe"


def test_clickable_pdf_hyperlink_extraction() -> None:
    """Verify clickable PDF hyperlink annotations are extracted and populated into draft."""
    raw_text = "Alex Rivera\n+91 9999999999 | alex@example.com | LinkedIn | GitHub"
    annot_urls = [
        "mailto:alex@example.com",
        "https://www.linkedin.com/in/alex-rivera-swe/",
        "https://github.com/alexrivera",
        "https://github.com/alexrivera/awesome-backend",
    ]
    draft = parse_resume_to_draft(raw_text, extracted_urls=annot_urls)
    assert draft.personal.email == "alex@example.com"
    assert draft.personal.github == "https://github.com/alexrivera"
    assert draft.personal.linkedin == "https://linkedin.com/in/alex-rivera-swe"


def test_clickable_pdf_annotation_extraction_end_to_end() -> None:
    """Verify extract_resume_content extracts PDF link annotations and feeds parser."""
    import pypdf
    from pypdf.annotations import Link

    writer = pypdf.PdfWriter()
    _ = writer.add_blank_page(width=612, height=792)
    writer.add_annotation(
        page_number=0,
        annotation=Link(rect=(10, 10, 100, 30), url="https://github.com/pdfuser"),
    )
    writer.add_annotation(
        page_number=0,
        annotation=Link(rect=(110, 10, 200, 30), url="https://www.linkedin.com/in/pdf-user-swe"),
    )

    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    extracted = extract_resume_content(pdf_bytes, "test_annotated.pdf")
    assert "https://github.com/pdfuser" in extracted.extracted_urls
    assert "https://www.linkedin.com/in/pdf-user-swe" in extracted.extracted_urls

    draft = parse_resume_to_draft(extracted.raw_text, extracted_urls=list(extracted.extracted_urls))
    assert draft.personal.github == "https://github.com/pdfuser"
    assert draft.personal.linkedin == "https://linkedin.com/in/pdf-user-swe"


def test_missing_and_invalid_social_urls_defense() -> None:
    """Verify missing, invalid, or cross-platform URLs leave fields empty without guessing."""
    # 1. Missing URLs -> fields remain None
    r1 = "John Doe\njohn@example.com\nNo social profile links listed."
    d1 = parse_resume_to_draft(r1)
    assert d1.personal.github is None
    assert d1.personal.linkedin is None

    # 2. LinkedIn URL present but NO GitHub URL -> GitHub must remain None (never cross-assigned)
    r2 = "John Doe\njohn@example.com\nhttps://linkedin.com/in/johndoe"
    d2 = parse_resume_to_draft(r2)
    assert d2.personal.linkedin == "https://linkedin.com/in/johndoe"
    assert d2.personal.github is None

    # 3. GitHub URL present but NO LinkedIn URL -> LinkedIn must remain None (never cross-assigned)
    r3 = "John Doe\njohn@example.com\nhttps://github.com/johndoe"
    d3 = parse_resume_to_draft(r3)
    assert d3.personal.github == "https://github.com/johndoe"
    assert d3.personal.linkedin is None

    # 4. Non-profile / reserved URLs
    r4 = "John Doe\njohn@example.com\nhttps://github.com/pricing\nhttps://linkedin.com/feed"
    d4 = parse_resume_to_draft(r4)
    assert d4.personal.github is None
    assert d4.personal.linkedin is None


def test_graduation_year_current_students_and_date_ranges() -> None:
    """Verify various date range formats extract the completion year rather than matriculation year."""
    # En-dash (as seen in PDF extractions)
    r1 = "EDUCATION\nVignan’s Institute of Information Technology (VIIT)2023 – 2027\nB.Tech in Computer Science |CGPA: 8.3"
    d1 = parse_resume_to_draft(r1)
    assert len(d1.education) == 1
    assert d1.education[0].graduation_year == 2027
    assert d1.education[0].institution == "Vignan’s Institute of Information Technology (VIIT)"

    # Hyphen with spaces
    r2 = "EDUCATION\nIndian Institute of Technology Madras\nB.Tech in Electrical Engineering\n2023 - 2027"
    d2 = parse_resume_to_draft(r2)
    assert d2.education[0].graduation_year == 2027

    # Hyphen without spaces in parens
    r3 = "EDUCATION\nNational Institute of Technology\nB.Tech in Computer Science (2023-2027)"
    d3 = parse_resume_to_draft(r3)
    assert d3.education[0].graduation_year == 2027

    # Em-dash
    r4 = "EDUCATION\nBITS Pilani\nB.Tech in Electronics 2023-2027"
    d4 = parse_resume_to_draft(r4)
    assert d4.education[0].graduation_year == 2027

    # Word 'to'
    r5 = "EDUCATION\nDelhi Technological University\nB.Tech in Information Technology\n2023 to 2027"
    d5 = parse_resume_to_draft(r5)
    assert d5.education[0].graduation_year == 2027

    # Month names in range
    r6 = "EDUCATION\nIIIT Hyderabad\nB.Tech Computer Science\nAugust 2023 – May 2027"
    d6 = parse_resume_to_draft(r6)
    assert d6.education[0].graduation_year == 2027

    # 2-digit end year
    r7 = "EDUCATION\nCollege of Engineering\nB.Tech 2023-27"
    d7 = parse_resume_to_draft(r7)
    assert d7.education[0].graduation_year == 2027


def test_graduation_year_explicit_labels() -> None:
    """Verify explicit graduation labels (Expected Graduation, Class of, Batch of, etc.)."""
    r1 = "EDUCATION\nB.Tech in Computer Science\nExpected Graduation: 2027"
    assert parse_resume_to_draft(r1).education[0].graduation_year == 2027

    r2 = "EDUCATION\nB.Tech in Computer Science\nExpected Graduation: May 2027"
    assert parse_resume_to_draft(r2).education[0].graduation_year == 2027

    r3 = "EDUCATION\nB.Tech in Computer Science\nGraduation Year: 2027"
    assert parse_resume_to_draft(r3).education[0].graduation_year == 2027

    r4 = "EDUCATION\nB.Tech in Artificial Intelligence\nClass of 2027"
    assert parse_resume_to_draft(r4).education[0].graduation_year == 2027

    r5 = "EDUCATION\nB.Tech in Data Science\nBatch of 2026"
    assert parse_resume_to_draft(r5).education[0].graduation_year == 2026

    r6 = "EDUCATION\nB.Tech in Mechanical Engineering\nYear of Passing: 2025"
    assert parse_resume_to_draft(r6).education[0].graduation_year == 2025


def test_graduation_year_completed_degrees() -> None:
    """Verify completed degrees with past ranges or single years."""
    # Past completed range
    r1 = "EDUCATION\nNational Institute of Technology\nB.Tech in Computer Science\n2019 - 2023"
    assert parse_resume_to_draft(r1).education[0].graduation_year == 2023

    # Single degree year
    r2 = "EDUCATION\nUniversity of Pune\nB.Tech Computer Science 2024"
    assert parse_resume_to_draft(r2).education[0].graduation_year == 2024


def test_graduation_year_multiple_education_entries() -> None:
    """Verify multiple education entries associate years correctly without cross-contamination."""
    multi_resume = """
    Aarav Patel
    aarav@example.com | Bengaluru

    EDUCATION
    Indian Institute of Technology Bombay
    M.Tech in Artificial Intelligence
    2024 - 2026

    National Institute of Technology Karnataka
    B.Tech in Computer Science and Engineering
    2020 - 2024 | CGPA: 8.9/10

    Sri Chaitanya Junior College
    Class XII (Intermediate)
    2018 - 2020 | 95%
    """
    draft = parse_resume_to_draft(multi_resume)
    assert len(draft.education) == 3

    # M.Tech entry has its own graduation year (2026), not B.Tech's start or end
    assert "M.Tech" in (draft.education[0].degree or "")
    assert draft.education[0].graduation_year == 2026

    # B.Tech entry has its own graduation year (2024), not 2026 and not 2020
    assert "B.Tech" in (draft.education[1].degree or "")
    assert draft.education[1].graduation_year == 2024

    # High school entry has 2020, not 2026
    assert draft.education[2].graduation_year == 2020


def test_graduation_year_ambiguous_and_ongoing_preserves_uncertainty() -> None:
    """Verify ambiguous ongoing dates and start-only dates preserve uncertainty without fabricating."""
    # Ongoing with Present and no expected year specified -> None (does not fabricate)
    r1 = "EDUCATION\nIndian Institute of Technology\nB.Tech in Computer Science\n2023 - Present"
    assert parse_resume_to_draft(r1).education[0].graduation_year is None

    # Ongoing with Current -> None
    r2 = "EDUCATION\nNIT Warangal\nB.Tech in Information Technology\n2023 to Current"
    assert parse_resume_to_draft(r2).education[0].graduation_year is None

    # Ongoing with Present BUT with explicit expected year -> extracts expected year
    r3 = "EDUCATION\nNIT Trichy\nB.Tech in Computer Science\n2023 - Present (Expected Graduation: 2027)"
    assert parse_resume_to_draft(r3).education[0].graduation_year == 2027

    # Explicit start-only indicator -> None
    r4 = "EDUCATION\nCollege of Engineering\nB.Tech CSE\nJoined in 2023"
    assert parse_resume_to_draft(r4).education[0].graduation_year is None


# ==============================================================================
# 4. Candidate Session, CSRF, and IDOR Defense
# ==============================================================================


def test_candidate_session_signing_and_tampering() -> None:
    cand_id = "cand_12345678"
    token = sign_candidate_session(cand_id, SECRET_KEY)
    assert token.startswith(cand_id + ".")

    # Valid parsing
    parsed = parse_candidate_session(token, SECRET_KEY)
    assert parsed == cand_id

    # Tampered candidate ID
    tampered = "cand_attacker." + token.split(".")[1]
    assert parse_candidate_session(tampered, SECRET_KEY) is None

    # Invalid secret
    assert parse_candidate_session(token, "different_secret") is None

    # Malformed token
    assert parse_candidate_session("no_dot_here", SECRET_KEY) is None
    assert parse_candidate_session(None, SECRET_KEY) is None


def test_candidate_csrf_token_validation() -> None:
    cand_id = "cand_12345678"
    csrf = candidate_csrf_token(cand_id, SECRET_KEY)

    # Valid token
    assert validate_candidate_csrf(cand_id, csrf, SECRET_KEY) is True

    # Wrong candidate ID
    assert validate_candidate_csrf("cand_other", csrf, SECRET_KEY) is False

    # Empty or tampered token
    assert validate_candidate_csrf(cand_id, "invalid_csrf", SECRET_KEY) is False
    assert validate_candidate_csrf(cand_id, None, SECRET_KEY) is False

    # Candidate CSRF strictly rejects arbitrary or global tokens
    assert validate_candidate_csrf(cand_id, "global_secret", SECRET_KEY) is False

    # Unauthenticated onboarding page CSRF validation
    assert validate_onboarding_page_csrf("page_token_abc", "page_token_abc") is True
    assert validate_onboarding_page_csrf("wrong_token", "page_token_abc") is False
    assert validate_onboarding_page_csrf(None, "page_token_abc") is False
    assert validate_onboarding_page_csrf("page_token_abc", "") is False


def test_candidate_idor_ownership_verification() -> None:
    cand_a = Candidate(id="cand_a", name="Alice", email="a@example.com")
    cand_b = Candidate(id="cand_b", name="Bob", email="b@example.com")

    profile_a = CandidateProfile(
        id="prof_a",
        candidate_id=cand_a.id,
        version=1,
        is_active=True,
        status="confirmed",
        profile_data={},
    )

    # Owner has access
    assert verify_profile_ownership(cand_a.id, profile_a) is True

    # Attacker candidate B is rejected
    assert verify_profile_ownership(cand_b.id, profile_a) is False


# ==============================================================================
# 5. Web Onboarding & Review Endpoints
# ==============================================================================


@pytest.fixture
def web_client(repo: Repo, clock: FakeClock) -> TestClient:
    app = create_app(settings(), repo, clock)
    return TestClient(app, base_url="http://127.0.0.1:8080")


def test_onboarding_page_renders(web_client: TestClient) -> None:
    res = web_client.get("/onboarding")
    assert res.status_code == 200
    assert "Candidate Profile Onboarding" in res.text
    assert "Upload Resume" in res.text


def test_onboarding_manual_entry(web_client: TestClient, repo: Repo) -> None:
    res = web_client.get("/onboarding/manual", follow_redirects=False)
    assert res.status_code == 303
    redirect_url = res.headers["location"]
    assert "/onboarding/review/" in redirect_url

    # Candidate cookie should be set
    assert CANDIDATE_SESSION_COOKIE in res.cookies
    token = res.cookies[CANDIDATE_SESSION_COOKIE]
    cand_id = token.split(".")[0]
    candidate = repo.get_candidate(cand_id)
    assert candidate is not None


def test_onboarding_upload_and_review_flow(web_client: TestClient, repo: Repo) -> None:
    page_tok = repo.get_setting("web_token") or "token"
    sample_text = (
        "Dev Candidate\n"
        "dev@example.com | 9876543210\n"
        "EDUCATION\n"
        "B.Tech in Computer Science 2026\n"
        "SKILLS\n"
        "Python, FastAPI, SQL\n"
    )
    docx_bytes = create_minimal_docx(sample_text)

    # Multipart upload using Boundary format including CSRF token
    boundary = "----TestBoundary12345"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="token"\r\n\r\n'
            f"{page_tok}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="my_resume.docx"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
        ).encode()
        + docx_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    upload_res = web_client.post(
        "/onboarding/upload",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        follow_redirects=False,
    )
    assert upload_res.status_code == 303
    review_url = upload_res.headers["location"]
    assert review_url.split("/")[-1]

    # Verify session cookie was set
    assert CANDIDATE_SESSION_COOKIE in upload_res.cookies
    session_cookie = upload_res.cookies[CANDIDATE_SESSION_COOKIE]

    # Candidate can view review page
    review_res = web_client.get(
        review_url,
        cookies={CANDIDATE_SESSION_COOKIE: session_cookie},
    )
    assert review_res.status_code == 200
    assert "Review & Confirm Candidate Profile" in review_res.text
    assert "Dev Candidate" in review_res.text
    assert "dev@example.com" in review_res.text


def test_onboarding_review_idor_protection(web_client: TestClient, repo: Repo) -> None:
    # Create Candidate A and profile
    cand_a = repo.create_candidate(name="Candidate A", email="a@example.com")
    prof_a = repo.save_profile_draft(cand_a.id, None, {"basics": {"name": "Candidate A"}})

    # Create Candidate B
    cand_b = repo.create_candidate(name="Candidate B", email="b@example.com")

    # Get app web token for signature
    token_setting = repo.get_setting("web_token") or "token"
    cookie_b = sign_candidate_session(cand_b.id, token_setting)

    # Candidate B attempts to access Candidate A's profile
    res = web_client.get(
        f"/onboarding/review/{prof_a.id}",
        cookies={CANDIDATE_SESSION_COOKIE: cookie_b},
    )
    assert res.status_code == 403
    assert "403" in res.text

    # Candidate without cookie is denied
    unauth_res = web_client.get(f"/onboarding/review/{prof_a.id}")
    assert unauth_res.status_code == 401


def test_onboarding_confirm_profile_edits_and_csrf(web_client: TestClient, repo: Repo) -> None:
    cand = repo.create_candidate(name="Initial Name", email="init@example.com")
    prof = repo.save_profile_draft(
        cand.id,
        None,
        {"basics": {"name": "Initial Name", "email": "init@example.com"}, "skills": ["Python"]},
    )

    token_setting = repo.get_setting("web_token") or "token"
    cookie_cand = sign_candidate_session(cand.id, token_setting)
    valid_csrf = candidate_csrf_token(cand.id, token_setting)

    # 1. Attempt confirmation with invalid CSRF token -> 403
    bad_csrf_res = web_client.post(
        f"/onboarding/confirm/{prof.id}",
        data={"token": "bad_token", "name": "Updated Name"},
        cookies={CANDIDATE_SESSION_COOKIE: cookie_cand},
    )
    assert bad_csrf_res.status_code == 403

    # 2. Valid confirmation with updated fields
    confirm_res = web_client.post(
        f"/onboarding/confirm/{prof.id}",
        data={
            "token": valid_csrf,
            "name": "Confirmed Developer",
            "email": "confirmed@example.com",
            "skills": "Python, TypeScript, FastAPI, Redis",
            "degree": "B.Tech",
            "branch": "Computer Science",
            "institution": "Top NIT",
            "graduation_year": "2026",
            "cgpa": "9.1/10",
        },
        cookies={CANDIDATE_SESSION_COOKIE: cookie_cand},
        follow_redirects=False,
    )
    assert confirm_res.status_code == 303

    # Verify database updates
    active_prof = repo.get_active_profile(cand.id)
    assert active_prof is not None
    assert active_prof.status == "confirmed"
    assert active_prof.profile_data["basics"]["name"] == "Confirmed Developer"
    assert active_prof.profile_data["basics"]["email"] == "confirmed@example.com"
    assert "TypeScript" in active_prof.profile_data["skills"]
    assert "Redis" in active_prof.profile_data["skills"]
    assert active_prof.profile_data["education"][0]["institution"] == "Top NIT"
    assert active_prof.profile_data["education"][0]["graduation_year"] == 2026


def test_onboarding_upload_content_length_exceeded_413(web_client: TestClient, repo: Repo) -> None:
    page_tok = repo.get_setting("web_token") or "token"
    res = web_client.post(
        "/onboarding/upload",
        content=b"small_body",
        headers={
            "Content-Type": "multipart/form-data; boundary=boundary",
            "Content-Length": str(10 * 1024 * 1024),  # 10 MB > 6 MB limit
            "x-jobradar-token": page_tok,
        },
    )
    assert res.status_code == 413
    assert "413" in res.text


def test_onboarding_upload_streaming_body_cutoff_413(web_client: TestClient, repo: Repo) -> None:
    page_tok = repo.get_setting("web_token") or "token"
    # Create body larger than 6 MB limit (6.2 MB)
    large_chunk = b"A" * (6 * 1024 * 1024 + 200 * 1024)
    res = web_client.post(
        "/onboarding/upload",
        content=large_chunk,
        headers={
            "Content-Type": "multipart/form-data; boundary=boundary",
            "x-jobradar-token": page_tok,
        },
    )
    assert res.status_code == 413
    assert "413" in res.text


def test_onboarding_upload_csrf_validation_and_no_db_side_effects(
    web_client: TestClient, repo: Repo
) -> None:
    docx_bytes = create_minimal_docx("Candidate Name\nskills\nPython")
    boundary = "----TestCSRFBoundary"
    valid_file_part = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="resume.docx"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n"
        ).encode()
        + docx_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )

    # Count initial records
    initial_candidates = len(repo.list_candidates())

    # 1. Missing CSRF token -> 403 Forbidden
    res_no_token = web_client.post(
        "/onboarding/upload",
        content=valid_file_part,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert res_no_token.status_code == 403
    assert len(repo.list_candidates()) == initial_candidates

    # 2. Invalid CSRF token -> 403 Forbidden
    res_bad_token = web_client.post(
        "/onboarding/upload",
        content=valid_file_part,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-jobradar-token": "invalid_forged_token",
        },
    )
    assert res_bad_token.status_code == 403
    assert len(repo.list_candidates()) == initial_candidates

    # 3. Valid page token for first-time unauthenticated onboarding -> 303 Redirect
    page_tok = repo.get_setting("web_token") or "token"
    res_valid = web_client.post(
        "/onboarding/upload",
        content=valid_file_part,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-jobradar-token": page_tok,
        },
        follow_redirects=False,
    )
    assert res_valid.status_code == 303
    assert len(repo.list_candidates()) == initial_candidates + 1
    session_cookie = res_valid.cookies[CANDIDATE_SESSION_COOKIE]
    cand_id = session_cookie.split(".")[0]

    # 4. Authenticated candidate session with candidate-bound CSRF token
    cand_csrf = candidate_csrf_token(cand_id, page_tok)
    res_cand_valid = web_client.post(
        "/onboarding/upload",
        content=valid_file_part,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-jobradar-token": cand_csrf,
        },
        cookies={CANDIDATE_SESSION_COOKIE: session_cookie},
        follow_redirects=False,
    )
    assert res_cand_valid.status_code == 303
    # Confirms it re-used the same candidate (no extra candidate created)
    assert len(repo.list_candidates()) == initial_candidates + 1

    # 5. Authenticated candidate with forged/invalid candidate CSRF token -> 403
    res_cand_bad_csrf = web_client.post(
        "/onboarding/upload",
        content=valid_file_part,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-jobradar-token": "forged_cand_csrf",
        },
        cookies={CANDIDATE_SESSION_COOKIE: session_cookie},
        follow_redirects=False,
    )
    assert res_cand_bad_csrf.status_code == 403

    # 6. Authenticated candidate submitting with valid master page_token -> 303
    res_cand_page_tok = web_client.post(
        "/onboarding/upload",
        content=valid_file_part,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "x-jobradar-token": page_tok,
        },
        cookies={CANDIDATE_SESSION_COOKIE: session_cookie},
        follow_redirects=False,
    )
    assert res_cand_page_tok.status_code == 303


def test_profile_version_collision_safe_reconfirmation(repo: Repo) -> None:
    cand = repo.create_candidate(name="Collision Test", email="collision@example.com")
    draft = repo.save_profile_draft(cand.id, None, {"basics": {"name": "V1"}})

    # First confirmation -> v1 confirmed
    v1 = repo.confirm_profile(draft.id)
    assert v1.version == 1
    assert v1.status == "confirmed"
    assert v1.is_active is True

    # Second confirmation -> creates v2
    v2 = repo.confirm_profile(v1.id, {"basics": {"name": "V2"}})
    assert v2.version == 2
    assert v2.status == "confirmed"
    assert v2.is_active is True
    # v1 is now deactivated
    v1_refreshed = repo.get_profile(v1.id)
    assert v1_refreshed is not None
    assert v1_refreshed.is_active is False

    # Third confirmation from v2 -> creates v3
    v3 = repo.confirm_profile(v2.id, {"basics": {"name": "V3"}})
    assert v3.version == 3
    assert v3.status == "confirmed"
    assert v3.is_active is True

    # Re-confirming an earlier version (v1) must NOT attempt version 2 (which already exists!)
    # It must monotonically increment to max_ver + 1 == 4!
    v4 = repo.confirm_profile(v1.id, {"basics": {"name": "V4 derived from V1"}})
    assert v4.version == 4
    assert v4.status == "confirmed"
    assert v4.is_active is True

    # Verify that only v4 is active and all previous profiles are inactive
    active = repo.get_active_profile(cand.id)
    assert active is not None
    assert active.id == v4.id
    assert active.version == 4

    profiles = repo.get_candidate_profiles(cand.id)
    assert [p.version for p in profiles] == [4, 3, 2, 1]
    assert [p.is_active for p in profiles] == [True, False, False, False]
