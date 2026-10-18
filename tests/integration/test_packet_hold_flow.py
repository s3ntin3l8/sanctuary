"""An email PDF that looks like several letters holds the email until the user decides."""

import email.message
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Document, DocumentPipelineStage, IngestBatch
from app.models.enums import IngestBatchSourceType
from app.services import auth_service
from app.services.ingestion import packet_hold
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.pipeline_status import clear_extraction_stamp
from app.services.triage_dismissal import dismiss_bundle
from app.services.user_settings_service import get_hold_packets
from app.tasks.document_processing import process_document_task

pytestmark = pytest.mark.integration

COVER = (
    "**Amtsgericht Ingolstadt**\nAbteilung für Familiensachen\n\n"
    "Herrn Björn Hansen\nAz. 003 F 1221/26\n\nSehr geehrter Herr Hansen,\n\n"
    "anbei wird Ihnen das Gesuch um Verfahrenskostenhilfe übersandt."
)
COVER_END = "Datenschutzhinweis siehe Webseite.\n\nMit freundlichen Grüßen\nAuf Anordnung\nBoiger"
FILING = (
    "An: Amtsgericht Ingolstadt\nAz.: 003 F 1221/26\n\n"
    "Antrag auf Verfahrenskostenhilfe\n\nSehr geehrte Frau Richter,\n\nich beantrage..."
)
SINGLE = [
    "Ein einzelner Brief.\n\nSeite 1 von 3",
    "Weiter im Text.\n\nSeite 2 von 3",
    "Ende.\n\nSeite 3 von 3",
]
client = TestClient(app)


def _pdf_bytes(tmp_path, pages=3) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 300)
    path = tmp_path / "packet.pdf"
    pdf.save(str(path))
    pdf.close()
    return path.read_bytes()


def _mail(pdf: bytes, message_id="<packet@example.com>") -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "gericht@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = "Gesuch"
    msg["Message-ID"] = message_id
    msg.set_content("Anbei.")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="Paket.pdf")
    msg.add_attachment(
        f"Beiliegender Text der zweiten Anlage {message_id}.\n".encode() * 3,
        maintype="text",
        subtype="plain",
        filename="Notiz.txt",
    )
    return msg.as_bytes()


def _extraction(texts):
    return {
        "content": "\n\n".join(
            f"--- PAGE {n} ---\n\n{t}" for n, t in enumerate(texts, 1)
        ),
        "metadata": {"pages": len(texts), "extractor": "chandra-ocr-2"},
        "chunks": [
            {"text": t, "meta": {"page": n, "failed": False}}
            for n, t in enumerate(texts, 1)
        ],
    }


def _convert(texts):
    def fake(file_path, engine=None):
        if str(file_path).endswith(".pdf"):
            return _extraction(texts)
        return {
            "content": "Beiliegender Text der zweiten Anlage, lang genug. " * 4,
            "metadata": {"pages": 1},
            "chunks": [],
        }

    return fake


@pytest.fixture
def pipeline(db_session):
    """Dispatch is recorded, never run: ``barrier`` is the OCR->chat fan-out of the
    task side, ``release`` the one a request handler makes."""
    with (
        patch("app.services.ingestion.batch_orchestrator.dispatch_task"),
        patch("app.tasks.document_processing.metadata_task.delay") as barrier,
        patch("app.tasks.dispatch.dispatch_task") as release,
    ):
        yield type("P", (), {"barrier": barrier, "release": release})


def _ingest(db, tmp_path, message_id="<packet@example.com>"):
    batch = ingest_raw_email(
        db,
        _mail(_pdf_bytes(tmp_path), message_id),
        source_type=IngestBatchSourceType.EMAIL,
    )
    docs = {
        d.original_filename: d
        for d in db.query(Document).filter(Document.ingest_batch_id == batch.id)
    }
    return batch, docs["Paket.pdf"], docs["Notiz.txt"]


def _extract(db, texts, *docs):
    with patch(
        "app.services.ingestion.service.convert_file", side_effect=_convert(texts)
    ):
        for doc in docs:
            process_document_task.run(doc.id)
    db.expire_all()


def _stage(db, doc, stage):
    return (
        db.query(DocumentPipelineStage)
        .filter_by(document_id=doc.id, stage=stage)
        .one()
        .status
    )


def test_a_packet_holds_the_whole_email(db_session, tmp_path, pipeline):
    batch, packet, note = _ingest(db_session, tmp_path)

    _extract(db_session, [COVER, COVER_END, FILING], packet, note)

    assert packet.held_for_split_at is not None
    assert packet.meta["packet_starts"] == [3]
    assert note.held_for_split_at is None
    assert _stage(db_session, note, "extract") == "completed"
    assert _stage(db_session, note, "metadata") == "pending"
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is None
    pipeline.barrier.assert_not_called()

    bundle = client.get("/api/v1/triage").json()["bundles"][0]
    assert bundle["pipeline"]["active_label"] == "Awaiting split"
    assert {d["id"]: d["held_for_split"] for d in bundle["documents"]} == {
        packet.id: True,
        note.id: False,
    }


def test_a_single_letter_is_not_held(db_session, tmp_path, pipeline):
    batch, packet, note = _ingest(db_session, tmp_path)

    _extract(db_session, SINGLE, packet, note)

    assert packet.held_for_split_at is None
    assert pipeline.barrier.call_count == 2  # the barrier fired for both documents


def test_keep_as_one_releases_the_email_and_is_remembered(
    db_session, tmp_path, pipeline
):
    batch, packet, note = _ingest(db_session, tmp_path)
    _extract(db_session, [COVER, COVER_END, FILING], packet, note)

    response = client.post(f"/api/v1/slicing/documents/{packet.id}/keep")
    assert response.status_code == 200 and response.json() == {"document_id": packet.id}
    db_session.expire_all()

    assert packet.held_for_split_at is None and packet.meta["packet_kept"] is True
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is not None
    assert pipeline.release.call_count == 2

    # A re-extraction decides again, but never holds a document the user kept.
    clear_extraction_stamp(packet)
    db_session.commit()
    _extract(db_session, [COVER, COVER_END, FILING], packet)
    assert packet.held_for_split_at is None
    assert packet.meta["packet_starts"] == [3]
    assert packet.meta["packet_kept"] is True


def test_a_split_replaces_the_packet_and_its_parts_are_not_held(
    db_session, tmp_path, pipeline
):
    batch, packet, note = _ingest(db_session, tmp_path)
    _extract(db_session, [COVER, COVER_END, FILING], packet, note)
    packet.meta = {**packet.meta, "slicing": {"status": "ready", "page_count": 3}}
    db_session.commit()
    packet_id = packet.id
    assert client.get(f"/api/v1/slicing/documents/{packet_id}").json()["held_for_split"]
    review = client.get(f"/api/v1/documents/{packet_id}/review").json()
    assert review["held_for_split"] is True and review["packet_starts"] == [3]

    response = client.post(
        f"/api/v1/slicing/documents/{packet_id}/confirm",
        json={"cuts": [{"page": 2, "kind": "letter"}]},
    )
    assert response.status_code == 200, response.text
    part_ids = response.json()["document_ids"]
    db_session.expire_all()
    assert db_session.get(Document, packet_id) is None
    assert not packet_hold.batch_is_held(db_session, batch.id)

    parts = [db_session.get(Document, i) for i in part_ids]
    _extract(db_session, [COVER, COVER_END, FILING], *parts)  # prefilled: no OCR

    assert all(p.held_for_split_at is None for p in parts)
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is not None
    assert pipeline.barrier.called


def test_two_held_attachments_need_two_decisions(db_session, tmp_path, pipeline):
    batch, packet, note = _ingest(db_session, tmp_path)
    _extract(db_session, [COVER, COVER_END, FILING], packet, note)
    note.held_for_split_at = packet.held_for_split_at
    db_session.commit()

    client.post(f"/api/v1/slicing/documents/{packet.id}/keep")
    db_session.expire_all()
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is None

    client.post(f"/api/v1/slicing/documents/{note.id}/keep")
    db_session.expire_all()
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is not None


def test_dismissing_the_held_document_lets_its_siblings_go_on(
    db_session, tmp_path, pipeline
):
    batch, packet, note = _ingest(db_session, tmp_path)
    _extract(db_session, [COVER, COVER_END, FILING], packet, note)

    assert dismiss_bundle(db_session, doc_id=packet.id) is True

    db_session.expire_all()
    assert packet.held_for_split_at is None
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is not None


def test_turning_the_switch_off_releases_everything_and_new_mail_is_only_hinted(
    db_session, tmp_path, pipeline
):
    batch, packet, note = _ingest(db_session, tmp_path)
    _extract(db_session, [COVER, COVER_END, FILING], packet, note)

    response = client.put("/api/v1/settings/ai/hold-packets", json={"enabled": False})
    assert response.status_code == 200
    db_session.expire_all()
    assert get_hold_packets(db_session) is False
    assert packet.held_for_split_at is None
    assert db_session.get(IngestBatch, batch.id).metadata_phase_queued_at is not None
    assert client.get("/api/v1/settings/ai").json()["hold_packets"] is False

    other, packet2, note2 = _ingest(db_session, tmp_path, "<other@example.com>")
    _extract(db_session, [COVER, COVER_END, FILING], packet2, note2)
    assert packet2.held_for_split_at is None
    assert packet2.meta["packet_starts"] == [3]  # the hint stays


def test_the_switch_is_admin_only(auth_enabled, db_session):
    auth_service.create_user(
        db_session,
        email="u@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.commit()
    c = TestClient(app)
    c.post(
        "/api/v1/auth/login",
        json={
            "email": "u@example.com",
            "password": "password123",  # pragma: allowlist secret
        },
    )
    assert (
        c.put("/api/v1/settings/ai/hold-packets", json={"enabled": False}).status_code
        == 403
    )
