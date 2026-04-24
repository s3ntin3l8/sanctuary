import pytest

from app.models.enums import OriginatorType
from app.services.ingestion import (
    extract_case_id,
    extract_cost_candidates,
    extract_internal_id,
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


# --- 2a: letterhead sender tests ---


@pytest.mark.unit
def test_extract_sender_court_letterhead():
    content = "Oberlandesgericht München\nPalais der Justiz\nPrielmayerstraße 5"
    result = extract_sender(content)
    assert result["value"] is not None
    assert "Oberlandesgericht" in result["value"]
    assert result["confidence"] == "medium"


@pytest.mark.unit
def test_extract_sender_lawfirm_letterhead():
    content = "Kanzlei Funk & Partner\nRechtsanwälte und Notare\nMusterstraße 1"
    result = extract_sender(content)
    assert result["value"] is not None
    assert "Kanzlei" in result["value"]
    assert result["confidence"] == "medium"


@pytest.mark.unit
def test_extract_sender_email_wins_over_letterhead():
    content = "From: counsel@lawfirm.de\nAmtsgericht Hamburg\nAktenzeichen: 001 F 55/25"
    result = extract_sender(content)
    assert result["value"] == "counsel@lawfirm.de"
    assert result["confidence"] == "high"


# --- 2b: anchor case_id tests ---


@pytest.mark.unit
def test_extract_case_id_anchor_beats_generic():
    filler = "a" * 6000
    content = filler + "\nAktenzeichen: 26 UF 288/26 e\n"
    result = extract_case_id("doc.pdf", content)
    assert result["value"] == "26-UF-288/26-E"
    assert result["confidence"] == "high"


@pytest.mark.unit
def test_extract_case_id_anchor_geschaeftszeichen():
    content = "Geschäftszeichen: 003 F 426/25\nSehr geehrte Damen und Herren,"
    result = extract_case_id("doc.pdf", content)
    assert result["value"] == "003-F-426/25"
    assert result["confidence"] == "high"


# --- 2c: extract_internal_id tests ---


@pytest.mark.unit
def test_extract_internal_id_unser_zeichen():
    content = "Unser Zeichen: 8124/25\nIhr Zeichen: XYZ"
    result = extract_internal_id(content)
    assert result["value"] == "8124/25"
    assert result["confidence"] == "high"


@pytest.mark.unit
def test_extract_internal_id_unser_az():
    content = "Unser Az.: 8124/25\nBetreff: Klage"
    result = extract_internal_id(content)
    assert result["value"] == "8124/25"
    assert result["confidence"] == "high"


@pytest.mark.unit
def test_extract_internal_id_none_when_absent():
    content = "Aktenzeichen: 26 UF 288/26 e\nDatum: 12.03.2025"
    result = extract_internal_id(content)
    assert result["value"] is None
