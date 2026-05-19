"""Tests for CaseService._batch_card_context.

Recently rewritten to use SQLAlchemy window functions (ROW_NUMBER() OVER
PARTITION BY case_id) so the case-directory render doesn't pull every
Document just to keep the first row per case. SQLite 3.25+ supports the
window functions and the repo runs 3.45, but no test verifies the
ordering / partitioning correctness.
"""

from datetime import datetime

import pytest

from app.models.database import Case, Document
from app.models.enums import (
    CaseStatus,
    Jurisdiction,
    OriginatorType,
    SignificanceTier,
)
from app.services.case_service import CaseService


def _seed_case(db_session, case_id: str) -> Case:
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.flush()
    return case


def _doc(db_session, case_id: str, *, ingest_day: int, sig: SignificanceTier | None):
    doc = Document(
        title=f"{case_id}-day{ingest_day}-{sig}",
        case_id=case_id,
        ingest_date=datetime(2026, 4, ingest_day),
        originator_type=OriginatorType.COURT,
        significance_tier=sig,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


@pytest.mark.unit
def test_batch_card_context_picks_last_doc_per_case(db_session):
    """last_doc_by_case[cid] is the case's most-recent doc by ingest_date,
    independent of insertion order or other cases' documents."""
    _seed_case(db_session, "BCC-A")
    _seed_case(db_session, "BCC-B")
    _seed_case(db_session, "BCC-C")

    # Cross-case interleaving so the partition has to be honored.
    a_old = _doc(db_session, "BCC-A", ingest_day=1, sig=SignificanceTier.ADMINISTRATIVE)
    b_mid = _doc(db_session, "BCC-B", ingest_day=5, sig=SignificanceTier.INFORMATIONAL)
    a_new = _doc(db_session, "BCC-A", ingest_day=10, sig=SignificanceTier.SIGNIFICANT)
    b_old = _doc(db_session, "BCC-B", ingest_day=2, sig=SignificanceTier.ADMINISTRATIVE)
    c_only = _doc(db_session, "BCC-C", ingest_day=7, sig=SignificanceTier.CRITICAL)
    db_session.commit()

    _, last_by_case, _ = CaseService(db_session)._batch_card_context(
        ["BCC-A", "BCC-B", "BCC-C"]
    )

    assert last_by_case["BCC-A"].id == a_new.id
    assert last_by_case["BCC-B"].id == b_mid.id
    assert last_by_case["BCC-C"].id == c_only.id

    # Sanity: older docs are excluded from the per-case "last" map.
    assert last_by_case["BCC-A"].id != a_old.id
    assert last_by_case["BCC-B"].id != b_old.id


@pytest.mark.unit
def test_batch_card_context_max_sig_is_strongest_in_window(db_session):
    """max_sig_by_case[cid] is the highest-rank tier among the case's docs
    (within the 20-row window)."""
    _seed_case(db_session, "BCC-D")

    _doc(db_session, "BCC-D", ingest_day=1, sig=SignificanceTier.ADMINISTRATIVE)
    _doc(db_session, "BCC-D", ingest_day=2, sig=SignificanceTier.INFORMATIONAL)
    _doc(db_session, "BCC-D", ingest_day=3, sig=SignificanceTier.SIGNIFICANT)
    db_session.commit()

    _, _, max_sig = CaseService(db_session)._batch_card_context(["BCC-D"])

    assert max_sig["BCC-D"] == SignificanceTier.SIGNIFICANT


@pytest.mark.unit
def test_batch_card_context_sig_window_caps_at_20_most_recent(db_session):
    """The sig_subq filters to row_num <= 20, so a CRITICAL tier on a 21st
    (older) doc must NOT bubble up to max_sig_by_case."""
    _seed_case(db_session, "BCC-E")

    # Day 100 (oldest) has CRITICAL — must be excluded by the 20-row cap.
    _doc(db_session, "BCC-E", ingest_day=1, sig=SignificanceTier.CRITICAL)

    # 25 newer docs with weaker tiers. ingest_day=2..26.
    for day in range(2, 27):
        _doc(db_session, "BCC-E", ingest_day=day, sig=SignificanceTier.INFORMATIONAL)
    db_session.commit()

    _, _, max_sig = CaseService(db_session)._batch_card_context(["BCC-E"])

    # The 20 most-recent docs are days 7..26, all INFORMATIONAL. Day 1's CRITICAL
    # falls outside the window and must not leak in.
    assert max_sig["BCC-E"] == SignificanceTier.INFORMATIONAL


@pytest.mark.unit
def test_batch_card_context_empty_case_list_returns_empty_dicts(db_session):
    """Defensive: empty case_ids → empty dicts, no DB query."""
    actions, last, max_sig = CaseService(db_session)._batch_card_context([])
    assert actions == {} and last == {} and max_sig == {}


@pytest.mark.unit
def test_batch_card_context_case_with_no_docs_is_absent_from_dicts(db_session):
    """A case_id with no Document rows → no entry in last/max dicts (not None)."""
    _seed_case(db_session, "BCC-EMPTY")

    _, last_by_case, max_sig = CaseService(db_session)._batch_card_context(
        ["BCC-EMPTY"]
    )

    assert "BCC-EMPTY" not in last_by_case
    assert "BCC-EMPTY" not in max_sig
