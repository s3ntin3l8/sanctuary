from datetime import date

import pytest

from app.services.ingestion import (
    OriginatorType,
    extract_case_id,
    extract_cost_candidates,
    extract_originator,
    extract_schedule_candidates,
    extract_sender,
)


@pytest.mark.unit
def test_extract_case_id():
    # Test ADV pattern in content (High confidence)
    cid, conf = extract_case_id("file.pdf", "Ref: ADV-123-K content")
    assert cid == "ADV-123-K"
    assert conf == "high"

    # Test German court AZ pattern in filename (Medium confidence)
    cid, conf = extract_case_id("AZ-2024-XY.pdf", "No numbers here")
    assert cid == "AZ-2024-XY"
    assert conf == "medium"

    # Test no match
    cid, conf = extract_case_id("random.pdf", "No numbers here")
    assert cid is None


@pytest.mark.unit
def test_extract_originator():
    # Test Court keywords
    otype, conf = extract_originator("", "This is a court order from the judge.")
    assert otype == OriginatorType.COURT

    # Test Opposing keywords
    otype, conf = extract_originator(
        "", "Letter from opposing counsel regarding the motion."
    )
    assert otype == OriginatorType.OPPOSING

    # Test Unknown
    otype, conf = extract_originator("file.txt", "Hello world")
    assert otype == OriginatorType.UNKNOWN


@pytest.mark.unit
def test_extract_sender():
    content = "From: John Doe <john@example.com>\nTo: Jane Smith\nSubject: Hello"
    sender, conf = extract_sender(content)
    assert sender == "John Doe <john@example.com>"

    # Test German style
    content = "Absender: Rechtsanwalt Miller\nDatum: 01.01.2024"
    sender, conf = extract_sender(content)
    assert "Miller" in sender


@pytest.mark.unit
def test_extract_schedule_candidates():
    # Test German date format
    content = "The hearing is scheduled for 15.05.2024 at 10:00."
    candidates = extract_schedule_candidates(content)
    assert len(candidates) > 0
    # Note: candidates have 'due_at' for deadlines and 'scheduled_for' for hearings
    assert any(
        (c.get("due_at") and c["due_at"].date() == date(2024, 5, 15))
        or (c.get("scheduled_for") and c["scheduled_for"].date() == date(2024, 5, 15))
        for c in candidates
    )


@pytest.mark.unit
def test_extract_cost_candidates():
    content = "The total amount due is EUR 1.234,56 including VAT."
    candidates = extract_cost_candidates(content)
    assert len(candidates) > 0
    # The regex extracts the string value
    assert any("1.234,56" in c["value"] for c in candidates)
