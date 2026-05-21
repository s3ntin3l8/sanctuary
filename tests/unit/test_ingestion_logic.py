import pytest

from app.models.enums import OriginatorType
from app.services.ingestion import (
    extract_case_id,
    extract_cost_candidates,
    extract_internal_id,
    extract_originator,
    extract_sender,
    normalize_az_court,
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


@pytest.mark.unit
def test_cost_candidate_schema_roundtrips_both_shapes():
    """CostCandidateSchema must accept both amount (float) and rvg_position (str) shapes."""
    from app.models.schemas import CostCandidateSchema

    amount = {"type": "amount", "value": 583.40, "context": "EUR 583,40"}
    rvg = {"type": "rvg_position", "value": "§ 286 ZPO", "context": "Beweislast"}

    dumped_amount = CostCandidateSchema(**amount).model_dump()
    assert dumped_amount["value"] == 583.40

    dumped_rvg = CostCandidateSchema(**rvg).model_dump()
    assert dumped_rvg["value"] == "§ 286 ZPO"


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
    assert result["value"] == "8124-25"
    assert result["confidence"] == "high"


@pytest.mark.unit
def test_extract_internal_id_unser_az():
    content = "Unser Az.: 8124/25\nBetreff: Klage"
    result = extract_internal_id(content)
    assert result["value"] == "8124-25"
    assert result["confidence"] == "high"


@pytest.mark.unit
def test_extract_internal_id_none_when_absent():
    content = "Aktenzeichen: 26 UF 288/26 e\nDatum: 12.03.2025"
    result = extract_internal_id(content)
    assert result["value"] is None


@pytest.mark.unit
def test_normalize_az_court_canonicalizes():
    # Dashes collapse to spaces
    assert normalize_az_court("22-T-342/26") == "22 T 342/26"
    # Leading zeros stripped from initial numeric segment
    assert normalize_az_court("003-F-426/25") == "3 F 426/25"
    assert normalize_az_court("003 F 951/25") == "3 F 951/25"
    assert normalize_az_court("3 F 951/25") == "3 F 951/25"
    # Missing space between digit and letter code repaired
    assert normalize_az_court("003F 951/25") == "3 F 951/25"
    # Already canonical — idempotent
    assert normalize_az_court("22 T 342/26") == "22 T 342/26"
    # Parenthetical annotations stripped, spacing around / fixed
    assert normalize_az_court("26 UF 288/ 26 E (ELTERL. SORGE)") == "26 UF 288/26 E"
    # Lowercase uppercased; optional single-letter suffix preserved
    assert normalize_az_court("26 uf 288/26 e") == "26 UF 288/26 E"
    # None / empty stays None
    assert normalize_az_court(None) is None
    assert normalize_az_court("") is None
    # Garbage strings rejected — return None
    assert normalize_az_court("26 UF 288/26 E 003 F 951/25 AG INGOLSTADT") is None
    assert normalize_az_court("003 F 1824/25003 F 951/25") is None
    assert normalize_az_court("Funk, Haidl & Partner") is None
    assert normalize_az_court("high") is None
    assert normalize_az_court("...") is None
    assert normalize_az_court("8372/25") is None  # internal ID, not a valid AZ


@pytest.mark.unit
def test_infer_case_type_from_az():
    from app.models.enums import CaseType
    from app.services.ingestion.extractors import infer_case_type_from_az

    # Family: any letter segment containing "F"
    assert infer_case_type_from_az("3 F 426/25") == CaseType.FAMILY
    assert infer_case_type_from_az("26 UF 288/26") == CaseType.FAMILY
    assert infer_case_type_from_az("26 UF 288/26 E") == CaseType.FAMILY  # with suffix
    assert infer_case_type_from_az("5 WF 100/24") == CaseType.FAMILY
    assert infer_case_type_from_az("2 SF 50/25") == CaseType.FAMILY

    # Civil
    assert infer_case_type_from_az("12 O 345/25") == CaseType.CIVIL
    assert infer_case_type_from_az("8 U 200/24") == CaseType.CIVIL

    # Administrative
    assert infer_case_type_from_az("3 VG 100/25") == CaseType.ADMINISTRATIVE

    # Criminal (post-uppercase)
    assert infer_case_type_from_az("5 CS 77/25") == CaseType.CRIMINAL
    assert infer_case_type_from_az("2 KLS 10/24") == CaseType.CRIMINAL

    # Unknown codes — no false positives
    assert infer_case_type_from_az("22 T 342/26") is None
    assert infer_case_type_from_az("4 S 100/25") is None

    # Non-canonical input → None (function requires normalize_az_court output)
    assert infer_case_type_from_az("003F426/25") is None
    assert infer_case_type_from_az("") is None


@pytest.mark.unit
def test_extract_originator_court_score_uses_header_only():
    from app.services.ingestion.extractors import extract_originator

    # Body text mentioning courts should NOT push a lawyer letter to COURT
    lawyer_body = "Sehr geehrte Frau Müller,\n\n" * 20 + (
        "Das Amtsgericht hat per Beschluss entschieden. "
        "Das Oberlandesgericht hat bestätigt. "
        "Az.: 3 F 951/25 wurde verhandelt. " * 10
    )
    result = extract_originator("brief.pdf", lawyer_body)
    assert result["value"] != "court", (
        "Court keywords in body text should not classify a lawyer letter as court"
    )

    # Court letterhead in first 500 chars SHOULD classify as court
    court_header = "Amtsgericht Ingolstadt\nRichter Dr. Meier\n\nBeschluss\n\n"
    result2 = extract_originator("beschluss.pdf", court_header)
    assert result2["value"] == "court"
