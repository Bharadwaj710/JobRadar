"""Resume file validation and text extraction for PDF and DOCX documents.

Keeps extraction logic separate from API routes and UI code. Strictly enforces
content validation, magic bytes, size limits (5 MB max), and path safety.
"""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Literal

import pypdf

MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5 MB
MIN_TEXT_CHARS = 50

PDF_MAGIC = b"%PDF"
DOCX_MAGIC = b"PK\x03\x04"

ExtractionStatus = Literal["extracted", "empty_or_scanned", "unreadable", "failed"]


class ExtractionError(Exception):
    """Raised when file validation or extraction fails."""


@dataclass(frozen=True)
class ExtractedResume:
    raw_text: str
    file_type: str
    original_filename: str
    status: ExtractionStatus
    note: str | None = None
    extracted_urls: tuple[str, ...] = ()


def sanitize_filename(name: str) -> str:
    """Strip directories and non-safe characters from client-provided filename."""
    base = os.path.basename(name).strip()
    clean = re.sub(r"[^\w.\-_ ]", "_", base)
    return clean[:120] or "resume"


def validate_upload_content(file_bytes: bytes, original_filename: str) -> tuple[str, str]:
    """Validate file size, extension, and magic bytes.

    Returns (sanitized_filename, file_type: 'pdf' | 'docx').
    Raises ExtractionError on invalid file.
    """
    if not file_bytes:
        raise ExtractionError("Uploaded file is empty (0 bytes).")

    if len(file_bytes) > MAX_UPLOAD_SIZE:
        raise ExtractionError(
            f"File size ({len(file_bytes) / 1024 / 1024:.2f} MB) exceeds the 5 MB limit."
        )

    clean_name = sanitize_filename(original_filename)
    lower = clean_name.lower()

    if lower.endswith(".pdf"):
        if not file_bytes.startswith(PDF_MAGIC):
            raise ExtractionError(
                "File has .pdf extension but does not contain a valid PDF signature."
            )
        return clean_name, "pdf"

    if lower.endswith(".docx"):
        if not file_bytes.startswith(DOCX_MAGIC):
            raise ExtractionError(
                "File has .docx extension but is not a valid OpenXML ZIP archive."
            )
        # Verify it actually contains word/document.xml
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                if "word/document.xml" not in z.namelist():
                    raise ExtractionError(
                        "Corrupted DOCX: 'word/document.xml' missing from archive."
                    )
        except zipfile.BadZipFile as e:
            raise ExtractionError(f"Corrupted or invalid DOCX archive: {e}") from e
        return clean_name, "docx"

    raise ExtractionError(
        "Unsupported file type. Please upload a PDF (.pdf) or Word document (.docx)."
    )


def extract_text_from_pdf(
    file_bytes: bytes,
) -> tuple[str, ExtractionStatus, str | None, list[str]]:
    """Extract text and hyperlinks from PDF using pypdf.

    Handles encryption, image-only scans, and link annotations.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        if reader.is_encrypted:
            # Try decrypting with blank password
            try:
                decrypted = reader.decrypt("")
                if decrypted == pypdf.PasswordType.NOT_DECRYPTED:
                    return (
                        "",
                        "unreadable",
                        "Password-protected PDF cannot be read without password.",
                        [],
                    )
            except Exception:
                return "", "unreadable", "Password-protected or encrypted PDF.", []

        pages_text: list[str] = []
        links: list[str] = []
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
                if text.strip():
                    pages_text.append(text.strip())
            except Exception as e:
                pages_text.append(f"[Error reading page {i + 1}: {e}]")

            # Extract hyperlink annotations (/Annots with /Link and /URI)
            try:
                annots = page.get("/Annots")
                if annots:
                    for a in annots:
                        try:
                            obj = a.get_object()
                            if obj and obj.get("/Subtype") == "/Link" and "/A" in obj:
                                action = obj["/A"].get_object()
                                if action and action.get("/S") == "/URI" and "/URI" in action:
                                    uri = str(action["/URI"]).strip()
                                    if uri and uri not in links:
                                        links.append(uri)
                        except (KeyError, TypeError, ValueError, AttributeError):
                            continue
            except (KeyError, TypeError, ValueError, AttributeError):
                pass

        full_text = "\n\n".join(pages_text).strip()
        if len(full_text) < MIN_TEXT_CHARS:
            return (
                full_text,
                "empty_or_scanned",
                (
                    "Resume appears to be scanned or image-only. "
                    "Text extraction returned little or no content."
                ),
                links,
            )

        # If any extracted document hyperlinks are not present in the body text,
        # append them cleanly so they are preserved in raw_text for search and display.
        if links:
            missing_links = [link for link in links if link not in full_text]
            if missing_links:
                full_text += "\n\n" + "\n".join(missing_links)

        return full_text, "extracted", None, links
    except Exception as e:
        return "", "failed", f"Failed to parse PDF document: {e}", []


def extract_text_from_docx(
    file_bytes: bytes,
) -> tuple[str, ExtractionStatus, str | None, list[str]]:
    """Extract text and hyperlinks from DOCX archive."""
    try:
        links: list[str] = []
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            xml_content = z.read("word/document.xml")
            rels_name = "word/_rels/document.xml.rels"
            if rels_name in z.namelist():
                try:
                    rels_xml = z.read(rels_name)
                    root_rels = ET.fromstring(rels_xml)  # noqa: S314 (internal DOCX XML parse)
                    for elem in root_rels:
                        target = elem.attrib.get("Target", "").strip()
                        is_hyperlink = (
                            target.startswith("http://")
                            or target.startswith("https://")
                            or target.startswith("mailto:")
                        )
                        if target and is_hyperlink and target not in links:
                            links.append(target)
                except (KeyError, TypeError, ValueError, ET.ParseError):
                    pass

        root = ET.fromstring(xml_content)  # noqa: S314 (internal DOCX XML parse)
        # XML namespace for WordprocessingML
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

        paragraphs: list[str] = []
        # Walk all paragraph elements
        for p in root.findall(".//w:p", ns):
            texts = [node.text for node in p.findall(".//w:t", ns) if node.text]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)

        full_text = "\n\n".join(paragraphs).strip()
        if len(full_text) < MIN_TEXT_CHARS:
            return (
                full_text,
                "empty_or_scanned",
                "Document contains very little readable text. Please verify content.",
                links,
            )

        if links:
            missing_links = [link for link in links if link not in full_text]
            if missing_links:
                full_text += "\n\n" + "\n".join(missing_links)

        return full_text, "extracted", None, links
    except Exception as e:
        return "", "failed", f"Failed to parse DOCX document: {e}", []


def extract_resume_content(file_bytes: bytes, filename: str) -> ExtractedResume:
    """High-level function validating file and extracting text and links."""
    clean_name, file_type = validate_upload_content(file_bytes, filename)

    if file_type == "pdf":
        text, status, note, links = extract_text_from_pdf(file_bytes)
    else:
        text, status, note, links = extract_text_from_docx(file_bytes)

    return ExtractedResume(
        raw_text=text,
        file_type=file_type,
        original_filename=clean_name,
        status=status,
        note=note,
        extracted_urls=tuple(links),
    )
