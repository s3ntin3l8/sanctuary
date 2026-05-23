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
def test_enclosed_doc_id_wires_enclosure_with_null_matched_filename(
    db_session, batch_with_two_docs
):
    """The new primary linkage: enclosed_doc_id wires the enclosure even when
    matched_filename is null. This is the IB-0033 failure mode — pre-fix, the
    AI returned null filenames and every doc was downgraded to STANDALONE."""
    batch, cover, enclosure = batch_with_two_docs

    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {
                        "description": "Klageerwiderung",
                        "attributed_originator": "Opposing counsel",
                        "originator_type": "opposing",
                        "enclosed_doc_id": enclosure.id,
                        "matched_filename": None,
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)
    db_session.expire_all()
    cover = db_session.get(Document, cover.id)
    enclosure = db_session.get(Document, enclosure.id)

    assert cover.role == DocumentRole.COVER_LETTER
    assert enclosure.role == DocumentRole.ENCLOSURE
    assert enclosure.parent_id == cover.id


@pytest.mark.unit
def test_enclosed_doc_id_rejects_self_reference(db_session, batch_with_two_docs):
    """A bundle where enclosed_doc_id == cover_letter_doc_id is malformed AI
    output; the cover gets no enclosures wired and is downgraded to STANDALONE."""
    batch, cover, enclosure = batch_with_two_docs

    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {
                        "description": "self-ref",
                        "originator_type": "court",
                        "enclosed_doc_id": cover.id,
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)
    db_session.expire_all()
    cover = db_session.get(Document, cover.id)
    enclosure = db_session.get(Document, enclosure.id)

    assert cover.role == DocumentRole.STANDALONE
    assert enclosure.parent_id is None


@pytest.mark.unit
def test_apply_batch_results_persists_encloses_relationship(
    db_session, batch_with_two_docs
):
    """Cover-letter → enclosure pairs are written as DocumentRelationship(ENCLOSES)."""
    from app.models.database import DocumentRelationship
    from app.models.enums import RelationshipType

    batch, cover, enclosure = batch_with_two_docs

    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {
                        "description": "Klageerwiderung",
                        "attributed_originator": "Opposing counsel",
                        "originator_type": "opposing",
                        "matched_filename": "Klageerwiderung.pdf",
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)
    db_session.commit()

    rels = (
        db_session.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == cover.id)
        .all()
    )
    assert len(rels) == 1
    assert rels[0].to_document_id == enclosure.id
    assert rels[0].relationship_type == RelationshipType.ENCLOSES


@pytest.mark.unit
def test_apply_batch_results_encloses_idempotent(db_session, batch_with_two_docs):
    """Re-applying the same batch result does not duplicate the ENCLOSES row."""
    from app.models.database import DocumentRelationship

    batch, cover, enclosure = batch_with_two_docs
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {
                        "description": "x",
                        "originator_type": "opposing",
                        "matched_filename": "Klageerwiderung.pdf",
                    }
                ],
            }
        ],
        "detected_actions": [],
    }
    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)
    db_session.commit()
    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)
    db_session.commit()

    count = (
        db_session.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == cover.id)
        .count()
    )
    assert count == 1


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

    batch, cover, enclosure = batch_with_two_docs

    detected = [
        {
            "title": "File response",
            "action_type": "deadline",
            "due_date": "2025-06-30",
            "description": "Must respond to opposing motion",
            "confidence": "high",
        }
    ]
    result = {
        "cover_letter_doc_id": cover.id,
        "is_cover_letter": True,
        "court_relay": False,
        "enclosed_descriptions": [],
        "detected_actions": detected,
    }

    _apply_batch_results(batch.id, [cover, enclosure], result, db_session)

    # Batch analyzer stores detected_actions as hints; it does NOT create ActionItem rows.
    db_session.refresh(batch)
    assert batch.detected_actions == detected

    # No ActionItem rows created at batch-analysis time — enricher is sole owner.
    items = (
        db_session.query(ActionItem).filter(ActionItem.case_id == sample_case.id).all()
    )
    assert len(items) == 0


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

    # Batch owns structure: document is wired as enclosure under the cover letter.
    assert lawyer_letter.role == DocumentRole.ENCLOSURE
    assert lawyer_letter.parent_id == court_relay.id
    # Metadata owns classification: OWN originator and attributed_originator survive.
    assert lawyer_letter.originator_type == OriginatorType.OWN
    assert lawyer_letter.attributed_originator == "Haidl Funk Rechtsanwälte"


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


@pytest.mark.unit
def test_two_cover_letters_dont_swallow_each_other(db_session, sample_case):
    """Regression: when the AI returns multiple bundles (each with its own cover
    letter), the completion sweep must NOT let one cover letter claim another
    cover letter as its enclosure. Doing so would downgrade the second cover
    and (because the loop continues) end up with mutual parent_id references
    — observed in batch_1 retry on 2026-05-07 where doc_2 ended up as
    enclosure of doc_5 and doc_5 as enclosure of doc_2.
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

    docs = []
    for title, originator in [
        ("Begleitschreiben AG – Beschluss", OriginatorType.COURT),  # cover 1
        ("Beschluss AG – einstweilige Anordnung", OriginatorType.COURT),  # encl 1
        ("Begleitschreiben AG – Vermerk", OriginatorType.COURT),  # cover 2
        ("Vermerk gerichtlicher Sitzung", OriginatorType.COURT),  # encl 2
    ]:
        d = Document(
            title=title,
            content="Long enough content. " * 5,
            case_id=sample_case.id,
            ingest_batch_id=batch.id,
            originator_type=originator,
            proceeding_id=proc.id,
        )
        db_session.add(d)
        docs.append(d)
    db_session.commit()
    for d in docs:
        db_session.refresh(d)

    cover1, encl1, cover2, encl2 = docs

    # AI returned two clean bundles, each cover letter with its own enclosure
    # matched by exact filename.
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover1.id,
                "enclosed": [
                    {
                        "description": "Beschluss",
                        "attributed_originator": "AG Hamburg",
                        "originator_type": "court",
                        "matched_filename": encl1.title,
                    }
                ],
            },
            {
                "cover_letter_doc_id": cover2.id,
                "enclosed": [
                    {
                        "description": "Vermerk",
                        "attributed_originator": "AG Hamburg",
                        "originator_type": "court",
                        "matched_filename": encl2.title,
                    }
                ],
            },
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, docs, result, db_session)

    db_session.expire_all()
    cover1 = db_session.get(Document, cover1.id)
    cover2 = db_session.get(Document, cover2.id)
    encl1 = db_session.get(Document, encl1.id)
    encl2 = db_session.get(Document, encl2.id)

    # Both cover letters keep their role; neither becomes the other's enclosure.
    assert cover1.role == DocumentRole.COVER_LETTER, (
        f"cover1 was downgraded to {cover1.role}"
    )
    assert cover2.role == DocumentRole.COVER_LETTER, (
        f"cover2 was downgraded to {cover2.role}"
    )
    assert cover1.parent_id is None
    assert cover2.parent_id is None
    # Each enclosure stays under its own cover.
    assert encl1.parent_id == cover1.id
    assert encl2.parent_id == cover2.id
    # No cycles: cover1 not under cover2, cover2 not under cover1.
    assert cover1.parent_id != cover2.id
    assert cover2.parent_id != cover1.id


@pytest.mark.unit
def test_batch_overrides_court_originator_with_party_type(db_session, sample_case):
    """Fix 2a regression: when METADATA misclassified a party-authored enclosure
    as COURT (e.g. doc #98 which has a court Rubrum header), the batch AI's
    explicit OPPOSING classification for that enclosure must override it.

    OWN/OPPOSING/THIRD_PARTY set by METADATA are still protected (batch AI
    cannot downgrade them to COURT)."""
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
        title="Begleitschreiben Amtsgericht",
        content="Das Gericht übermittelt...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
        court_relay=True,
    )
    # METADATA misclassified as COURT (confused by the Rubrum header).
    party_filing = Document(
        title="Antrag auf alleiniges Sorgerecht",
        content="Im Namen meiner Mandantin beantrage ich...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,  # wrong — misfire from METADATA
        attributed_originator=None,
        sender="Yingying Liu",
    )
    db_session.add(cover)
    db_session.add(party_filing)
    db_session.commit()
    db_session.refresh(cover)
    db_session.refresh(party_filing)
    db_session.refresh(batch)

    # Batch AI correctly identifies the enclosure as OPPOSING.
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {
                        "enclosed_doc_id": party_filing.id,
                        "originator_type": "opposing",
                        "attributed_originator": "Yingying Liu",
                        "matched_filename": None,
                    }
                ],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, party_filing], result, db_session)

    db_session.expire_all()
    party_filing = db_session.get(Document, party_filing.id)

    assert party_filing.role == DocumentRole.ENCLOSURE
    # Batch AI's OPPOSING must override METADATA's incorrect COURT.
    assert party_filing.originator_type == OriginatorType.OPPOSING
    assert party_filing.attributed_originator == "Yingying Liu"


@pytest.mark.unit
def test_completion_sweep_sets_attributed_originator_from_sender(
    db_session, sample_case
):
    """Fix 2b: when the completion sweep claims an unclaimed doc as an enclosure
    (doc wasn't in any AI bundle), it must fill attributed_originator from the
    doc's sender field if not already set."""
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        status=IngestBatchStatus.PENDING,
        received_at=datetime.now(),
        ingest_date=datetime.now(),
    )
    db_session.add(batch)
    db_session.flush()

    from app.models.database import Proceeding

    proceeding = Proceeding(
        case_id=sample_case.id,
        court_level="ag",
        court_name="Amtsgericht Ingolstadt",
    )
    db_session.add(proceeding)
    db_session.flush()

    cover = Document(
        title="Begleitschreiben",
        content="Das Gericht übersendet...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
        court_relay=True,
        proceeding_id=proceeding.id,
    )
    # Not in any bundle — will be picked up by completion sweep.
    unclaimed = Document(
        title="Antrag",
        content="Im Namen meiner Mandantin...",
        case_id=sample_case.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
        attributed_originator=None,
        sender="Yingying Liu",
        proceeding_id=proceeding.id,
    )
    db_session.add(cover)
    db_session.add(unclaimed)
    db_session.commit()
    db_session.refresh(cover)
    db_session.refresh(unclaimed)
    db_session.refresh(batch)

    # AI produced a bundle with only the cover (enclosed is empty) —
    # unclaimed doc is not in any bundle.
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [],
            }
        ],
        "detected_actions": [],
    }

    _apply_batch_results(batch.id, [cover, unclaimed], result, db_session)

    db_session.expire_all()
    unclaimed = db_session.get(Document, unclaimed.id)

    # Completion sweep wired it as an enclosure.
    assert unclaimed.role == DocumentRole.ENCLOSURE
    assert unclaimed.parent_id == cover.id
    # attributed_originator must be propagated from sender.
    assert unclaimed.attributed_originator == "Yingying Liu"
