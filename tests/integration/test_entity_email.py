"""The From address of a mail is recorded on the case entity it names."""

import email.message

import pytest

from app.models.database import Document, Entity
from app.models.enums import EntityType, IngestBatchSourceType, OriginatorType
from app.services.entity_email import attach_sender_email
from app.services.ingestion.batch_orchestrator import ingest_raw_email

pytestmark = pytest.mark.integration


def _batch_doc(db, case_id, sender="RA Hans Müller <h.mueller@kanzlei.example>"):
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "client@example.com"
    msg["Subject"] = "Schriftsatz"
    msg["Message-ID"] = "<ee@example.com>"
    msg.set_content("Der Inhalt des Schriftsatzes.")
    batch = ingest_raw_email(
        db, msg.as_bytes(), source_type=IngestBatchSourceType.EMAIL
    )
    doc = db.query(Document).filter(Document.ingest_batch_id == batch.id).one()
    doc.case_id = case_id
    db.commit()
    return doc


def _entity(db, case_id, name, type_=EntityType.PERSON, **kw):
    entity = Entity(case_id=case_id, type=type_, name=name, **kw)
    db.add(entity)
    db.commit()
    return entity


def test_the_from_address_is_recorded_on_the_entity_its_name_names(
    db_session, sample_case
):
    doc = _batch_doc(
        db_session, sample_case.id, '"Müller, Hans" <H.Mueller@Kanzlei.example>'
    )
    entity = _entity(db_session, sample_case.id, "Hans Müller")
    other = _entity(db_session, sample_case.id, "Erika Schmidt")

    assert attach_sender_email(db_session, doc.id) == 1
    db_session.commit()
    db_session.refresh(entity)
    db_session.refresh(other)
    assert entity.email == "h.mueller@kanzlei.example"
    assert other.email is None


def test_an_address_already_recorded_is_kept(db_session, sample_case):
    doc = _batch_doc(db_session, sample_case.id, "Hans Müller <new@kanzlei.example>")
    entity = _entity(
        db_session, sample_case.id, "Hans Müller", email="old@kanzlei.example"
    )
    assert attach_sender_email(db_session, doc.id) == 0
    db_session.refresh(entity)
    assert entity.email == "old@kanzlei.example"


def test_no_entity_is_created_from_a_header(db_session, sample_case):
    doc = _batch_doc(db_session, sample_case.id)
    assert attach_sender_email(db_session, doc.id) == 0
    assert db_session.query(Entity).filter_by(case_id=sample_case.id).count() == 0


def test_a_bare_address_without_a_display_name_maps_nothing(db_session, sample_case):
    doc = _batch_doc(db_session, sample_case.id, "h.mueller@kanzlei.example")
    _entity(db_session, sample_case.id, "Hans Müller")
    assert attach_sender_email(db_session, doc.id) == 0


def test_the_users_own_mail_is_skipped(db_session, sample_case):
    doc = _batch_doc(db_session, sample_case.id, "Hans Müller <me@example.com>")
    doc.originator_type = OriginatorType.OWN
    entity = _entity(db_session, sample_case.id, "Hans Müller")
    assert attach_sender_email(db_session, doc.id) == 0
    db_session.refresh(entity)
    assert entity.email is None


def test_a_document_still_in_the_triage_inbox_maps_nothing(db_session, sample_case):
    doc = _batch_doc(db_session, "_TRIAGE")
    _entity(db_session, sample_case.id, "RA Hans Müller")
    assert attach_sender_email(db_session, doc.id) == 0


def test_own_mail_attachments_and_forwards_are_skipped_by_from_address(
    db_session, sample_case, sample_user
):
    """Ingest does not stamp OWN on an own mail's attachments, so the skip keys
    on the From address being one of the owner's own."""
    from app.services import user_settings_service

    user_settings_service.set_gmail_own_addresses(
        db_session, sample_user.id, ["me@example.com"], "2026-10-17T00:00:00"
    )
    db_session.commit()
    msg = email.message.EmailMessage()
    msg["From"] = "Hans Müller <ME@example.com>"
    msg["To"] = "lawyer@example.com"
    msg["Subject"] = "Bescheid"
    msg["Message-ID"] = "<own-att@example.com>"
    msg.set_content("Bitte prüfen.")
    msg.add_attachment(
        b"%PDF-1.4 bescheid",
        maintype="application",
        subtype="pdf",
        filename="Bescheid.pdf",
    )
    batch = ingest_raw_email(
        db_session,
        msg.as_bytes(),
        source_type=IngestBatchSourceType.EMAIL,
        owner_id=sample_user.id,
        own_addresses={"me@example.com"},
    )
    attachment = (
        db_session.query(Document)
        .filter_by(ingest_batch_id=batch.id, original_filename="Bescheid.pdf")
        .one()
    )
    attachment.case_id = sample_case.id
    entity = _entity(db_session, sample_case.id, "Hans Müller")

    assert attach_sender_email(db_session, attachment.id) == 0
    db_session.refresh(entity)
    assert entity.email is None


def test_variants_the_extractor_unifies_get_the_address_too(db_session, sample_case):
    sample_case.parties = [{"name": "Liu Yingying", "role": "client"}]
    db_session.commit()
    doc = _batch_doc(db_session, sample_case.id, "Yingying Liu <y.liu@example.com>")
    entity = _entity(db_session, sample_case.id, "Liu Yingying")

    assert attach_sender_email(db_session, doc.id) == 1
    db_session.commit()
    db_session.refresh(entity)
    assert entity.email == "y.liu@example.com"


def test_a_sub_unit_address_is_recorded_on_the_parent_that_exists(
    db_session, sample_case
):
    doc = _batch_doc(
        db_session, sample_case.id, '"Landratsamt Ingolstadt, Amt 5" <amt5@lra.example>'
    )
    entity = _entity(
        db_session, sample_case.id, "Landratsamt Ingolstadt", EntityType.ORGANIZATION
    )

    assert attach_sender_email(db_session, doc.id) == 1
    db_session.commit()
    db_session.refresh(entity)
    assert entity.email == "amt5@lra.example"


def test_a_bare_honorific_names_nobody(db_session, sample_case):
    doc = _batch_doc(db_session, sample_case.id, '"Dr." <someone@example.com>')
    entity = _entity(db_session, sample_case.id, "Frau")
    assert attach_sender_email(db_session, doc.id) == 0
    db_session.refresh(entity)
    assert entity.email is None
