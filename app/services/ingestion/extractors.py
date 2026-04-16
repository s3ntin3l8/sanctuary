import re
from datetime import UTC, datetime
from typing import TypedDict

from app.models.enums import OriginatorType

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

COURT_KEYWORDS = {
    "court order": 2,
    "court clerk": 3,
    "landgericht": 3,
    "oberlandesgericht": 3,
    "amtsgericht": 3,
    "gericht": 2,
    "richter": 2,
    "beschluss": 2,
    "urteil": 2,
    "klage": 2,
    "az.": 3,
    "aktenzeichen": 3,
    "gerichtsbeschluss": 3,
    "mahnbescheid": 3,
    "vollstreckungsbescheid": 3,
}

OPPOSING_KEYWORDS = {
    "anwalt": 1,
    "rechtsanwalt": 2,
    "kläger": 1,
    "klägerin": 1,
    "antragsteller": 1,
    "antragstellerin": 1,
    "beklagter": 1,
    "beklagte": 1,
    "gegner": 2,
    "gegnerin": 2,
    "widerspruch": 2,
    "einspruch": 2,
    "klageerwiderung": 2,
}

OWN_KEYWORDS = {
    "mandant": 2,
    "mandantin": 2,
    "unser": 1,
    "unsere": 1,
    "ihre": 1,
    "hiermit": 1,
    "vereinbarung": 1,
    "vollmacht": 2,
}


class ExtractionResult(TypedDict):
    value: str | None
    confidence: str


def extract_case_id(filename: str, content: str) -> ExtractionResult:
    """Extract case ID from filename and content."""
    value = None
    confidence = "low"

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


def extract_originator(filename: str, content: str) -> ExtractionResult:
    """Extract originator type from content."""
    value = OriginatorType.UNKNOWN
    confidence = "low"
    score = {"court": 0, "opposing": 0, "own": 0}

    text = content.lower()[:10000] if content else ""
    filename_lower = filename.lower()

    for keyword, weight in COURT_KEYWORDS.items():
        if keyword in text or keyword in filename_lower:
            score["court"] += weight

    for keyword, weight in OPPOSING_KEYWORDS.items():
        if keyword in text or keyword in filename_lower:
            score["opposing"] += weight

    for keyword, weight in OWN_KEYWORDS.items():
        if keyword in text:
            score["own"] += weight

    if score["court"] > score["opposing"] and score["court"] > score["own"]:
        value = OriginatorType.COURT
        confidence = "medium" if score["court"] >= 3 else "low"
    elif score["opposing"] > score["court"] and score["opposing"] > score["own"]:
        value = OriginatorType.OPPOSING
        confidence = "medium" if score["opposing"] >= 3 else "low"
    elif score["own"] > 2:
        value = OriginatorType.OWN
        confidence = "low"

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


def extract_received_date(content: str, filename: str) -> ExtractionResult:
    """Extract received date from content."""
    value = None
    confidence = "low"

    text = content[:5000] if content else ""

    date_patterns = [
        r"(?:eingegangen|eingereicht|erhalten|dated|received|received on)[\s:]*(\d{1,2}\.\d{1,2}\.\d{2,4})",
        r"(\d{1,2}\.\d{1,2}\.\d{2,4})",
        r"(?:vom|from)[\s]*(\d{1,2}\.\d{1,2}\.\d{2,4})",
    ]

    for pattern in date_patterns:
        import re

        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = _parse_date_string(match.group(1))
            if parsed:
                if parsed.year < 100:
                    parsed = parsed.replace(year=parsed.year + 2000)
                value = parsed.replace(tzinfo=UTC)
                confidence = "medium"
                break

    if not value:
        import re

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


def extract_sender(content: str) -> ExtractionResult:
    """Extract sender from email content."""
    value = None
    confidence = "low"

    text = content[:3000] if content else ""
    import re

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


def extract_schedule_candidates(content: str, base_date=None) -> list[dict]:
    """Extract deadlines and hearings from content."""
    import re

    candidates = []
    base_date or datetime.now(UTC)

    text = content[:15000] if content else ""

    deadline_patterns = [
        r"(?:frist bis|fällig am|fallsällig|deadline|due)[\s:]*(?:am|on)?[\s:]*(\d{1,2}\.\d{1,2}\.\d{2,4})",
        r"(\d{1,2}\.\d{1,2}\.\d{2,4})[\s]*(?:frist|fällig|deadline)",
    ]

    for pattern in deadline_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            parsed = _parse_candidate_date(match.group(1))
            if parsed:
                if parsed.year < 100:
                    parsed = parsed.replace(year=parsed.year + 2000)
                candidates.append(
                    {
                        "type": "deadline",
                        "date": parsed.isoformat(),
                        "context": text[max(0, match.start() - 30) : match.end() + 30],
                    }
                )

    hearing_patterns = [
        r"(?:verhandlung|hearing|termin)[\s:]*(?:am|on)?[\s:]*(?:den)?[\s:]*(\d{1,2}\.\d{1,2}\.\d{2,4})",
        r"(\d{1,2}\.\d{1,2}\.\d{2,4})[\s]*(?:verhandlung|termin)",
    ]

    for pattern in hearing_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            parsed = _parse_candidate_date(match.group(1))
            if parsed:
                if parsed.year < 100:
                    parsed = parsed.replace(year=parsed.year + 2000)
                candidates.append(
                    {
                        "type": "hearing",
                        "date": parsed.isoformat(),
                        "context": text[max(0, match.start() - 30) : match.end() + 30],
                    }
                )

    return candidates
