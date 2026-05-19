"""Tests for the three fallback paths inside _apply_batch_results when the
AI returns an empty `bundles` list.

The fallbacks fire in order:
1. Single-relay  : exactly one doc carries court_relay=True
2. Proceeding    : pick a cover candidate + claim siblings sharing proceeding_id
3. Completion sweep: for every already-promoted COVER_LETTER, claim same-proc siblings

Each path has an originator_type guard that keeps own/opposing/third_party
documents out of enclosures. The two later paths had no direct tests; this
file fills both.
"""

from datetime import datetime

import pytest

from app.models.database import Case, Document, IngestBatch, Proceeding
from app.models.enums import (
    CaseStatus,
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    Jurisdiction,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services.intelligence.batch_analyzer import _apply_batch_results


def _make_case_with_proceeding(db_session, case_id: str = "TEST-FALLBACK"):
    case = Case(
        id=case_id,
        title="Fallback Case",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.flush()

    proc = Proceeding(
        case_id=case.id,
        court_level=ProceedingCourtLevel.AG,
        court_name="AG Hamburg",
        az_court="003 F 1/24",
        status=ProceedingStatus.ACTIVE,
        started_at=datetime(2026, 1, 1),
    )
    db_session.add(proc)
    db_session.flush()
    return case, proc


def _make_batch(db_session, case_id):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=case_id,
        status=IngestBatchStatus.PENDING,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


# ---------------------------------------------------------------------------
# Gap #2 — proceeding-grouping fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_proceeding_grouping_fallback_wires_siblings_under_cover_candidate(db_session):
    """Empty bundles + no court_relay + cover candidate with proceeding_id →
    siblings sharing the proceeding get role=ENCLOSURE pointing at the cover."""
    case, proc = _make_case_with_proceeding(db_session)
    batch = _make_batch(db_session, case.id)

    # Cover candidate: short content + cover keyword in title → wins
    # _pick_cover_letter_candidate's first pass.
    cover_candidate = Document(
        title="Begleitschreiben des AG",
        content="kurzer Schriftsatz",  # short → cover-keyword path matches
        case_id=case.id,
        proceeding_id=proc.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    sibling_a = Document(
        title="Beschluss",
        content="Langer Beschluss Text " * 50,
        case_id=case.id,
        proceeding_id=proc.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    sibling_b = Document(
        title="Anlage 2",
        content="weitere Anlage Text " * 50,
        case_id=case.id,
        proceeding_id=proc.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([cover_candidate, sibling_a, sibling_b])
    db_session.commit()

    docs = [cover_candidate, sibling_a, sibling_b]
    _apply_batch_results(batch.id, docs, {"bundles": []}, db_session)
    db_session.commit()

    for d in docs:
        db_session.refresh(d)

    assert cover_candidate.role == DocumentRole.COVER_LETTER
    assert cover_candidate.parent_id is None
    assert sibling_a.role == DocumentRole.ENCLOSURE
    assert sibling_a.parent_id == cover_candidate.id
    assert sibling_b.role == DocumentRole.ENCLOSURE
    assert sibling_b.parent_id == cover_candidate.id


@pytest.mark.unit
def test_proceeding_grouping_fallback_skips_own_and_opposing_originators(db_session):
    """The fallback's originator guard must exclude own/opposing/third_party docs
    from being swept under the cover candidate."""
    case, proc = _make_case_with_proceeding(db_session, case_id="TEST-FALLBACK-2")
    batch = _make_batch(db_session, case.id)

    cover = Document(
        title="Begleitschreiben",
        content="kurz",
        case_id=case.id,
        proceeding_id=proc.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    own_doc = Document(
        title="Eigene Stellungnahme",
        content="Stellungnahme Text " * 50,
        case_id=case.id,
        proceeding_id=proc.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OWN,
    )
    opposing_doc = Document(
        title="Schriftsatz Gegenseite",
        content="Gegenschriftsatz " * 50,
        case_id=case.id,
        proceeding_id=proc.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OPPOSING,
    )
    db_session.add_all([cover, own_doc, opposing_doc])
    db_session.commit()

    docs = [cover, own_doc, opposing_doc]
    _apply_batch_results(batch.id, docs, {"bundles": []}, db_session)
    db_session.commit()

    for d in docs:
        db_session.refresh(d)

    # Own and opposing must NOT be wired under the cover. Without any claimable
    # sibling the fallback bails out entirely — cover stays unpromoted.
    assert own_doc.parent_id is None
    assert opposing_doc.parent_id is None
    assert own_doc.role != DocumentRole.ENCLOSURE
    assert opposing_doc.role != DocumentRole.ENCLOSURE


# ---------------------------------------------------------------------------
# Gap #3 — completion sweep
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_completion_sweep_claims_same_proceeding_court_siblings_only(db_session):
    """Completion sweep: an already-promoted COVER_LETTER with proceeding_id
    claims same-proceeding court siblings but skips other-proceeding docs and
    own/opposing/third_party docs."""
    case, proc_x = _make_case_with_proceeding(db_session, case_id="TEST-SWEEP-X")
    proc_y = Proceeding(
        case_id=case.id,
        court_level=ProceedingCourtLevel.LG,
        court_name="LG Hamburg",
        az_court="999 O 7/24",
        status=ProceedingStatus.ACTIVE,
        started_at=datetime(2026, 1, 1),
    )
    db_session.add(proc_y)
    db_session.flush()

    batch = _make_batch(db_session, case.id)

    # Pre-promoted cover — the sweep / proceeding-grouping fallback wires
    # siblings under it. Content must be > 10 chars to survive the
    # _pick_cover_letter_candidate health filter so this doc wins the cover
    # pick (rather than the originator-guarded opposing doc).
    cover = Document(
        title="Begleitschreiben AG",
        content="kurze Begleitschreiben Notiz, im Auftrag des Gerichts.",
        case_id=case.id,
        proceeding_id=proc_x.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
        role=DocumentRole.COVER_LETTER,
    )
    # Same proc, court → SHOULD be swept under cover.
    sweepable = Document(
        title="Beschluss AG",
        content="Beschluss Text " * 20,
        case_id=case.id,
        proceeding_id=proc_x.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    # Same proc, OPPOSING → originator guard blocks.
    guarded = Document(
        title="Schriftsatz Gegenseite",
        content="Schriftsatz Text " * 20,
        case_id=case.id,
        proceeding_id=proc_x.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.OPPOSING,
    )
    # Different proceeding → proceeding guard blocks.
    other_proc = Document(
        title="Beschluss LG",
        content="LG Beschluss Text " * 20,
        case_id=case.id,
        proceeding_id=proc_y.id,
        ingest_batch_id=batch.id,
        originator_type=OriginatorType.COURT,
    )
    db_session.add_all([cover, sweepable, guarded, other_proc])
    db_session.commit()

    docs = [cover, sweepable, guarded, other_proc]

    # Empty bundles + the proceeding-grouping fallback won't fire because the
    # cover already carries role=COVER_LETTER (would short-circuit at the
    # cover-letter-IDs filter) — but more importantly because there'd be no
    # other eligible cover candidate distinct from the existing cover. So the
    # completion-sweep block is the one we're exercising.
    _apply_batch_results(batch.id, docs, {"bundles": []}, db_session)
    db_session.commit()

    for d in docs:
        db_session.refresh(d)

    # The same-proc COURT sibling gets claimed.
    assert sweepable.role == DocumentRole.ENCLOSURE
    assert sweepable.parent_id == cover.id

    # OPPOSING and other-proceeding docs are NOT claimed.
    assert guarded.parent_id is None
    assert guarded.role != DocumentRole.ENCLOSURE
    assert other_proc.parent_id is None
    assert other_proc.role != DocumentRole.ENCLOSURE
