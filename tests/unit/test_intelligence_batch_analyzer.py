"""Tests for Phase 4 batch analyzer."""

from datetime import datetime

import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
)
from app.services.intelligence.batch_analyzer import _apply_batch_results


@pytest.fixture
def batch_with_two_docs(db_session, sample_case):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        sender_email="court@ag-hamburg.de",
        subject="Begleitschreiben + Anlage",
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    cover = Document(
        title="Begleitschreiben",
        content="Im Auftrag des Gerichts übersende ich anliegend...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    enclosure = Document(
        title="Klageerwiderung.pdf",
        content="Die Beklagte widerspricht der Klage...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.UNKNOWN,
    )
    db_session.add(cover)
    db_session.add(enclosure)
    db_session.commit()
    db_session.refresh(cover)
    db_session.refresh(enclosure)
    db_session.refresh(batch)
    return batch, cover, enclosure


@pytest.mark.unit
def test_cover_letter_detected(db_session, batch_with_two_docs):
    batch, cover, enclosure = batch_with_two_docs

    result = {
        "cover_letter_doc_id": cover.id,
        "is_cover_letter": True,
        "court_relay": True,
        "enclosed_descriptions": [
            {
                "description": "Klageerwiderung",
                "attributed_originator": "Opposing counsel",
                "originator_type": "opposing",
                "matched_filename": "Klageerwiderung.pdf",
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)

    db_session.expire_all()
    cover = db_session.get(Document, cover.id)
    enclosure = db_session.get(Document, enclosure.id)

    assert cover.role == DocumentRole.COVER_LETTER
    # court_relay is owned by METADATA — batch analyzer must not overwrite it
    # even when the AI response contains the legacy court_relay key.
    assert cover.court_relay is False
    assert enclosure.role == DocumentRole.ENCLOSURE
    assert enclosure.parent_id == cover.id
    assert enclosure.attributed_originator == "Opposing counsel"
    assert enclosure.originator_type == OriginatorType.OPPOSING


@pytest.mark.unit
def test_single_doc_gets_standalone_role(db_session, sample_case):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.SCAN,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    doc = Document(
        title="Urteil.pdf",
        content="Das Urteil lautet...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    result = {
        "cover_letter_doc_id": None,
        "is_cover_letter": False,
        "court_relay": False,
        "enclosed_descriptions": [],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [doc], result, db_session)

    db_session.expire_all()
    doc = db_session.get(Document, doc.id)
    assert doc.role == DocumentRole.STANDALONE
    assert doc.parent_id is None


@pytest.mark.unit
def test_action_items_created(db_session, sample_case, batch_with_two_docs):
    from app.models.database import ActionItem
    from app.models.enums import ActionItemStatus, ActionItemType

    batch, cover, enclosure = batch_with_two_docs

    result = {
        "cover_letter_doc_id": cover.id,
        "is_cover_letter": True,
        "court_relay": False,
        "enclosed_descriptions": [],
        "detected_actions": [
            {
                "title": "File response",
                "action_type": "deadline",
                "due_date": "2025-06-30",
                "description": "Must respond to opposing motion",
                "confidence": "high",
            }
        ],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)

    items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).all()
    )
    assert len(items) == 1
    assert items[0].action_type == ActionItemType.DEADLINE
    assert items[0].title == "File response"
    assert items[0].status == ActionItemStatus.OPEN
    assert items[0].source_document_id == cover.id


@pytest.mark.unit
def test_metadata_originator_not_overwritten_by_batch(db_session, sample_case):
    """When metadata stage determined OWN originator, batch must not overwrite
    it with 'court' — even if the batch AI guessed the doc is a court enclosure.

    Reproduces the bug where doc 12 (lawyer's cover letter to client) was
    reclassified as COURT+ENCLOSURE by the batch analyzer despite metadata
    correctly identifying it as OWN.
    """
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    # The court relay doc that the batch AI picks as cover-letter candidate.
    court_relay = Document(
        title="Schr. OLG München v. 14.04.26",
        content="Im Auftrag des Gerichts...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
        court_relay=True,
    )
    # The lawyer-to-client letter — metadata stage already set OWN.
    lawyer_letter = Document(
        title="Schr an Mdt",
        content="Sehr geehrter Herr Hansen, anliegend übersende ich...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OWN,
        attributed_originator="Haidl Funk Rechtsanwälte",
    )
    db_session.add(court_relay)
    db_session.add(lawyer_letter)
    db_session.commit()
    db_session.refresh(court_relay)
    db_session.refresh(lawyer_letter)
    db_session.refresh(batch)

    # Batch AI guessed lawyer_letter is a court enclosure (wrong).
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": court_relay.id,
                "enclosed": [
                    {
                        "originator_type": "court",
                        "attributed_originator": "OLG München",
                        "matched_filename": "Schr an Mdt",
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [court_relay, lawyer_letter], result, db_session)

    db_session.expire_all()
    lawyer_letter = db_session.get(Document, lawyer_letter.id)

    # Metadata-determined OWN must survive the batch pass.
    assert lawyer_letter.originator_type == OriginatorType.OWN
    assert lawyer_letter.attributed_originator == "Haidl Funk Rechtsanwälte"
    assert lawyer_letter.role != DocumentRole.ENCLOSURE
    assert lawyer_letter.parent_id is None


@pytest.mark.unit
def test_null_cover_letter_doc_id_leaves_doc_standalone(db_session, sample_case):
    """When AI returns cover_letter_doc_id=null with enclosed entries, the doc
    must NOT be wired as ENCLOSURE — it should fall through to STANDALONE.

    Reproduces the doc-7 batch-3 bug: the AI signaled doc 7 as standalone
    (cover_letter_doc_id=null) but _apply_batch_results wired it as ENCLOSURE
    with parent_id=NULL anyway, poisoning the enricher's batch_context.
    """
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    standalone = Document(
        title="Schriftsatz Beschwerde",
        content="Some content here that is long enough to be healthy.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OWN,
        attributed_originator="Haidl Funk Rechtsanwälte",
    )
    db_session.add(standalone)
    db_session.commit()
    db_session.refresh(standalone)

    result = {
        "bundles": [
            {
                "cover_letter_doc_id": None,
                "enclosed": [
                    {
                        "description": "Schriftsatz Beschwerde",
                        "attributed_originator": "Hansen",
                        "originator_type": "own",
                        "matched_filename": "Schriftsatz Beschwerde",
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [standalone], result, db_session)

    db_session.expire_all()
    standalone = db_session.get(Document, standalone.id)

    assert standalone.role == DocumentRole.STANDALONE
    assert standalone.parent_id is None
    # Metadata's attributed_originator must survive; batch's "Hansen" guess is wrong.
    assert standalone.attributed_originator == "Haidl Funk Rechtsanwälte"


@pytest.mark.unit
def test_metadata_attributed_originator_preserved_for_non_court_enclosure(
    db_session, sample_case
):
    """For OWN/OPPOSING enclosures, metadata's sender extraction wins over
    batch's title-only guess. Court enclosures still let batch overwrite."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    cover = Document(
        title="Begleitschreiben",
        content="Im Auftrag des Mandanten übersende ich anliegend...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OWN,
    )
    enclosure = Document(
        title="Klageerwiderung Liu",
        content="Der Beklagte erwidert...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OPPOSING,
        attributed_originator="Kanzlei Müller & Partner",
    )
    db_session.add(cover)
    db_session.add(enclosure)
    db_session.commit()
    db_session.refresh(cover)
    db_session.refresh(enclosure)

    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {
                        "description": "Klageerwiderung",
                        "attributed_originator": "Liu",
                        "originator_type": "opposing",
                        "matched_filename": "Klageerwiderung Liu",
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)

    db_session.expire_all()
    enclosure = db_session.get(Document, enclosure.id)

    assert enclosure.role == DocumentRole.ENCLOSURE
    assert enclosure.parent_id == cover.id
    # Metadata's firm name survives — batch's party guess is rejected.
    assert enclosure.attributed_originator == "Kanzlei Müller & Partner"


@pytest.mark.unit
def test_completion_sweep_claims_unbundled_proceeding_siblings(db_session, sample_case):
    """When AI under-bundles, the post-bundle completion sweep must claim
    unclaimed siblings sharing the cover letter's proceeding_id as enclosures.

    Reproduces ib-0006 round-N behaviour: AI returned a bundle wiring only one
    enclosure (the doc whose title literally matched 'Abschrift'), leaving two
    other rulings from the same AG proceeding STANDALONE despite arriving in
    the same email. The originator guard must keep an OWN letter out even
    when its proceeding_id matches.
    """
    from app.models.database import Proceeding
    from app.models.enums import ProceedingCourtLevel, ProceedingStatus

    proc = Proceeding(
        case_id=sample_case.id,
        court_name="Amtsgericht Test",
        court_level=ProceedingCourtLevel.AG,
        az_court="003 F 553/26",
        status=ProceedingStatus.ACTIVE,
        started_at=datetime.now(),
    )
    db_session.add(proc)
    db_session.flush()

    other_proc = Proceeding(
        case_id=sample_case.id,
        court_name="OLG München",
        court_level=ProceedingCourtLevel.OLG,
        az_court="26 WF 363/26 E",
        status=ProceedingStatus.ACTIVE,
        started_at=datetime.now(),
    )
    db_session.add(other_proc)
    db_session.flush()

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        proceeding_id=proc.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    cover = Document(
        title="Beschlussabschrift",
        content="anbei erhalten Sie eine beglaubigte Abschrift des Beschlusses nebst Anlage.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        proceeding_id=proc.id,
        originator_type=OriginatorType.COURT,
    )
    enclosure_ai = Document(  # AI explicitly bundles this one
        title="Abschrift Beschluss",
        content="Beschlussabschrift content.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        proceeding_id=proc.id,
        originator_type=OriginatorType.COURT,
    )
    enclosure_swept = Document(  # AI doesn't bundle, sweep should claim
        title="Beschluss einstweilige Anordnung",
        content="Anordnung content.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        proceeding_id=proc.id,
        originator_type=OriginatorType.COURT,
    )
    own_letter = Document(  # OWN — must NOT be swept
        title="Anwaltsbrief",
        content="Sehr geehrter Herr Hansen, anbei...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        proceeding_id=proc.id,  # same proceeding, but originator guards it out
        originator_type=OriginatorType.OWN,
    )
    different_proc_doc = Document(  # different proceeding — must NOT be swept
        title="OLG Schreiben",
        content="OLG content.",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        proceeding_id=other_proc.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all(
        [cover, enclosure_ai, enclosure_swept, own_letter, different_proc_doc]
    )
    db_session.commit()
    for d in (cover, enclosure_ai, enclosure_swept, own_letter, different_proc_doc):
        db_session.refresh(d)

    # AI returns a single bundle that explicitly wires only enclosure_ai.
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {
                        "description": "Abschrift",
                        "attributed_originator": "Amtsgericht Test",
                        "originator_type": "court",
                        "matched_filename": "Abschrift Beschluss",
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    docs = [cover, enclosure_ai, enclosure_swept, own_letter, different_proc_doc]
    _apply_batch_results(batch.id, docs, result, db_session)

    db_session.expire_all()
    cover = db_session.get(Document, cover.id)
    enclosure_ai = db_session.get(Document, enclosure_ai.id)
    enclosure_swept = db_session.get(Document, enclosure_swept.id)
    own_letter = db_session.get(Document, own_letter.id)
    different_proc_doc = db_session.get(Document, different_proc_doc.id)

    assert cover.role == DocumentRole.COVER_LETTER
    # AI-bundled enclosure stays wired.
    assert enclosure_ai.role == DocumentRole.ENCLOSURE
    assert enclosure_ai.parent_id == cover.id
    # Sweep claims the unbundled court sibling sharing the proceeding.
    assert enclosure_swept.role == DocumentRole.ENCLOSURE
    assert enclosure_swept.parent_id == cover.id
    # OWN letter and different-proceeding doc must remain unbundled.
    assert own_letter.role == DocumentRole.STANDALONE
    assert own_letter.parent_id is None
    assert different_proc_doc.role == DocumentRole.STANDALONE
    assert different_proc_doc.parent_id is None
