"""Unit tests for triage_view — sub-bundle aggregation, mock_status, stats."""

from datetime import datetime

import pytest

from app.models.database import Case, Document
from app.models.enums import (
    CaseStatus,
    DocumentRole,
    IngestBatchSourceType,
    PipelineState,
    SignificanceTier,
)
from app.services.triage_service import BundleView
from app.services.triage_view import (
    STATUS_NEEDS_CLASSIFICATION,
    STATUS_NEEDS_REVIEW,
    STATUS_PROCESSING,
    STATUS_STUCK,
    build_sub_bundles,
    mock_status,
    stats_for_chips,
)


def _bundle(docs: list[Document], **overrides) -> BundleView:
    defaults = {
        "key": "batch-1",
        "batch_id": 1,
        "source_type": IngestBatchSourceType.EMAIL,
        "subject": "Test bundle",
        "sender_email": "lawyer@example.com",
        "received_at": datetime(2026, 4, 14, 11, 22),
        "documents": docs,
    }
    defaults.update(overrides)
    return BundleView(**defaults)


def _make_doc(
    db_session,
    *,
    title="Doc",
    role=DocumentRole.STANDALONE,
    significance=SignificanceTier.INFORMATIONAL,
    pipeline_state=None,
    case_id=None,
    parent_id=None,
    extraction_confidence=None,
) -> Document:
    doc = Document(
        title=title,
        role=role,
        significance_tier=significance,
        pipeline_state=pipeline_state,
        case_id=case_id,
        parent_id=parent_id,
        extraction_confidence=extraction_confidence,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


# ---------------------------------------------------------------------------
# build_sub_bundles
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_sub_bundles_single_root_returns_one(db_session):
    cover = _make_doc(db_session, title="Schriftsatz", role=DocumentRole.COVER_LETTER)
    enc = _make_doc(
        db_session, title="Anlage K7", role=DocumentRole.ENCLOSURE, parent_id=cover.id
    )
    bundle = _bundle([cover, enc])

    sub_bundles = build_sub_bundles(bundle)

    assert len(sub_bundles) == 1
    assert sub_bundles[0].lead_doc.id == cover.id
    assert [d.id for _, d in sub_bundles[0].docs] == [cover.id, enc.id]


@pytest.mark.unit
def test_build_sub_bundles_two_roots_returns_two(db_session):
    root_a = _make_doc(db_session, title="Schriftsatz")
    root_b = _make_doc(db_session, title="Deckungszusage")
    bundle = _bundle([root_a, root_b])

    sub_bundles = build_sub_bundles(bundle)

    assert len(sub_bundles) == 2
    lead_ids = {sb.lead_doc.id for sb in sub_bundles}
    assert lead_ids == {root_a.id, root_b.id}


@pytest.mark.unit
def test_lead_doc_picks_cover_letter_when_present(db_session):
    """Cover-letter wins regardless of significance order."""
    annex = _make_doc(
        db_session,
        title="Anlage",
        role=DocumentRole.ENCLOSURE,
        significance=SignificanceTier.CRITICAL,
    )
    cover = _make_doc(
        db_session,
        title="Cover",
        role=DocumentRole.COVER_LETTER,
        significance=SignificanceTier.INFORMATIONAL,
    )
    cover.parent_id = None
    annex.parent_id = cover.id
    db_session.flush()
    bundle = _bundle([cover, annex])

    sub_bundles = build_sub_bundles(bundle)

    assert sub_bundles[0].lead_doc.id == cover.id


@pytest.mark.unit
def test_lead_doc_falls_back_to_significance_then_id(db_session):
    """Without a cover letter, the most-significant doc leads; ties break by id."""
    a = _make_doc(db_session, title="A", significance=SignificanceTier.INFORMATIONAL)
    b = _make_doc(db_session, title="B", significance=SignificanceTier.CRITICAL)
    bundle = _bundle([a, b])  # both root, no cover letter

    sub_bundles = build_sub_bundles(bundle)

    leads = sorted(sb.lead_doc.id for sb in sub_bundles)
    # Two root sub-bundles; each picks its sole doc.
    assert leads == sorted([a.id, b.id])


@pytest.mark.unit
def test_sub_bundle_carries_case_id_extraction_confidence(db_session):
    """field_confidence_case passes through from the lead doc's
    extraction_confidence['case_id']."""
    case = Case(id="ADV-024-A", title="Test", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.flush()
    lead = _make_doc(
        db_session,
        title="Lead",
        case_id="ADV-024-A",
        extraction_confidence={"case_id": "high", "sender": "medium"},
    )
    bundle = _bundle([lead])

    sub_bundles = build_sub_bundles(bundle)

    assert sub_bundles[0].field_confidence_case == "high"
    assert sub_bundles[0].suggested_case_id == "ADV-024-A"


@pytest.mark.unit
def test_build_sub_bundles_empty_bundle_returns_empty(db_session):
    bundle = _bundle([])
    assert build_sub_bundles(bundle) == []


# ---------------------------------------------------------------------------
# mock_status precedence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mock_status_failed_doc_returns_stuck(db_session):
    """FAILED dominates everything else."""
    failed = _make_doc(db_session, pipeline_state=PipelineState.FAILED)
    running = _make_doc(db_session, pipeline_state=PipelineState.RUNNING)
    bundle = _bundle([failed, running])
    assert mock_status(bundle) == STATUS_STUCK


@pytest.mark.unit
def test_mock_status_running_doc_returns_processing(db_session):
    running = _make_doc(db_session, pipeline_state=PipelineState.RUNNING)
    completed = _make_doc(db_session, pipeline_state=PipelineState.COMPLETED)
    bundle = _bundle([running, completed])
    assert mock_status(bundle) == STATUS_PROCESSING


@pytest.mark.unit
def test_mock_status_pending_doc_returns_processing(db_session):
    """PENDING also counts as processing."""
    pending = _make_doc(db_session, pipeline_state=PipelineState.PENDING)
    bundle = _bundle([pending])
    assert mock_status(bundle) == STATUS_PROCESSING


@pytest.mark.unit
def test_mock_status_no_case_returns_needs_classification(db_session):
    """No confirmed and no suggested case → needs_classification."""
    completed = _make_doc(db_session, pipeline_state=PipelineState.COMPLETED)
    bundle = _bundle([completed])
    assert mock_status(bundle) == STATUS_NEEDS_CLASSIFICATION


@pytest.mark.unit
def test_mock_status_with_suggested_case_returns_needs_review(db_session):
    db_session.add(Case(id="ADV-024-A", title="Test", status=CaseStatus.INTAKE))
    db_session.commit()

    completed = _make_doc(
        db_session,
        pipeline_state=PipelineState.COMPLETED,
        case_id="ADV-024-A",
    )
    bundle = _bundle(
        [completed],
        suggested_case_id="ADV-024-A",
        suggested_case_title="Musterklage",
    )
    assert mock_status(bundle) == STATUS_NEEDS_REVIEW


@pytest.mark.unit
def test_mock_status_failed_dominates_no_case(db_session):
    """A failing bundle without a case is still 'stuck', not 'needs_classification'."""
    failed = _make_doc(db_session, pipeline_state=PipelineState.FAILED)
    bundle = _bundle([failed])  # no case
    assert mock_status(bundle) == STATUS_STUCK


@pytest.mark.unit
def test_mock_status_partial_doc_returns_processing(db_session):
    """PARTIAL is the between-stage gap (some completed, others still pending) —
    must keep the row in 'processing' so the Confirm gate holds mid-pipeline."""
    db_session.add(Case(id="ADV-024-A", title="Test", status=CaseStatus.INTAKE))
    db_session.commit()

    partial = _make_doc(
        db_session,
        pipeline_state=PipelineState.PARTIAL,
        case_id="ADV-024-A",
    )
    bundle = _bundle(
        [partial],
        suggested_case_id="ADV-024-A",
        suggested_case_title="Musterklage",
    )
    # Without the PARTIAL guard this would fall through to needs_review and
    # the Confirm button would render despite enrich/relationships/claims/
    # entities/embeddings still being queued.
    assert mock_status(bundle) == STATUS_PROCESSING


@pytest.mark.unit
def test_mock_status_failed_dominates_partial(db_session):
    """FAILED still wins over PARTIAL — precedence preserved."""
    partial = _make_doc(db_session, pipeline_state=PipelineState.PARTIAL)
    failed = _make_doc(db_session, pipeline_state=PipelineState.FAILED)
    bundle = _bundle([partial, failed])
    assert mock_status(bundle) == STATUS_STUCK


# ---------------------------------------------------------------------------
# stats_for_chips
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stats_for_chips_counts_each_status(db_session):
    db_session.add(Case(id="ADV-001", title="Test", status=CaseStatus.INTAKE))
    db_session.commit()

    failed = _make_doc(db_session, pipeline_state=PipelineState.FAILED)
    running = _make_doc(db_session, pipeline_state=PipelineState.RUNNING)
    completed_no_case = _make_doc(db_session, pipeline_state=PipelineState.COMPLETED)
    completed_with_case = _make_doc(
        db_session, pipeline_state=PipelineState.COMPLETED, case_id="ADV-001"
    )

    bundles = [
        _bundle([failed], key="b1"),
        _bundle([running], key="b2"),
        _bundle([completed_no_case], key="b3"),
        _bundle(
            [completed_with_case],
            key="b4",
            suggested_case_id="ADV-001",
        ),
    ]
    stats = stats_for_chips(bundles)

    assert stats["stuck"] == 1
    assert stats["processing"] == 1
    assert stats["needs_classification"] == 1
    assert stats["needs_review"] == 1
    assert stats["pending"] == 4
    assert stats["completed_today"] == 0


@pytest.mark.unit
def test_stats_for_chips_empty_returns_zeros():
    stats = stats_for_chips([])
    assert stats == {
        "pending": 0,
        "completed_today": 0,
        "stuck": 0,
        "processing": 0,
        "needs_classification": 0,
        "needs_review": 0,
    }
