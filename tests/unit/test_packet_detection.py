"""A PDF that holds several letters is noticed from its page text, without the AI."""

from types import SimpleNamespace

import pytest

from app.services.ingestion import service
from app.services.ingestion.slicer import likely_document_starts

pytestmark = pytest.mark.unit

COURT_COVER = (
    "**Amtsgericht Ingolstadt**\nAbteilung für Familiensachen\n\n"
    "Herrn Björn Hansen\nAz. 003 F 1221/26\n\nSehr geehrter Herr Hansen,\n\n"
    "anbei wird Ihnen das Gesuch um Verfahrenskostenhilfe übersandt."
)
COURT_COVER_END = "Datenschutzhinweis siehe Webseite.\n\nMit freundlichen Grüßen\nAuf Anordnung\nBoiger"
FILING_HEAD = (
    "An: Amtsgericht Ingolstadt\nAz.: 003 F 1221/26\n\n"
    "Antrag auf Verfahrenskostenhilfe\n\nSehr geehrte Frau Richter,\n\nich beantrage..."
)


def test_a_filing_behind_a_court_cover_is_found():
    pages = {
        1: COURT_COVER,
        2: "Seite 2\n\n" + COURT_COVER_END,
        3: FILING_HEAD,
        4: "Fortsetzung der Begründung zum Antrag.\n\nDie Antragsgegnerin trägt vor.",
    }

    assert likely_document_starts(pages) == [3]


def test_a_court_letterhead_on_a_later_page_starts_another_letter():
    pages = {
        1: "Antrag der Antragstellerin.\n\nDie Begründung folgt.",
        2: "Weitere Begründung ohne Briefkopf.",
        3: "Amtsgericht Ingolstadt\nAbteilung für Familiensachen\n\nBeschluss",
    }

    assert likely_document_starts(pages) == [3]


def test_numbering_that_restarts_marks_a_new_document():
    pages = {
        1: "Schriftsatz\n\nAnfang der Begründung.",
        2: "Weiter im Text.\n\nSeite 2 von 2",
        3: "Seite 1 von 3\n\nAnderes Schreiben, anderer Inhalt.",
    }

    assert likely_document_starts(pages) == [3]


def test_one_long_filing_is_not_taken_for_a_packet():
    # "Hiermit", a mention of an Anlage and a date turn up inside a single filing.
    pages = {
        1: "Antragsschrift\n\nDie Antragstellerin beantragt.\n\nSeite 1 von 40",
        2: "Hiermit wird vorgetragen, dass die Beteiligten sich getrennt haben.\n\nSeite 2 von 40",
        3: "Wie in Anlage K3 dargelegt, besteht Gefahr.\n\nSeite 3 von 40",
        4: "Am 13.07.2026 wurde das Verfahren eingestellt.\n\nSeite 4 von 40",
    }

    assert likely_document_starts(pages) == []


def test_a_missing_page_reading_is_skipped_not_guessed():
    pages = {1: COURT_COVER, 3: FILING_HEAD}  # page 2 failed OCR

    assert likely_document_starts(pages) == []


def _doc(meta, file_path="/data/x.pdf"):
    return SimpleNamespace(meta=meta, file_path=file_path)


def _chunks(texts):
    return [
        {"text": t, "meta": {"page": n, "failed": False}}
        for n, t in enumerate(texts, start=1)
    ]


PACKET = _chunks([COURT_COVER, COURT_COVER_END, FILING_HEAD])


def test_extraction_notes_where_the_letters_start():
    doc = _doc({"chunks": PACKET})

    service._flag_packet(doc)

    assert doc.meta["packet_starts"] == [3]


def test_a_document_somebody_already_cut_is_not_flagged_again():
    doc = _doc({"chunks": PACKET, "slice_range": [1, 3]})

    service._flag_packet(doc)

    assert "packet_starts" not in doc.meta


def test_only_a_pdf_read_page_by_page_can_be_flagged():
    assert "packet_starts" not in _flag(_doc({"chunks": PACKET}, "/data/x.docx"))
    assert "packet_starts" not in _flag(_doc({"extractor": "docling"}))


def test_a_new_extraction_replaces_an_old_flag():
    doc = _doc({"chunks": _chunks(["Ein Brief."]), "packet_starts": [3]})

    service._flag_packet(doc)

    assert "packet_starts" not in doc.meta


def _flag(doc):
    service._flag_packet(doc)
    return doc.meta
