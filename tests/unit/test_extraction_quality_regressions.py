"""Regressions from a real Gmail-history ingest (docs 5, 64, 100 and the
fragmented 8441/25 case): fixtures are the strings the pipeline actually saw."""

from datetime import UTC, datetime

import pytest

from app.models.database import Case
from app.services.ai_summary import enrich_document_with_ai
from app.services.ingestion.extractors import (
    date_in_text,
    extract_issued_date,
    extract_sender,
    normalize_internal_id,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8441/25", "8441-25"),
        ("8441-25", "8441-25"),
        ("8441/25 L02 RS D4/2247-25", "8441-25"),  # firm's full Gz.
        ("8441-25 L02 RS D4-1811-26", "8441-25"),
        ("9418/26 L02 RS D4/2584-26", "9418-26"),
        ("ADV-024-A", "ADV-024-A"),  # an already-canonical Case.id survives
        ("8441-25-A", "8441-25-A"),  # ...including a numeric one with a suffix
        ("888520392705", None),  # a phone/ID number, not a file reference
        ("Aktenzeichen unbekannt", None),
        ("", None),
        (None, None),
        (8441, None),
    ],
)
def test_normalize_internal_id(raw, expected):
    assert normalize_internal_id(raw) == expected


@pytest.mark.unit
def test_date_anchor_tolerates_chandra_bold_labels():
    # doc 5: the label is bold and the date sits on the next line
    content = "**Datum**  \n23.09.2026\n\nIn der Familiensache"
    result = extract_issued_date(content, "x.pdf")
    assert result["value"] == datetime(2026, 9, 23, tzinfo=UTC)


@pytest.mark.unit
def test_birth_dates_are_never_the_letter_date():
    # doc 100: the parties block precedes the decision date
    content = (
        "Hansen Björn, geboren am 12.09.1986, Hauptstraße 23 b\n"
        "Liu Yingying, geboren am 06.09.1981, Schlossgasse 1 e\n\n"
        "Beschluss\n\nIngolstadt, 28.07.2026\n"
    )
    result = extract_issued_date(content, "x.pdf")
    assert result["value"] == datetime(2026, 7, 28, tzinfo=UTC)


@pytest.mark.unit
def test_sender_letterhead_loses_markdown_heading_markers():
    result = extract_sender("## Landgericht Ingolstadt\n\nAz. 22 T 342/26")
    assert result["value"] == "Landgericht Ingolstadt"
    result = extract_sender("> **Landgericht Ingolstadt** >\n\nAz. 22 T 342/26")
    assert result["value"] == "Landgericht Ingolstadt"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("day", "content", "expected"),
    [
        (datetime(2026, 9, 23), "Datum 23.09.2026", True),
        (datetime(2026, 9, 3), "vom 3.9.2026", True),
        (datetime(2026, 9, 23), "Ingolstadt, den 23. September 2026", True),
        (datetime(2026, 9, 23), "eingegangen 2026-09-23", True),
        (datetime(2025, 9, 23), "Datum 23.09.2026", False),
        (datetime(2026, 9, 23), "23. September\n2026", True),
        # a birth date in the parties block is not a letter date
        (datetime(1986, 9, 12), "Hansen, geboren am 12.09.1986", False),
    ],
)
def test_date_in_text(day, content, expected):
    assert date_in_text(day, content) is expected


def _enrich(db_session, doc, summary, *, content, issued):
    doc.content = content
    doc.issued_date = issued
    doc.case_id = None
    enrich_document_with_ai(doc, {"confidence": {}, **summary}, db_session)
    return doc


@pytest.mark.unit
def test_ai_date_that_is_not_in_the_text_does_not_replace_the_regex_date(
    db_session, sample_document
):
    # doc 5: regex read 23.09.2026; the model said 2025-09-23 with "high"
    read = datetime(2026, 9, 23, tzinfo=UTC)
    doc = _enrich(
        db_session,
        sample_document,
        {"issued_date": "2025-09-23"},
        content="Datum 23.09.2026",
        issued=read,
    )
    assert doc.issued_date == read


@pytest.mark.unit
def test_ai_date_found_in_the_text_replaces_a_wrong_regex_date(
    db_session, sample_document
):
    wrong = datetime(1986, 9, 12, tzinfo=UTC)  # a birth date picked up earlier
    doc = _enrich(
        db_session,
        sample_document,
        {"issued_date": "2026-07-28"},
        content="geboren am 12.09.1986 ... Ingolstadt, 28.07.2026",
        issued=wrong,
    )
    assert doc.issued_date == datetime(2026, 7, 28, tzinfo=UTC)


@pytest.mark.unit
def test_ai_date_is_accepted_when_nothing_was_read_from_the_text(
    db_session, sample_document
):
    doc = _enrich(
        db_session,
        sample_document,
        {"issued_date": "2026-07-28"},
        content="handwritten scan, no digits",
        issued=None,
    )
    assert doc.issued_date == datetime(2026, 7, 28, tzinfo=UTC)


@pytest.mark.unit
def test_firm_reference_from_the_model_resolves_to_one_draft_case(
    db_session, sample_document
):
    """The 8441/25 matter was split across three cases because the model
    returned the firm's whole Gz. as the internal_id."""
    for raw in ("8441/25 L02 RS D4/2247-25", "8441/25 L02 RS D4/1811-26", "8441/25"):
        sample_document.case_id = "_TRIAGE"
        enrich_document_with_ai(
            sample_document,
            {"confidence": {}, "internal_id": raw, "case_title": "Hansen ./. Liu"},
            db_session,
        )
        assert sample_document.case_id == "8441-25"
    db_session.flush()
    assert db_session.query(Case).filter(Case.id.like("8441-25%")).count() == 1


@pytest.mark.unit
def test_an_unrelated_number_never_creates_a_draft_case(db_session, sample_document):
    sample_document.case_id = "_TRIAGE"
    enrich_document_with_ai(
        sample_document,
        {"confidence": {}, "internal_id": "888520392705"},
        db_session,
    )
    assert sample_document.case_id == "_TRIAGE"
    assert db_session.get(Case, "888520392705") is None


@pytest.mark.unit
def test_extractors_import_on_their_own():
    """A module-level import of app.core.validators made this a circular import."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "import app.services.ingestion.extractors"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
