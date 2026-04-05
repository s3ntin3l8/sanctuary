from __future__ import annotations

import os
import re
import aiofiles
import asyncio
from typing import Optional
from datetime import timedelta
from fastapi import UploadFile, HTTPException
from sqlalchemy.orm import Session
from app.models.database import Document, OriginatorType
from app.services.normalization import normalize_hm

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".pptx", ".xlsx"}

# ---------------------------------------------------------------------------
# Lazy converter initialization
# ---------------------------------------------------------------------------

_converter: Optional[object] = None


def _get_converter():
    """Lazy-init the Docling DocumentConverter on first use."""
    global _converter
    if _converter is None:
        try:
            from docling.document_converter import DocumentConverter

            _converter = DocumentConverter()
        except Exception as e:
            raise RuntimeError(
                f"Failed to initialize Docling converter: {e}. "
                "Ensure Docling is installed and system dependencies are met."
            )
    return _converter


class IngestionError(Exception):
    """Structured error for ingestion pipeline failures."""

    def __init__(self, message: str, detail: str | None = None):
        self.message = message
        self.detail = detail
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Heuristic metadata extraction from filename + content
# ---------------------------------------------------------------------------

# Common case-ID patterns: ADV-992-K, REF-441-22, 2023-CV-01234, German court file numbers
CASE_ID_PATTERNS = [
    re.compile(r"\b(ADV-\d{3,4}-[A-Z]{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(REF-\d{3,4}-\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{4}-CV-\d{4,6})\b", re.IGNORECASE),
    re.compile(r"\b(Case\s*#?\s*\d{3,6}-?[A-Z]{0,3})\b", re.IGNORECASE),
    # German court file numbers: AZ-123-A, AZ 123 A
    re.compile(r"\b(AZ[-\s]?\d{2,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    # CASE-123-X pattern
    re.compile(r"\b(CASE[-\s]?\d{3,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    # German court file: 12/2024, 123/2024
    re.compile(r"\b(\d{1,4}/\d{2,4})\b"),
]

# Filename-only case ID patterns (broader, used as fallback when content yields nothing)
FILENAME_CASE_ID_PATTERNS = [
    re.compile(r"\b(ADV-\d{3,4}-[A-Z]{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(AZ[-\s]?\d{2,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(CASE[-\s]?\d{3,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,4}/\d{2,4})\b"),
    re.compile(r"\b(REF-\d{3,4}-\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{4}-CV-\d{4,6})\b", re.IGNORECASE),
]

# Originator keywords with weighted specificity scores
COURT_KEYWORDS = {
    "court order": 2,
    "court clerk": 3,
    "judge": 1,
    "subpoena": 3,
    "summons": 3,
    "notice of motion": 2,
    "ruling": 2,
    "decree": 2,
    "judgment": 2,
    "tribunal": 2,
    "magistrate": 3,
    "docket": 1,
    "hereby orders": 3,
    "it is ordered": 2,
    # German court keywords
    "beschluss": 3,
    "verfügung": 3,
    "urteil": 3,
    "termin": 1,
    "ladung": 2,
    "zustellung": 2,
    "gericht": 1,
    "amtsgericht": 4,
    "landgericht": 4,
    "oberlandesgericht": 5,
    "bundesgerichtshof": 5,
}
OPPOSING_KEYWORDS = {
    "opposing counsel": 3,
    "defendant": 1,
    "respondent": 1,
    "counter-claim": 2,
    "demand letter": 3,
    "settlement offer": 2,
    "plaintiff": 1,
    "claimant": 1,
    "blake & torres": 3,
    "counter-offer": 2,
    # German opposing counsel keywords
    "klage": 2,
    "widerspruch": 2,
    "antrag des klägers": 3,
    "antrag des klaegers": 3,
    "gegner": 2,
    "beklagte": 2,
    "anwalt des klägers": 4,
    "anwalt des klaegers": 4,
}
OWN_KEYWORDS = {
    "our client": 2,
    "memo to file": 2,
    "internal memo": 2,
    "work product": 2,
    "privileged": 1,
    "expert witness": 2,
    "draft": 1,
    "strategy": 1,
    # German own counsel keywords
    "unser mandant": 3,
    "wir vertreten": 3,
    "im namen unserer": 3,
    "kanzlei": 2,
    "rechtsanwalt": 1,
}

# Date patterns in content (ordered by specificity: contextual first, then absolute)
DATE_PATTERNS = [
    re.compile(
        r"(?:received|dated|filed|sent|vom|eingegangen)\s+(?:on\s+|am\s+|vom\s+)?(\w+ \d{1,2},? \d{4})",
        re.IGNORECASE,
    ),
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})"),
    re.compile(r"(\d{1,2}\.\d{1,2}\.\d{4})"),
    re.compile(r"(\d{1,2}\.\d{1,2}\.\d{2})"),
]

# Filename date patterns (fallback)
FILENAME_DATE_PATTERNS = [
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    re.compile(r"(\d{4}\d{2}\d{2})"),
    re.compile(r"(\d{2}-\d{2}-\d{4})"),
    re.compile(r"(\d{8})"),
]

# German month names for DD. Month YYYY parsing
GERMAN_MONTHS = {
    "januar": 1,
    "jan": 1,
    "februar": 2,
    "feb": 2,
    "marz": 3,
    "mrz": 3,
    "mars": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "juni": 6,
    "jun": 6,
    "juli": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "oktober": 10,
    "okt": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "dez": 12,
}

# Pattern for DD. Month YYYY (German long form)
GERMAN_LONG_DATE_PATTERN = re.compile(
    r"\b(\d{1,2})\.\s+"
    r"(Januar|Februar|Marz|Mrz|Mars|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember|"
    r"Jan|Feb|Mrz|Mar|Apr|Mai|Jun|Jul|Aug|Sep|Sept|Okt|Nov|Dez)"
    r"\s+(\d{4})\b",
    re.IGNORECASE,
)

# Sender patterns (ordered by priority: explicit headers first, then contextual)
SENDER_PATTERNS = [
    # From: header (email style)
    re.compile(r"^[Ff]rom:\s*(.+)$", re.MULTILINE),
    # Von: header (German email style)
    re.compile(r"^[Vv]on:\s*(.+)$", re.MULTILINE),
    # From/Sender/By/Signed/Von/Absender with name
    re.compile(
        r"(?:from|sender|by|signed|submitted by|von|absender)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        re.IGNORECASE,
    ),
    # From/Sender with firm name
    re.compile(
        r"(?:from|sender|von|absender)[:\s]+([A-Z][a-z]+ (?:&|and) [A-Z][a-z]+ (?:LLP|LLC|PC|PLLC|GmbH|AG|KG|e\.K\.))",
        re.IGNORECASE,
    ),
    # Sehr geehrte(r) followed by name
    re.compile(
        r"(?:Sehr geehrte[rms]+)\s+([A-Z][a-z]+ [A-Z][a-z]+)",
        re.IGNORECASE,
    ),
]

# Signature block patterns (look at end of document)
SIGNATURE_PATTERNS = [
    re.compile(
        r"(?:Mit freundlichen Grüßen|Mit freundlichen Grüssen|Kind regards|Best regards|Sincerely|Yours faithfully)\s*\n\s*([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:Rechtsanwalt|Rechtsanwältin|Attorney|Counsel|Lawyer)\s+([A-Z][a-z]+ [A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        re.IGNORECASE,
    ),
]

ABSOLUTE_DATE_PATTERN = re.compile(
    r"\b("
    r"(?:Jan(?:uary|uar)?|Feb(?:ruary|ruar)?|Mar(?:ch|z)?|Apr(?:il)?|May|Mai|Jun(?:e|i)?|Jul(?:y|i)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Okt(?:ober)?|Oct(?:ober)?|Nov(?:ember)?|Dez(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r"|\d{1,2}\.\d{1,2}\.\d{4}"
    r"|\d{1,2}\.\d{1,2}\.\d{2}"
    r")\b",
    re.IGNORECASE,
)
HEARING_KEYWORDS = (
    "hearing",
    "conference",
    "appearance",
    "oral argument",
    "trial",
    "status conference",
)
DEADLINE_KEYWORDS = (
    "deadline",
    "due",
    "respond",
    "response",
    "file",
    "serve",
    "submit",
    "production",
)
RELATIVE_DEADLINE_PATTERN = re.compile(
    r"(?:within|no later than)\s+(\d{1,3})\s+days?\s+(?:of|after|from)\s+(?:receipt|service|receipt of this notice|the order|this order|filing)",
    re.IGNORECASE,
)

RELATIVE_DAYS_PATTERNS = [
    re.compile(r"innerhalb\s+von\s+(\d+)\s+Tagen", re.IGNORECASE),
    re.compile(r"within\s+(\d+)\s+days?", re.IGNORECASE),
    re.compile(r"Frist\s+von\s+(\d+)\s+Wochen", re.IGNORECASE),
    re.compile(r"respond\s+within\s+(\d+)\s+days?", re.IGNORECASE),
]

BY_DATE_PATTERNS = [
    re.compile(r"bis\s+zum\s+(.+?)(?:\.|$|,)", re.IGNORECASE),
    re.compile(r"by\s+(\w+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE),
    re.compile(r"no\s+later\s+than\s+(\w+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE),
]

DEADLINE_LABEL_PATTERN = re.compile(
    r"(?:deadline|answer\s+due|file\s+by)[:\s]+(.+?)(?:\.|$|,)", re.IGNORECASE
)

# Common email/document headers to skip when extracting title from content
HEADER_PREFIXES = (
    "from:",
    "to:",
    "cc:",
    "bcc:",
    "date:",
    "subject:",
    "sent:",
    "received:",
    "von:",
    "an:",
    "datum:",
    "betrifft:",
    "betreff:",
    "email:",
    "phone:",
    "fax:",
    "ref:",
    "reference:",
    "case id:",
    "file number:",
    "aktenzeichen:",
)

MAX_TITLE_LENGTH = 120


def extract_case_id(filename: str, content: str) -> str | None:
    """Try to extract a case ID from content first, then from filename as fallback."""
    # Search first 2000 chars of content for performance
    snippet = (content or "")[:2000]
    for pattern in CASE_ID_PATTERNS:
        match = pattern.search(snippet)
        if match:
            return match.group(1).upper()
    # Fallback: check filename with primary patterns
    for pattern in CASE_ID_PATTERNS:
        match = pattern.search(filename)
        if match:
            return match.group(1).upper()
    # Final fallback: check filename with broader patterns
    for pattern in FILENAME_CASE_ID_PATTERNS:
        match = pattern.search(filename)
        if match:
            return match.group(1).upper()
    return None


def extract_originator(filename: str, content: str) -> OriginatorType:
    """Classify originator based on weighted keyword matching."""
    combined = (filename + " " + (content or "")[:3000]).lower()
    court_score = sum(weight for kw, weight in COURT_KEYWORDS.items() if kw in combined)
    opposing_score = sum(
        weight for kw, weight in OPPOSING_KEYWORDS.items() if kw in combined
    )
    own_score = sum(weight for kw, weight in OWN_KEYWORDS.items() if kw in combined)

    best = max(court_score, opposing_score, own_score)
    if best == 0:
        return OriginatorType.UNKNOWN
    if court_score == best:
        return OriginatorType.COURT
    if opposing_score == best:
        return OriginatorType.OPPOSING
    return OriginatorType.OWN


def _parse_date_string(date_str: str):
    """Parse a date string in various formats including German."""
    from datetime import datetime as dt

    cleaned = date_str.strip().replace(",", "")

    # Try standard formats first
    for fmt in ("%B %d %Y", "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.strptime(cleaned, fmt)
        except ValueError:
            continue

    # Try German DD.MM.YYYY
    german_short = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})$", cleaned)
    if german_short:
        day, month, year = (
            int(german_short.group(1)),
            int(german_short.group(2)),
            int(german_short.group(3)),
        )
        try:
            return dt(year, month, day)
        except ValueError:
            pass

    # Try German DD.MM.YY
    german_short2 = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{2})$", cleaned)
    if german_short2:
        day, month, year = (
            int(german_short2.group(1)),
            int(german_short2.group(2)),
            int(german_short2.group(3)),
        )
        year = 2000 + year if year < 100 else year
        try:
            return dt(year, month, day)
        except ValueError:
            pass

    return None


def extract_received_date(content: str, filename: str = ""):
    """Try to extract a date from document content, with filename fallback."""
    from datetime import datetime as dt

    snippet = (content or "")[:3000]

    # 1. Try contextual date patterns (received/dated/filed/etc.)
    for pattern in DATE_PATTERNS:
        match = pattern.search(snippet)
        if match:
            date_str = match.group(1)
            parsed = _parse_date_string(date_str)
            if parsed:
                return parsed

    # 2. Try German long form: DD. Month YYYY (e.g. "5. Januar 2024")
    german_match = GERMAN_LONG_DATE_PATTERN.search(snippet)
    if german_match:
        day = int(german_match.group(1))
        month_name = german_match.group(2).lower()
        year = int(german_match.group(3))
        month = GERMAN_MONTHS.get(month_name)
        if month:
            try:
                return dt(year, month, day)
            except ValueError:
                pass

    # 3. Try absolute date pattern as fallback
    absolute_match = ABSOLUTE_DATE_PATTERN.search(snippet)
    if absolute_match:
        parsed = _parse_candidate_date(absolute_match.group(1))
        if parsed:
            return parsed

    # 4. Fallback: check filename for date patterns
    if filename:
        for pattern in FILENAME_DATE_PATTERNS:
            match = pattern.search(filename)
            if match:
                date_str = match.group(1)
                # Handle YYYYMMDD compact format
                if re.match(r"^\d{8}$", date_str):
                    try:
                        return dt.strptime(date_str, "%Y%m%d")
                    except ValueError:
                        pass
                parsed = _parse_date_string(date_str)
                if parsed:
                    return parsed

    return None


def extract_sender(content: str) -> str | None:
    """Try to extract a sender name from content."""
    snippet = (content or "")[:3000]

    # 1. Try explicit sender patterns first (From:, Von:, etc.)
    for pattern in SENDER_PATTERNS:
        match = pattern.search(snippet)
        if match:
            sender = match.group(1).strip()
            # Filter out empty or too-short matches
            if len(sender) >= 3:
                return sender

    # 2. Try signature block patterns at end of document
    tail = (content or "")[-2000:]
    for pattern in SIGNATURE_PATTERNS:
        match = pattern.search(tail)
        if match:
            sender = match.group(1).strip()
            if len(sender) >= 3:
                return sender

    return None


def _parse_candidate_date(raw_value: str):
    """Parse a single absolute date string into a datetime."""
    from datetime import datetime as dt

    cleaned = raw_value.strip().replace(",", "")
    for fmt in ("%B %d %Y", "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.strptime(cleaned, fmt)
        except ValueError:
            continue
    # Try German month names: "5. Januar 2024"
    german_match = GERMAN_LONG_DATE_PATTERN.search(cleaned)
    if german_match:
        day = int(german_match.group(1))
        month_str = german_match.group(2).lower()
        year = int(german_match.group(3))
        month = GERMAN_MONTHS.get(month_str)
        if month:
            try:
                return dt(year, month, day)
            except ValueError:
                pass
    return None


def extract_schedule_candidates(content: str, base_date=None) -> list[dict]:
    """
    Extract likely hearing/deadline candidates from document content.
    This is heuristic on purpose: we want promotion hooks, not full AI extraction yet.
    """
    from datetime import datetime as dt

    candidates: list[dict] = []
    seen: set[tuple] = set()
    snippet = (content or "")[:5000]
    lines = [line.strip(" -*\t") for line in snippet.splitlines() if line.strip()]

    for line in lines:
        lowered = line.lower()
        absolute_match = ABSOLUTE_DATE_PATTERN.search(line)

        if absolute_match and any(keyword in lowered for keyword in HEARING_KEYWORDS):
            scheduled_for = _parse_candidate_date(absolute_match.group(1))
            if scheduled_for:
                title = "Court hearing"
                if "status conference" in lowered:
                    title = "Status conference"
                elif "settlement conference" in lowered:
                    title = "Settlement conference"
                elif "trial" in lowered:
                    title = "Trial setting"
                elif "motion" in lowered:
                    title = "Motion hearing"
                key = ("hearing", title, scheduled_for.isoformat())
                if key not in seen:
                    candidates.append(
                        {
                            "type": "hearing",
                            "title": title,
                            "description": line[:220],
                            "scheduled_for": scheduled_for,
                        }
                    )
                    seen.add(key)

        if absolute_match and any(keyword in lowered for keyword in DEADLINE_KEYWORDS):
            due_at = _parse_candidate_date(absolute_match.group(1))
            if due_at:
                title = "Filing deadline"
                if "respond" in lowered or "response" in lowered:
                    title = "Response deadline"
                elif "serve" in lowered:
                    title = "Service deadline"
                elif "submit" in lowered:
                    title = "Submission deadline"
                key = ("deadline", title, due_at.isoformat())
                if key not in seen:
                    candidates.append(
                        {
                            "type": "deadline",
                            "title": title,
                            "description": line[:220],
                            "due_at": due_at,
                        }
                    )
                    seen.add(key)

        if base_date:
            relative_match = RELATIVE_DEADLINE_PATTERN.search(line)
            if relative_match:
                due_at = base_date + timedelta(days=int(relative_match.group(1)))
                title = "Relative response deadline"
                key = ("deadline", title, due_at.isoformat())
                if key not in seen:
                    candidates.append(
                        {
                            "type": "deadline",
                            "title": title,
                            "description": line[:220],
                            "due_at": due_at,
                        }
                    )
                    seen.add(key)

        for pattern in RELATIVE_DAYS_PATTERNS:
            relative_match = pattern.search(line)
            if relative_match:
                days = int(relative_match.group(1))
                if "wochen" in lowered:
                    days *= 7
                ref_date = base_date if base_date else dt.utcnow()
                due_at = ref_date + timedelta(days=days)
                title = "Response deadline"
                key = ("deadline", title, due_at.isoformat())
                if key not in seen:
                    candidates.append(
                        {
                            "type": "deadline",
                            "title": title,
                            "description": line[:220],
                            "due_at": due_at,
                        }
                    )
                    seen.add(key)

        for pattern in BY_DATE_PATTERNS:
            by_match = pattern.search(line)
            if by_match:
                date_str = by_match.group(1).strip()
                due_at = _parse_date_string(date_str)
                if due_at:
                    title = "Filing deadline"
                    if "bis zum" in lowered:
                        title = "Fristende"
                    key = ("deadline", title, due_at.isoformat())
                    if key not in seen:
                        candidates.append(
                            {
                                "type": "deadline",
                                "title": title,
                                "description": line[:220],
                                "due_at": due_at,
                            }
                        )
                        seen.add(key)

        label_match = DEADLINE_LABEL_PATTERN.search(line)
        if label_match:
            date_str = label_match.group(1).strip()
            due_at = _parse_date_string(date_str)
            if due_at:
                if "answer due" in lowered:
                    title = "Answer deadline"
                elif "file by" in lowered:
                    title = "Filing deadline"
                else:
                    title = "Deadline"
                key = ("deadline", title, due_at.isoformat())
                if key not in seen:
                    candidates.append(
                        {
                            "type": "deadline",
                            "title": title,
                            "description": line[:220],
                            "due_at": due_at,
                        }
                    )
                    seen.add(key)

    candidates.sort(key=lambda item: item.get("due_at") or item.get("scheduled_for"))
    return candidates[:6]


EMAIL_HEADER_KEYS = {
    "from:",
    "to:",
    "date:",
    "subject:",
    "cc:",
    "bcc:",
    "sent:",
    "received:",
    "reply-to:",
    "message-id:",
    "mime-version:",
    "content-type:",
    "content-transfer-encoding:",
}


def extract_clean_title(filename: str, content: str = "") -> str:
    """Convert filename into a human-readable title, or extract from content."""
    # Try to find a Subject: line in content
    if content:
        subject_match = re.search(r"^[Ss]ubject:\s*(.+)$", content, re.MULTILINE)
        if subject_match:
            title = subject_match.group(1).strip()
            if title and len(title) > 2:
                return title[:120]

    # Use first non-empty, non-header line from content
    if content:
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().split(":")[0] + ":" in EMAIL_HEADER_KEYS:
                continue
            if stripped.startswith("---") or stripped.startswith("==="):
                continue
            title = stripped.strip("#*-_ ")
            if title and len(title) > 2:
                return title[:120]

    # Fallback: convert filename to title
    name = os.path.splitext(filename)[0]
    name = re.sub(r"[_\-]+", " ", name)
    for pattern in CASE_ID_PATTERNS:
        name = pattern.sub("", name)
    name = name.strip()
    if name:
        return name.title()[:120]
    return filename[:120]


def compute_review_reasons(doc: Document) -> list[str]:
    """
    Compute a list of review reason codes based on which metadata fields
    are still missing or were not extracted. This is the single source of
    truth for whether a document needs human review.
    """
    reasons = []
    if not doc.case_id:
        reasons.append("missing_case_id")
    if doc.originator_type == OriginatorType.UNKNOWN:
        reasons.append("missing_originator")
    if not doc.sender:
        reasons.append("missing_sender")
    if not doc.received_date:
        reasons.append("missing_received_date")
    # Title is the raw filename — still counts as "needs review" for renaming
    if (
        doc.title
        and "." in doc.title
        and doc.title == os.path.basename(doc.file_path or "")
    ):
        reasons.append("missing_title")
    if not doc.content or len(doc.content.strip()) < 20:
        reasons.append("missing_content")
    if doc.content and "Conversion failed:" in doc.content:
        reasons.append("conversion_failed")
    return reasons


# ---------------------------------------------------------------------------
# Main ingestion pipeline
# ---------------------------------------------------------------------------


async def ingest_file(
    file: UploadFile,
    case_id: Optional[str] = None,
    db: Session = None,
    parent_id: int = None,
) -> Document:
    """
    Saves an uploaded file to a local directory grouped by case_id,
    converts it to Markdown using Docling, runs heuristic metadata
    extraction, and stores the result in the database.

    Documents with incomplete metadata are flagged for triage review.
    Corrupt or unsupported files are stored with a failure marker for review.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # 1. Ensure the destination directory exists
    case_dir = os.path.join("./data", case_id or "_triage")
    os.makedirs(case_dir, exist_ok=True)

    # Secure the filename (basic safety)
    safe_filename = os.path.basename(file.filename)
    file_path = os.path.join(case_dir, safe_filename)

    # 2. Save the file to disk asynchronously
    try:
        async with aiofiles.open(file_path, "wb") as out_file:
            while content := await file.read(1024 * 1024):  # 1MB chunks
                await out_file.write(content)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save uploaded file: {e}",
        )

    # 3. Convert to markdown with docling
    markdown_content: str | None = None
    conversion_error: str | None = None

    def convert_to_md(path: str) -> str:
        conv = _get_converter()
        result = conv.convert(path)
        return result.document.export_to_markdown()

    try:
        markdown_content = await asyncio.to_thread(convert_to_md, file_path)
        markdown_content = normalize_hm(markdown_content)
    except Exception as e:
        conversion_error = str(e)
        markdown_content = f"Conversion failed: {conversion_error}"

    # 4. Heuristic metadata extraction
    extracted_case_id = extract_case_id(safe_filename, markdown_content)
    extracted_originator = extract_originator(safe_filename, markdown_content)
    extracted_date = extract_received_date(markdown_content, safe_filename)
    extracted_sender = extract_sender(markdown_content)
    extracted_title = extract_clean_title(safe_filename, markdown_content)

    # Prefer the explicitly provided case_id, fall back to extraction
    final_case_id = case_id if case_id else extracted_case_id

    # 5. Build the document
    new_doc = Document(
        title=extracted_title if extracted_title != safe_filename else safe_filename,
        content=markdown_content,
        case_id=final_case_id,
        file_path=file_path,
        parent_id=parent_id,
        originator_type=extracted_originator,
        sender=extracted_sender,
        received_date=extracted_date,
    )

    # 6. Compute review reasons BEFORE persisting
    reasons = compute_review_reasons(new_doc)
    new_doc.review_reasons = reasons
    new_doc.needs_review = len(reasons) > 0

    # 7. Persist with rollback safety
    try:
        db.add(new_doc)
        db.commit()
        db.refresh(new_doc)
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Database error while saving document: {e}",
        )

    return new_doc
