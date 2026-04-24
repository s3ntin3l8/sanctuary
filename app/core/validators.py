import re
from datetime import datetime
from pathlib import Path

from app.models.enums import CaseStatus, Jurisdiction

CASE_ID_PATTERN = re.compile(r"^[A-Z0-9]{1,10}-\d{1,6}(-[A-Z0-9]+)?$")


def normalize_case_id(case_id: str | None) -> str | None:
    """Sanitize case_id: replace / with -, strip, uppercase."""
    if not case_id:
        return case_id
    return case_id.replace("/", "-").strip().upper()


def validate_case_id(case_id: str | None) -> str | None:
    """Validate case ID format. Returns normalized ID or None if invalid."""
    if not case_id:
        return None
    case_id = normalize_case_id(case_id)
    if case_id == "_TRIAGE":
        return "_TRIAGE"
    if CASE_ID_PATTERN.match(case_id):
        return case_id
    return None


def validate_case_id_required(case_id: str) -> str:
    """Validate case ID is required and valid. Raises ValueError if invalid."""
    validated = validate_case_id(case_id)
    if not validated or validated == "_TRIAGE":
        raise ValueError(f"Invalid case ID: {case_id}")
    return validated


def validate_case_status(status: str | None) -> CaseStatus | None:
    """Validate case status string. Returns CaseStatus enum or None."""
    if not status:
        return None
    try:
        return CaseStatus(status.lower())
    except ValueError:
        return None


def validate_jurisdiction(jurisdiction: str | None) -> Jurisdiction | None:
    """Validate jurisdiction string. Returns Jurisdiction enum or None."""
    if not jurisdiction:
        return None
    try:
        return Jurisdiction(jurisdiction.lower())
    except ValueError:
        return None


def validate_date(dt: datetime | str | None) -> datetime | None:
    """Parse and validate date from string or datetime."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, str):
        try:
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def validate_positive_float(value: float | str | None) -> float | None:
    """Validate positive float amount. Returns float or None."""
    if value is None:
        return None
    try:
        val = float(value)
        if val >= 0:
            return val
    except (ValueError, TypeError):
        pass
    return None


def validate_file_extension(filename: str, allowed: list[str] | None = None) -> bool:
    """Check if file extension is allowed."""
    if not filename:
        return False
    ext = Path(filename).suffix.lower()
    allowed = allowed or [".pdf", ".docx", ".txt", ".md"]
    return ext in allowed


def validate_file_size(size_bytes: int, max_mb: float = 50.0) -> bool:
    """Check if file size is within limit."""
    return 0 < size_bytes <= int(max_mb * 1024 * 1024)


def validate_required_string(value: str | None, field_name: str) -> str:
    """Validate required string field. Raises ValueError if empty."""
    if not value or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def validate_optional_string(value: str | None) -> str | None:
    """Validate optional string, returns None if empty."""
    if not value or not value.strip():
        return None
    return value.strip()


def validate_title(title: str | None) -> str | None:
    """Validate document/title field."""
    return validate_optional_string(title)


def validate_email(email: str | None) -> str | None:
    """Basic email validation. Returns cleaned email or None."""
    if not email or "@" not in email:
        return None
    email = email.strip().lower()
    if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return email
    return None
