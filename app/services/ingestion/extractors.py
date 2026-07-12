import re
from datetime import UTC, datetime
from typing import TypedDict

from app.models.enums import CaseType, ProceedingCourtLevel

CASE_ID_PATTERNS = [
    re.compile(r"(?:\||^|\s+)(ADV-\d{3,4}-[A-Z]{1,3})\b", re.IGNORECASE),
    re.compile(r"(?:\||^|\s+)(REF-\d{3,4}-\d{1,3})\b", re.IGNORECASE),
    re.compile(r"(?:\||^|\s+)(\d{4}-CV-\d{4,6})\b", re.IGNORECASE),
    re.compile(r"(?:\||^|\s+)(Case\s*#?\s*\d{3,6}-?[A-Z]{0,3})\b", re.IGNORECASE),
    re.compile(r"(?:\||^|\s+)(AZ[-\s]?\d{2,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    re.compile(r"(?:\||^|\s+)(CASE[-\s]?\d{3,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    # German Court ID: e.g. "003 F 426/25", "003F426/25", "003F 426/25", "003 F426/25"
    re.compile(
        r"(?:\||^|\s+)(\d{1,3}\s?[A-Z]{1,2}\s?\d{1,5}/\d{2,4})\b", re.IGNORECASE
    ),
    # German Court ID with dash: e.g. "003-F-426/25"
    re.compile(r"(?:\||^|\s+)(\d{1,3}-[A-Z]{1,2}-\d{1,5}/\d{2,4})\b", re.IGNORECASE),
    # Lawyer/Court ID: e.g. 8124/25 or 426/25
    re.compile(r"(?:\||^|\s+)(\d{1,5}/\d{2,4})\b"),
]

# Anchor-first pass for extract_case_id: Aktenzeichen / Geschäftszeichen are
# canonical labels in German court letters. Match with a wide (20k) window because
# the reference block reliably appears in the header — never in tail noise.
ANCHOR_CASE_ID_PATTERN = re.compile(
    r"(?:Aktenzeichen|Gesch(?:ä|ae)fts(?:zeichen|nummer|nr\.?)|Az\.?)\s*[:\.]?\s*"
    r"(\d{1,3}\s?[A-Z]{1,2}\s?\d{1,5}/\d{2,4}(?:\s+[A-Za-z]\b)?|\d{1,5}/\d{2,4})",
    re.IGNORECASE,
)

FILENAME_CASE_ID_PATTERNS = [
    re.compile(r"\b(ADV-\d{3,4}-[A-Z]{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(AZ[-\s]?\d{2,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(CASE[-\s]?\d{3,4}[-\s]?\w{1,3})\b", re.IGNORECASE),
    # German Court ID: flexible spaces
    re.compile(r"\b(\d{1,3}\s?[A-Z]{1,2}\s?\d{1,5}/\d{2,4})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,3}-[A-Z]{1,2}-\d{1,5}/\d{2,4})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,5}/\d{2,4})\b"),
    re.compile(r"\b(REF-\d{3,4}-\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(\d{4}-CV-\d{4,6})\b", re.IGNORECASE),
]


class ExtractionResult(TypedDict):
    value: str | None
    confidence: str


class DateExtractionResult(TypedDict):
    value: datetime | None
    confidence: str


def extract_case_id(filename: str, content: str) -> ExtractionResult:
    """Extract case ID from filename and content."""
    value = None
    confidence = "low"

    # Anchor pass first: scan the full header region for Aktenzeichen/Geschäftszeichen.
    # Filename is excluded — anchor prose never appears there.
    anchor_match = ANCHOR_CASE_ID_PATTERN.search(content[:20000] if content else "")
    if anchor_match:
        return {
            "value": anchor_match.group(1).upper().replace(" ", "-"),
            "confidence": "high",
        }

    # Generic pass: keep the 5k window to avoid bare \d{1,5}/\d{2,4} false positives.
    text = content[:5000] if content else ""
    search_text = f"{filename} {text}"

    for pattern in CASE_ID_PATTERNS:
        match = pattern.search(search_text)
        if match:
            value = match.group(1).upper().replace(" ", "-")
            confidence = "high"
            break

    if not value:
        for pattern in FILENAME_CASE_ID_PATTERNS:
            match = pattern.search(filename)
            if match:
                value = match.group(1).upper().replace(" ", "-")
                confidence = "medium"
                break

    return {"value": value, "confidence": confidence}


def _parse_date_string(date_str: str) -> datetime | None:
    """Parse various date formats."""
    import re

    date_str = date_str.strip()

    patterns = [
        (r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", "%d.%m.%Y"),
        (r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", "%d.%m.%y"),
        (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"),
        (r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", None),
    ]

    for pattern, fmt in patterns:
        match = re.search(pattern, date_str)
        if match:
            groups = match.groups()
            if fmt:
                try:
                    return datetime.strptime(date_str[:10], fmt)
                except ValueError:
                    pass
            else:
                try:
                    day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
                    if year < 100:
                        year += 2000
                    return datetime(year, month, day)
                except ValueError:
                    pass

    return None


def extract_issued_date(content: str, filename: str) -> DateExtractionResult:
    """Extract the date on the document itself (Datum:, Date: header, Bescheiddatum)."""
    value = None
    confidence = "low"

    text = content[:5000] if content else ""

    # Specific patterns — highest priority first; broad first-date scan is last resort.
    specific_patterns = [
        r"(?:eingegangen|eingereicht|erhalten|dated|received|received on)[\s:]*(\d{1,2}\.\d{1,2}\.\d{2,4})",
        r"Datum[\s:]*(\d{1,2}\.\d{1,2}\.\d{2,4})",
        r"(?:vom|from)[\s]*(\d{1,2}\.\d{1,2}\.\d{2,4})",
    ]

    for pattern in specific_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = _parse_date_string(match.group(1))
            if parsed:
                if parsed.year < 100:
                    parsed = parsed.replace(year=parsed.year + 2000)
                value = parsed.replace(tzinfo=UTC)
                confidence = "medium"
                break

    # City+date closing line in document tail (e.g. "Ingolstadt, 07.08.2025").
    # Searched before the broad head-scan so Ladungsschreiben return the letter
    # date (footer) instead of the hearing date (top table).
    if not value:
        tail = (content or "")[-2000:]
        city_match = re.search(
            r"[A-ZÄÖÜ][a-zäöü]{2,},\s*(?:den\s+)?(\d{1,2}\.\d{1,2}\.\d{2,4})",
            tail,
        )
        if city_match:
            parsed = _parse_date_string(city_match.group(1))
            if parsed:
                if parsed.year < 100:
                    parsed = parsed.replace(year=parsed.year + 2000)
                value = parsed.replace(tzinfo=UTC)
                confidence = "medium"

    # Broad first-date fallback — grabs first date in document head, last resort.
    if not value:
        match = re.search(r"(\d{1,2}\.\d{1,2}\.\d{2,4})", text, re.IGNORECASE)
        if match:
            parsed = _parse_date_string(match.group(1))
            if parsed:
                if parsed.year < 100:
                    parsed = parsed.replace(year=parsed.year + 2000)
                value = parsed.replace(tzinfo=UTC)
                confidence = "medium"

    if not value:
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", filename)
        if match:
            try:
                value = datetime(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                    tzinfo=UTC,
                )
                confidence = "low"
            except ValueError:
                pass

    return {"value": value, "confidence": confidence}


_LETTERHEAD_RE = re.compile(
    r"^.{0,120}?\b("
    r"Amtsgericht|Landgericht|Oberlandesgericht|Bundesgerichtshof|"
    r"Verwaltungsgericht|Finanzgericht|Sozialgericht|Arbeitsgericht|"
    r"Rechtsanw(?:alt|ältin|älte)|Kanzlei|Notar(?:iat)?"
    r")\b.*$",
    re.MULTILINE | re.IGNORECASE,
)


_COURT_LEVEL_PREFIXES = (
    ("oberlandesgericht", ProceedingCourtLevel.OLG),
    ("bundesgerichtshof", ProceedingCourtLevel.BGH),
    ("landgericht", ProceedingCourtLevel.LG),
    ("amtsgericht", ProceedingCourtLevel.AG),
)


def infer_court_level(court_name: str | None) -> ProceedingCourtLevel | None:
    """Infer court level from a German court name string via prefix matching."""
    if not court_name:
        return None
    name = court_name.strip().lower()
    for prefix, level in _COURT_LEVEL_PREFIXES:
        if name.startswith(prefix) or f" {prefix}" in name:
            return level
    return None


def extract_sender(content: str) -> ExtractionResult:
    """Extract sender from email content or document letterhead."""
    value = None
    confidence = "low"

    text = content[:3000] if content else ""
    import re

    # Email patterns — highest confidence
    patterns = [
        r"(?:from|absender|sender)[\s:]*([^\n\r<>]+@[^\n\r<>]+)",
        r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        r"<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1).strip().lower()
            confidence = "high"
            break

    # Letterhead fallback for paper documents (no email address present)
    if not value:
        lh_match = _LETTERHEAD_RE.search(content[:1500] if content else "")
        if lh_match:
            value = lh_match.group(0).strip()[:120]
            confidence = "medium"

    if not value:
        match = re.search(r"von:?\s*([^\n\r]+)", text, re.IGNORECASE)
        if match:
            sender = match.group(1).strip()
            if "@" not in sender:
                value = sender
                confidence = "low"

    return {"value": value, "confidence": confidence}


def _parse_candidate_date(raw_value: str) -> datetime | None:
    """Parse date from cost candidate."""
    return _parse_date_string(raw_value)


# Compiled once — used for subject-line case matching at email ingest time.
_SUBJECT_AZ_COURT_RE = re.compile(
    r"\b(\d{1,3}\s?[A-Z]{1,2}\s?\d{1,5}/\d{2,4})\b", re.IGNORECASE
)
# Internal IDs are purely numeric: digits / digits (no embedded letters).
# Anchored to the very start of the subject so we don't pick up year-like tokens.
_SUBJECT_INTERNAL_ID_RE = re.compile(r"^\s*(\d{1,6}/\d{2,4})\b")


def extract_internal_id_from_subject(subject: str) -> str | None:
    """Return the lawyer internal reference (e.g. '8372/25') from an email subject.

    Matches a purely numeric NNN/YY token at the start of the subject — the
    conventional German law-firm file number format.  Returns None when no
    leading token is found.
    """
    m = _SUBJECT_INTERNAL_ID_RE.match(subject)
    if not m:
        return None
    return m.group(1).replace("/", "-")


# Strict canonical AZ pattern: "N+ L{1-3} N+/N+ [L{1-3}]?"
_AZ_CANONICAL_RE = re.compile(r"^\d+\s[A-Z]{1,3}\s\d+/\d+(?:\s[A-Z]{1,3})?$")


def normalize_az_court(value: str | None) -> str | None:
    """Canonicalise a court Aktenzeichen for equality comparison.

    Strips parenthetical annotations, converts dashes around digit/letter
    boundaries to spaces, inserts missing space between digit and letter,
    strips leading zeros from the initial numeric segment, normalises
    whitespace and slash, uppercases, then validates against the canonical
    AZ format. Returns None for garbage inputs (concatenated AZs, law firm
    names, schema fragments, etc.).

    "003 F 951/25"  → "3 F 951/25"
    "003F 951/25"   → "3 F 951/25"
    "22-T-342/26"   → "22 T 342/26"
    "26 UF 288/ 26 E" → "26 UF 288/26 E"
    "26 UF 288/26e" → "26 UF 288/26 E"
    "26 UF 288/26 E (ELTERL. SORGE)" → "26 UF 288/26 E"
    "26 UF 288/26 E 003 F 951/25 AG INGOLSTADT" → None  (concatenated)
    "Funk, Haidl & Partner" → None  (not an AZ)
    "8372/25" → None  (internal ID, not an AZ)
    """
    if not value:
        return None
    # Strip parenthetical annotations
    cleaned = re.sub(r"\s*\([^)]*\)\s*", "", value)
    # Convert dashes around digit/letter boundaries to spaces
    cleaned = re.sub(r"(\d)\s*-\s*([A-Za-z])", r"\1 \2", cleaned)
    cleaned = re.sub(r"([A-Za-z])\s*-\s*(\d)", r"\1 \2", cleaned)
    # Insert space at digit→letter boundary (e.g. "003F" → "003 F")
    cleaned = re.sub(r"(\d)([A-Za-z])", r"\1 \2", cleaned)
    # Insert space at letter→digit boundary (e.g. "UF288" → "UF 288")
    cleaned = re.sub(r"([A-Za-z])(\d)", r"\1 \2", cleaned)
    # Collapse whitespace and normalise slash
    cleaned = re.sub(r"\s+", " ", cleaned.strip())
    cleaned = re.sub(r"\s*/\s*", "/", cleaned)
    # Re-check for digit→letter boundary after slash normalization (e.g. "26/e" -> "26/E" -> "26 E")
    # Actually "288/26E" -> "288/26 E" is already handled by the (\d)([A-Za-z]) rule.
    result = cleaned.upper()
    # Strip leading zeros from the initial numeric segment only
    result = re.sub(r"^0+(\d)", r"\1", result)
    # Validate — reject anything that doesn't look like a real AZ
    if not _AZ_CANONICAL_RE.match(result):
        return None
    return result


def infer_case_type_from_az(az: str) -> CaseType | None:
    """Infer CaseType from a canonical court Aktenzeichen.

    Requires canonical form as produced by normalize_az_court() — uppercased,
    leading-zero-stripped, space-normalised.  Returns None when no mapping exists
    (unknown letter codes), preventing false positives.

    Family rule: any letter segment containing "F" maps to FAMILY.
    This covers F, UF, WF, SF, VF, EF and OLG variants conservatively.

    Examples:
        "3 F 426/25"    → FAMILY
        "26 UF 288/26"  → FAMILY
        "12 O 345/25"   → CIVIL
        "22 T 342/26"   → None  (unknown)
    """
    m = re.match(r"^\d+\s([A-Z]{1,3})\s\d+/\d+", az)
    if not m:
        return None
    code = m.group(1)  # already uppercase from normalize_az_court

    if "F" in code:
        return CaseType.FAMILY
    if code == "VG":
        return CaseType.ADMINISTRATIVE
    if code in ("CS", "KLS", "DS"):  # Cs/KLs/Ds after normalize_az_court .upper()
        return CaseType.CRIMINAL
    if code in ("O", "U"):
        return CaseType.CIVIL
    return None


def extract_az_court_from_subject(subject: str) -> str | None:
    """Return the court Aktenzeichen (e.g. '003 F 426/25') from an email subject.

    Searches anywhere in the subject — court AZ often appears after a dash or
    later in the subject line.  Returns None when no AZ is found.
    """
    m = _SUBJECT_AZ_COURT_RE.search(subject)
    return normalize_az_court(m.group(1)) if m else None


# "Unser Zeichen" / "Unser Az." is the standard German heading for a law firm's own
# internal file number. "Geschäftsnummer" is also used by some courts for their
# internal tracking number (distinct from Aktenzeichen), so we include it.
_INTERNAL_ID_ANCHOR_RE = re.compile(
    r"(?:Unser\s+(?:Zeichen|Az\.?)|Gesch(?:ä|ae)ftsnummer)\s*[:\.]?\s*"
    r"(\d{1,6}/\d{2,4})",
    re.IGNORECASE,
)


def extract_internal_id(content: str) -> ExtractionResult:
    """Extract the lawyer's internal file reference (e.g. '8124/25') from body text.

    Uses "Unser Zeichen" / "Unser Az." anchors — the standard German heading for a
    law firm's own reference number. Returns ExtractionResult for shape parity with
    the other extractors.
    """
    text = content[:20000] if content else ""
    match = _INTERNAL_ID_ANCHOR_RE.search(text)
    if match:
        return {"value": match.group(1).replace("/", "-"), "confidence": "high"}
    return {"value": None, "confidence": "low"}
