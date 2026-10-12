"""Copies of documents already in the system are linked, not re-analysed."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.database import (
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    Document,
    DocumentRelationship,
    IngestBatch,
    User,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    PipelineStage,
    ProposalConfidence,
    ProposalStatus,
    RelationshipConfidence,
    RelationshipType,
)
from app.repositories.document_relationship import reject_edge
from app.services.intelligence import copy_detector as cd

pytestmark = pytest.mark.unit

RULING = (
    "Amtsgericht Ingolstadt Abteilung für Familiensachen Beschluss in der "
    "Familiensache Liu gegen Hansen wegen elterlicher Sorge. Der Antrag der "
    "Antragstellerin auf Übertragung der alleinigen elterlichen Sorge wird "
    "zurückgewiesen. Die Kosten des Verfahrens trägt die Antragstellerin. "
    "Der Verfahrenswert wird auf 4000 Euro festgesetzt. Rechtsbehelfsbelehrung: "
    "Gegen diesen Beschluss ist die Beschwerde zulässig, wenn sie innerhalb "
    "eines Monats nach der schriftlichen Bekanntgabe beim Amtsgericht eingeht."
)
DAY = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)


@pytest.fixture
def env(db_session):
    owner = db_session.query(User).filter_by(email="admin@localhost").one()

    def batch():
        b = IngestBatch(
            owner_id=owner.id,
            source_type=IngestBatchSourceType.SCAN,
            status=IngestBatchStatus.PROCESSING,
            received_at=datetime(2026, 10, 9, tzinfo=UTC),
        )
        db_session.add(b)
        db_session.flush()
        return b

    def doc(batch, content=RULING, **kw):
        kw.setdefault("title", "Beschluss")
        kw.setdefault("sender", "Amtsgericht Ingolstadt")
        kw.setdefault("issued_date", DAY)
        kw.setdefault("role", DocumentRole.STANDALONE)
        d = Document(
            owner_id=owner.id,
            case_id="_TRIAGE",
            ingest_batch_id=batch.id,
            content=content,
            **kw,
        )
        db_session.add(d)
        db_session.flush()
        return d

    return db_session, batch, doc


def _edges(db, from_id=None):
    q = db.query(DocumentRelationship).filter(
        DocumentRelationship.relationship_type == RelationshipType.COPY_OF
    )
    if from_id is not None:
        q = q.filter(DocumentRelationship.from_document_id == from_id)
    return q.all()


def test_containment_is_about_the_shorter_text():
    a = cd._shingles(RULING)
    b = cd._shingles("Eingegangen 28. AUG. 2026 Nachtbriefkasten " + RULING)
    assert cd.containment(a, a) == 1.0
    assert cd.containment(a, b) >= cd.COPY_CONTAINMENT_MIN
    assert cd.containment(a, cd._shingles("etwas ganz anderes " * 40)) < 0.1
    assert cd.containment(a, set()) == 0.0


def test_an_exhibit_is_linked_to_the_letter_it_copies(env):
    db, batch, doc = env
    original = doc(batch(), ingest_date=DAY)
    exhibit = doc(
        batch(),
        content="![stamp]() Eingegangen 28. AUG. 2026 " + RULING,
        role=DocumentRole.ENCLOSURE,
        ingest_date=DAY + timedelta(days=100),
    )

    outcome = cd.detect_and_link(db, exhibit)

    assert outcome.original_id == original.id
    (edge,) = _edges(db, exhibit.id)
    assert edge.to_document_id == original.id
    assert edge.confidence == RelationshipConfidence.AI_DETECTED
    assert cd.is_copy(db, exhibit.id) and not cd.is_copy(db, original.id)


def test_no_match_on_another_day_or_without_a_tie(env):
    db, batch, doc = env
    doc(batch(), ingest_date=DAY)
    other_day = doc(batch(), issued_date=DAY + timedelta(days=3))
    no_tie = doc(batch(), sender="Haidl Funk Rechtsanwälte")

    assert cd.detect_and_link(db, other_day).original_id is None
    assert cd.detect_and_link(db, no_tie).original_id is None
    assert _edges(db) == []


def test_a_shared_court_file_number_is_a_tie(env):
    db, batch, doc = env
    original = doc(batch(), sender="AG Ingolstadt", az_court="3 F 1221/26")
    copy = doc(
        batch(),
        sender="Amtsgericht Ingolstadt, Familiengericht",
        az_court="3 F 1221/26",
        role=DocumentRole.ENCLOSURE,
    )

    assert cd.detect_and_link(db, copy).original_id == original.id


def test_documents_of_one_bundle_are_not_each_others_copies(env):
    db, batch, doc = env
    b = batch()
    doc(b)
    twin = doc(b)
    assert cd.detect_and_link(db, twin).original_id is None


def test_a_short_text_never_matches(env):
    db, batch, doc = env
    doc(batch(), content="Eingang bestätigt.")
    short = doc(batch(), content="Eingang bestätigt.")
    assert cd.detect_and_link(db, short).original_id is None


def test_the_original_may_arrive_after_its_copy(env):
    db, batch, doc = env
    copy = doc(
        batch(),
        role=DocumentRole.ENCLOSURE,
        ingest_date=DAY + timedelta(days=100),
    )
    assert cd.detect_and_link(db, copy).original_id is None  # nothing to copy yet

    original = doc(batch(), ingest_date=DAY + timedelta(days=200))
    outcome = cd.detect_and_link(db, original)

    # A free-standing letter is the original even though it arrived later.
    assert outcome.original_id is None
    assert outcome.copy_ids == [copy.id]
    (edge,) = _edges(db, copy.id)
    assert edge.to_document_id == original.id


def test_detection_is_idempotent_and_honours_a_rejection(env):
    db, batch, doc = env
    doc(batch(), ingest_date=DAY)
    copy = doc(
        batch(), role=DocumentRole.ENCLOSURE, ingest_date=DAY + timedelta(days=9)
    )

    cd.detect_and_link(db, copy)
    cd.detect_and_link(db, copy)
    (edge,) = _edges(db, copy.id)

    reject_edge(db, edge)
    db.flush()
    assert cd.detect_and_link(db, copy).original_id is None
    assert _edges(db, copy.id) == []


def test_quiet_copy_removes_only_what_the_machine_made(env):
    db, batch, doc = env
    original = doc(batch())
    other = doc(batch(), content="Ein ganz anderes Schreiben " * 12, sender="X")
    copy = doc(batch(), role=DocumentRole.ENCLOSURE)
    claim = Claim(
        claim_text="Der Verfahrenswert beträgt 4000 Euro",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db.add(claim)
    db.flush()
    db.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=copy.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db.add(
        ClaimEvidenceProposal(
            target_claim_id=claim.id,
            source_document_id=copy.id,
            proposed_role=ClaimEvidenceRole.SUPPORTS,
            confidence=ProposalConfidence.MEDIUM,
            status=ProposalStatus.PENDING,
        )
    )
    for to_doc, conf in (
        (other, RelationshipConfidence.AI_DETECTED),
        (original, RelationshipConfidence.USER_CONFIRMED),
    ):
        db.add(
            DocumentRelationship(
                from_document_id=copy.id,
                to_document_id=to_doc.id,
                relationship_type=RelationshipType.REFERENCES,
                confidence=conf,
            )
        )
    db.flush()

    with patch("app.services.pipeline_status.mark_skipped") as skipped:
        cd.quiet_copy(db, copy)
    db.flush()

    assert {c.args[1] for c in skipped.call_args_list} == {
        PipelineStage.RELATIONSHIPS,
        PipelineStage.CLAIMS,
    }
    assert db.get(Claim, claim.id) is None
    assert db.query(ClaimEvidenceProposal).count() == 0
    kept = db.query(DocumentRelationship).all()
    assert [r.confidence for r in kept] == [RelationshipConfidence.USER_CONFIRMED]


def test_the_relationship_prompt_cannot_emit_copy_of():
    from app.services.intelligence.relationship_detector import (
        VALID_RELATIONSHIP_TYPES,
    )

    assert RelationshipType.COPY_OF.value not in VALID_RELATIONSHIP_TYPES


def test_enrich_hook_links_and_quiets_a_copy(env):
    from app.services import pipeline_status
    from app.tasks.enrich_document import _link_copies

    db, batch, doc = env
    original = doc(batch(), ingest_date=DAY)
    copy = doc(
        batch(), role=DocumentRole.ENCLOSURE, ingest_date=DAY + timedelta(days=9)
    )
    pipeline_status.initialize(copy, batched=True, db=db)
    db.commit()

    with (
        patch("app.dependencies.get_db_session", return_value=db),
        patch.object(db, "close"),
    ):
        assert _link_copies(copy.id) == original.id
        assert _link_copies(original.id) is None  # the original is not a copy

    db.expire_all()
    stages = pipeline_status.stages_dict(db.get(Document, copy.id))
    assert stages[PipelineStage.RELATIONSHIPS.value]["status"] == "skipped"
    assert stages[PipelineStage.CLAIMS.value]["reason"] == "copy_of"


def test_the_claims_and_relationship_tasks_skip_a_copy(env):
    from datetime import UTC

    from app.services import pipeline_status
    from app.tasks.detect_relationships import detect_relationships_task
    from app.tasks.extract_claims import extract_claims_task

    db, batch, doc = env
    original = doc(batch(), ingest_date=DAY)
    copy = doc(
        batch(), role=DocumentRole.ENCLOSURE, ingest_date=DAY + timedelta(days=9)
    )
    pipeline_status.initialize(copy, batched=True, db=db)
    copy.ai_summary_created_at = datetime.now(UTC)
    db.add(
        DocumentRelationship(
            from_document_id=copy.id,
            to_document_id=original.id,
            relationship_type=RelationshipType.COPY_OF,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    for stage in (PipelineStage.ENRICH,):
        pipeline_status.mark_completed(copy.id, stage, db)
    db.commit()

    with (
        patch("app.dependencies.get_db_session", return_value=db),
        patch.object(db, "close"),
        patch("app.services.intelligence.orchestrator.trigger_case_brief_if_ready"),
        patch("app.services.intelligence.claim_extractor.extract") as extract,
        patch("app.services.intelligence.relationship_detector.detect") as detect,
    ):
        claims = extract_claims_task.run(copy.id)
        rels = detect_relationships_task.run(copy.id)

    assert claims["reason"] == "copy_of" and rels["reason"] == "copy_of"
    extract.assert_not_called()
    detect.assert_not_called()


def test_a_copy_already_analysed_is_cleaned_when_its_original_arrives(env):
    """The harder order: the copy's CLAIMS and RELATIONSHIPS ran to completion
    before the original existed. Its machine-made outputs go; a person's stay."""
    from app.models.database import ActionItem
    from app.models.enums import ActionItemStatus, ActionItemType
    from app.services import pipeline_status
    from app.tasks.enrich_document import _link_copies

    db, batch, doc = env
    other = doc(batch(), content="Ein ganz anderes Schreiben " * 12, sender="X")
    copy = doc(
        batch(), role=DocumentRole.ENCLOSURE, ingest_date=DAY + timedelta(days=9)
    )
    pipeline_status.initialize(copy, batched=True, db=db)
    for stage in (PipelineStage.CLAIMS, PipelineStage.RELATIONSHIPS):
        pipeline_status.mark_completed(copy.id, stage, db)
    claim = Claim(
        claim_text="Der Verfahrenswert beträgt 4000 Euro",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=datetime.now(),
        last_updated_at=datetime.now(),
    )
    db.add(claim)
    db.flush()
    db.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=copy.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db.add(
        ActionItem(
            case_id="_TRIAGE",
            source_document_id=copy.id,
            title="Beschwerde einlegen",
            due_date=DAY + timedelta(days=400),
            action_type=ActionItemType.DEADLINE,
            status=ActionItemStatus.OPEN,
        )
    )
    db.add_all(
        [
            DocumentRelationship(
                from_document_id=copy.id,
                to_document_id=other.id,
                relationship_type=RelationshipType.REFERENCES,
                confidence=RelationshipConfidence.AI_DETECTED,
            ),
            DocumentRelationship(
                from_document_id=copy.id,
                to_document_id=other.id,
                relationship_type=RelationshipType.REPLIES_TO,
                confidence=RelationshipConfidence.USER_CONFIRMED,
            ),
        ]
    )
    db.commit()

    original = doc(batch(), ingest_date=DAY + timedelta(days=200))
    db.commit()
    with (
        patch("app.dependencies.get_db_session", return_value=db),
        patch.object(db, "close"),
    ):
        assert _link_copies(original.id) is None

    db.expire_all()
    stages = pipeline_status.stages_dict(db.get(Document, copy.id))
    assert stages[PipelineStage.CLAIMS.value]["status"] == "skipped"
    assert stages[PipelineStage.RELATIONSHIPS.value]["reason"] == "copy_of"
    assert db.get(Claim, claim.id) is None
    assert db.query(ActionItem).count() == 0
    kept = {
        r.relationship_type
        for r in db.query(DocumentRelationship).filter(
            DocumentRelationship.relationship_type != RelationshipType.COPY_OF
        )
    }
    assert kept == {RelationshipType.REPLIES_TO}  # the person's edge stays
    (edge,) = _edges(db, copy.id)
    assert edge.to_document_id == original.id


# --- scoring on distinctive text (#304) -------------------------------------

TEMPLATE = " ".join(f"vordruck{i}" for i in range(80))


def _unique(tag: str, n: int) -> str:
    return " ".join(f"{tag}wort{i}" for i in range(n))


def test_form_letters_sharing_a_sender_template_are_not_copies(env):
    db, batch, doc = env
    # Raw containment ~0.8 (template dominates), distinctive ~0.
    a = doc(batch(), content=f"{TEMPLATE} {_unique('a', 20)}")
    b = doc(batch(), content=f"{TEMPLATE} {_unique('b', 20)}")
    doc(batch(), content=f"{TEMPLATE} {_unique('c', 40)}")  # defines the boilerplate
    assert cd.containment(cd._shingles(a.content), cd._shingles(b.content)) > 0.75

    assert cd.detect_and_link(db, b).original_id is None
    assert _edges(db) == []


def test_a_noisy_copy_is_still_linked_on_distinctive_text(env):
    db, batch, doc = env
    body = f"{TEMPLATE} {_unique('x', 120)}"
    original = doc(batch(), content=body, ingest_date=DAY)
    doc(batch(), content=f"{TEMPLATE} {_unique('y', 120)}")  # same-sender form
    words = body.split()
    for i in range(7, len(words), 50):  # ~2% word error
        words[i] = "ocrfehler"
    copy = doc(
        batch(),
        content=" ".join(words),
        role=DocumentRole.ENCLOSURE,
        ingest_date=DAY + timedelta(days=9),
    )

    assert cd.detect_and_link(db, copy).original_id == original.id
    (edge,) = _edges(db, copy.id)
    assert "(distinctive)" in edge.notes


def test_a_short_distinctive_remainder_falls_back_to_raw_containment(env):
    db, batch, doc = env
    original = doc(batch(), content=f"{TEMPLATE} {_unique('z', 3)}", ingest_date=DAY)
    doc(batch(), content=f"{TEMPLATE} {_unique('q', 30)}")
    copy = doc(
        batch(),
        content=f"{TEMPLATE} {_unique('z', 3)}",
        role=DocumentRole.ENCLOSURE,
        ingest_date=DAY + timedelta(days=9),
    )

    assert cd.detect_and_link(db, copy).original_id == original.id
    (edge,) = _edges(db, copy.id)
    assert "(distinctive)" not in edge.notes
