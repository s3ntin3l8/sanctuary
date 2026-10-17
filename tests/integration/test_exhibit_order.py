"""A lawyer's Anlage list gives the enclosures their number and order."""

import email.message
from unittest.mock import patch

import pytest

from app.models.database import Document
from app.models.enums import DocumentRole, IngestBatchSourceType
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.triage_subgroups import ensure_sub_groups_initialized

pytestmark = pytest.mark.integration

BODY = (
    "Sehr geehrte Damen und Herren,\n"
    "in dem Verfahren überreichen wir:\n"
    "Anlagen:\n"
    "Anlage K1: Mietvertrag vom 01.01.2024\n"
    "Anlage K2: Kuendigung zum 30.06.2025\n"
    "Anlage K3: Zahlungsaufstellung\n"
    "Mit freundlichen Grüßen\n"
)


def _mail(attachments, body=BODY):
    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = "Schriftsatz"
    msg["Message-ID"] = "<exhibits@example.com>"
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(
            data,
            maintype="text",
            subtype="plain",
            filename=name,
            disposition="attachment",
        )
    return msg.as_bytes()


def _ingest(db, raw):
    with patch("app.services.ingestion.batch_orchestrator.dispatch_task"):
        return ingest_raw_email(db, raw, source_type=IngestBatchSourceType.EMAIL)


def _exhibits(db, batch):
    return {
        d.original_filename: (d.meta or {}).get("exhibit")
        for d in db.query(Document).filter(Document.ingest_batch_id == batch.id)
    }


def test_listed_exhibits_are_matched_by_label_and_by_description(db_session):
    batch = _ingest(
        db_session,
        _mail(
            [
                ("Zahlungsaufstellung final.txt", b"Aufstellung der Zahlungen 2024"),
                ("Kuendigung 2025 Kopie.txt", b"Kuendigung des Vertrags"),
                ("Anlage_K1_Vertrag.txt", b"Mietvertrag Text"),
                ("scan_0042.txt", b"unrelated scan"),
            ]
        ),
    )
    found = _exhibits(db_session, batch)
    assert found["Anlage_K1_Vertrag.txt"] == {
        "label": "K 1",
        "position": 1,
        "source": "anlage_list",
    }
    assert found["Kuendigung 2025 Kopie.txt"]["label"] == "K 2"
    assert found["Zahlungsaufstellung final.txt"]["label"] == "K 3"
    assert found["scan_0042.txt"] is None  # no match: no exhibit number invented


def test_exhibits_survive_extraction(db_session):
    from app.services.ingestion.service import process_uploaded_document

    batch = _ingest(
        db_session, _mail([("Anlage_K1_Vertrag.txt", b"Mietvertrag Text " * 5)])
    )
    doc = (
        db_session.query(Document)
        .filter_by(ingest_batch_id=batch.id, original_filename="Anlage_K1_Vertrag.txt")
        .one()
    )
    process_uploaded_document(doc, db_session)
    assert doc.meta["exhibit"]["label"] == "K 1"


def test_enclosures_are_ordered_as_the_sender_listed_them(db_session):
    batch = _ingest(
        db_session,
        _mail(
            [
                ("Anlage_K3_c.txt", b"drei"),
                ("Anlage_K1_a.txt", b"eins"),
                ("Anlage_K2_b.txt", b"zwei"),
            ]
        ),
    )
    docs = {
        d.original_filename: d
        for d in db_session.query(Document).filter_by(ingest_batch_id=batch.id)
    }
    cover = Document(
        title="Begleitschreiben",
        content="Begleitschreiben",
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        role=DocumentRole.COVER_LETTER,
    )
    db_session.add(cover)
    db_session.flush()
    for d in docs.values():
        d.parent_id = cover.id
        d.role = DocumentRole.ENCLOSURE
    db_session.commit()

    ensure_sub_groups_initialized(db_session, batch.id)
    db_session.commit()

    ordered = sorted((d for d in docs.values()), key=lambda d: d.sub_group_sort_order)
    assert [d.original_filename for d in ordered] == [
        "Anlage_K1_a.txt",
        "Anlage_K2_b.txt",
        "Anlage_K3_c.txt",
    ]
