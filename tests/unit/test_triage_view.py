"""Unit tests for triage_view — sub-bundle aggregation, mock_status, stats."""

from datetime import datetime
from unittest.mock import MagicMock

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
    _pick_lead_doc,
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


# ---------------------------------------------------------------------------
# _pick_lead_doc
# ---------------------------------------------------------------------------


def _make_mock_doc(
    id=1,
    title="Doc",
    role=DocumentRole.STANDALONE,
    significance_tier=None,
    sub_group_id=None,
    sub_group_sort_order=0,
    sub_group=None,
    parent_id=None,
    ingest_batch_id=1,
    case_id=None,
):
    d = MagicMock()
    d.id = id
    d.title = title
    d.role = role
    d.significance_tier = significance_tier
    d.sub_group_id = sub_group_id
    d.sub_group_sort_order = sub_group_sort_order
    d.sub_group = sub_group
    d.parent_id = parent_id
    d.ingest_batch_id = ingest_batch_id
    d.case_id = case_id
    d.extraction_confidence = {}
    return d


@pytest.mark.unit
def test_pick_lead_doc_empty_returns_none():
    from app.services.triage_view import _pick_lead_doc

    assert _pick_lead_doc([]) is None


@pytest.mark.unit
def test_pick_lead_doc_cover_letter_wins():
    cover = _make_mock_doc(id=2, role=DocumentRole.COVER_LETTER)
    other = _make_mock_doc(id=1, role=DocumentRole.STANDALONE)
    result = _pick_lead_doc([(0, other), (0, cover)])
    assert result.id == 2


# ---------------------------------------------------------------------------
# build_sub_bundles — manual mode (BatchSubGroup)
# Helpers: make_doc, make_sg, FakeBundle
# ---------------------------------------------------------------------------


def make_sg(id, sort_order=0, label=None):
    """Create a minimal BatchSubGroup-like object for testing."""
    sg = MagicMock()
    sg.id = id
    sg.sort_order = sort_order
    sg.label = label
    return sg


def make_doc(
    id=1,
    role=DocumentRole.STANDALONE,
    sub_group_id=None,
    sub_group_sort_order=0,
    sub_group=None,
    case_id=None,
    extraction_confidence=None,
):
    """Create a minimal Document-like mock for manual-mode tests."""
    d = MagicMock()
    d.id = id
    d.title = f"Doc {id}"
    d.role = role
    d.significance_tier = None
    d.sub_group_id = sub_group_id
    d.sub_group_sort_order = sub_group_sort_order
    d.sub_group = sub_group
    d.parent_id = None
    d.ingest_batch_id = 1
    d.case_id = case_id
    d.extraction_confidence = extraction_confidence or {}
    return d


from dataclasses import dataclass


@dataclass
class FakeBundle:
    """Minimal BundleView-like object for manual-mode unit tests."""

    batch_id: int | None
    documents: list
    key: str = "batch-1"
    parent_groups: list | None = None
    suggested_case_id: str | None = None
    suggested_case_title: str | None = None
    confirmed_case_id: str | None = None


@pytest.mark.unit
def test_build_sub_bundles_manual_mode_groups_by_sub_group_id():
    """Manual mode: docs grouped by sub_group_id, ordered by BatchSubGroup.sort_order."""
    from app.services.triage_view import build_sub_bundles

    sg1 = make_sg(id=1, sort_order=0)
    sg2 = make_sg(id=2, sort_order=1)
    docs = [
        make_doc(id=10, sub_group_id=2, sub_group_sort_order=0, sub_group=sg2),
        make_doc(id=11, sub_group_id=1, sub_group_sort_order=0, sub_group=sg1),
        make_doc(id=12, sub_group_id=1, sub_group_sort_order=1, sub_group=sg1),
    ]
    bundle = FakeBundle(batch_id=1, documents=docs)
    result = build_sub_bundles(bundle)
    assert len(result) == 2
    assert result[0].sub_group_id == 1  # sg1 sorts first (sort_order=0)
    assert result[1].sub_group_id == 2
    assert len(result[0].docs) == 2


@pytest.mark.unit
def test_build_sub_bundles_manual_explicit_label_used():
    from app.services.triage_view import build_sub_bundles

    sg = make_sg(id=1, sort_order=0, label="My Custom Group")
    doc = make_doc(id=10, sub_group_id=1, sub_group=sg)
    bundle = FakeBundle(batch_id=1, documents=[doc])
    result = build_sub_bundles(bundle)
    assert result[0].label == "My Custom Group"


@pytest.mark.unit
def test_build_sub_bundles_manual_cover_letter_is_lead():
    from app.services.triage_view import build_sub_bundles

    sg = make_sg(id=1, sort_order=0)
    docs = [
        make_doc(id=10, sub_group_id=1, role=DocumentRole.STANDALONE, sub_group=sg),
        make_doc(id=11, sub_group_id=1, role=DocumentRole.COVER_LETTER, sub_group=sg),
    ]
    bundle = FakeBundle(batch_id=1, documents=docs)
    result = build_sub_bundles(bundle)
    assert result[0].lead_doc.id == 11


@pytest.mark.unit
def test_build_sub_bundles_ungrouped_docs_go_to_first_group():
    """Docs without sub_group_id when manual mode is active go into first group."""
    from app.services.triage_view import build_sub_bundles

    sg = make_sg(id=1, sort_order=0)
    docs = [
        make_doc(id=10, sub_group_id=1, sub_group=sg),
        make_doc(id=11, sub_group_id=None, sub_group=None),  # orphan
    ]
    bundle = FakeBundle(batch_id=1, documents=docs)
    result = build_sub_bundles(bundle)
    assert len(result) == 1
    doc_ids = [d.id for _, d in result[0].docs]
    assert 11 in doc_ids


@pytest.mark.unit
def test_build_sub_bundles_auto_mode_no_sub_group_ids():
    """Auto mode used when no docs have sub_group_id set."""
    from app.services.triage_view import build_sub_bundles

    docs = [make_doc(id=10, sub_group_id=None), make_doc(id=11, sub_group_id=None)]
    bundle = FakeBundle(
        batch_id=1,
        documents=docs,
        parent_groups=[[(0, docs[0])], [(0, docs[1])]],
    )
    result = build_sub_bundles(bundle)
    assert len(result) == 2
