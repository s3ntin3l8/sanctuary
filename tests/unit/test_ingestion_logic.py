import pytest

from app.models.enums import OriginatorType
from app.services.ingestion import (
    extract_case_id,
    extract_cost_candidates,
    extract_originator,
    extract_sender,
)


@pytest.mark.unit
def test_extract_case_id():
    result = extract_case_id("file.pdf", "Ref: ADV-123-K content")
    assert result["value"] == "ADV-123-K"
    assert result["confidence"] == "high"

    result = extract_case_id("AZ-2024-XY.pdf", "No numbers here")
    assert result["value"] == "AZ-2024-XY"

    # German Court ID
    result = extract_case_id("doc.pdf", "Aktenzeichen: 003 F 426/25")
    assert result["value"] == "003-F-426/25"

    # Lawyer ID
    result = extract_case_id("doc.pdf", "Unser Zeichen: 8124/25")
    assert result["value"] == "8124/25"

    result = extract_case_id("random.pdf", "No numbers here")
    assert result["value"] is None


@pytest.mark.unit
def test_extract_originator():
    result = extract_originator("", "This is a court order from the judge.")
    assert result["value"] == OriginatorType.COURT

    result = extract_originator("file.txt", "Hello world")
    assert result["value"] == OriginatorType.UNKNOWN


@pytest.mark.unit
def test_extract_sender():
    content = "From: John Doe <john@example.com>\nTo: Jane Smith\nSubject: Hello"
    result = extract_sender(content)
    assert result["value"] == "john@example.com"


@pytest.mark.unit
def test_extract_cost_candidates():
    content = "Amount: 500 EUR"
    candidates = extract_cost_candidates(content)
    assert isinstance(candidates, list)
