"""Unit tests for attention scoring (home dashboard prioritization)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.database import ActionItem, Document, IngestBatch
from app.models.enums import ActionItemType, SignificanceTier
from app.services.attention_scoring import score_action_item, score_triage_batch


def _item(
    *, days_from_now=None, action_type=ActionItemType.RESPONSE_REQUIRED, doc=None
):
    due = (
        None
        if days_from_now is None
        else datetime.now(UTC) + timedelta(days=days_from_now)
    )
    item = ActionItem(due_date=due, action_type=action_type)
    item.source_document = doc
    return item


@pytest.mark.unit
def test_no_due_date_scores_zero():
    assert score_action_item(_item(days_from_now=None)) == 0


@pytest.mark.unit
def test_overdue_is_capped_at_100_over_baseline():
    # 200 days overdue → min(abs(days), 100) == 100 → 1100 (robust to ±1 day rounding).
    assert score_action_item(_item(days_from_now=-200)) == 1100


@pytest.mark.unit
def test_overdue_scores_above_baseline():
    assert score_action_item(_item(days_from_now=-3)) > 1000


@pytest.mark.unit
def test_due_today_scores_900():
    # +1 hour → delta.days == 0 → "today" band.
    item = _item()
    item.due_date = datetime.now(UTC) + timedelta(hours=1)
    assert score_action_item(item) == 900


@pytest.mark.unit
@pytest.mark.parametrize(
    "days,expected",
    [
        (1.5, 800),  # tomorrow
        (5, 700),  # within a week
        (11, 600),  # within two weeks
        (20, 500),  # within a month
        (60, 100),  # beyond a month
    ],
)
def test_future_bands(days, expected):
    assert score_action_item(_item(days_from_now=days)) == expected


@pytest.mark.unit
def test_deadline_type_boost():
    assert (
        score_action_item(_item(days_from_now=5, action_type=ActionItemType.DEADLINE))
        == 750
    )


@pytest.mark.unit
def test_court_date_type_boost():
    assert (
        score_action_item(_item(days_from_now=5, action_type=ActionItemType.COURT_DATE))
        == 730
    )


@pytest.mark.unit
def test_critical_source_document_boost():
    doc = Document(title="crit", significance_tier=SignificanceTier.CRITICAL)
    assert score_action_item(_item(days_from_now=5, doc=doc)) == 720


def _batch(*, age_days, doc_count=0, case_id=None):
    batch = IngestBatch(
        received_at=datetime.now(UTC) - timedelta(days=age_days, hours=12),
        case_id=case_id,
    )
    batch.documents = [Document(title=f"d{i}") for i in range(doc_count)]
    return batch


@pytest.mark.unit
def test_batch_age_drives_score():
    # 3.5 days old → age_days == 3 → 30; case_id set so no uncategorized boost.
    assert score_triage_batch(_batch(age_days=3, case_id="ADV-024-A")) == 30


@pytest.mark.unit
def test_batch_uncategorized_boost():
    assert score_triage_batch(_batch(age_days=3, case_id=None)) == 80


@pytest.mark.unit
def test_batch_document_count_boost():
    # 30 (age) + 50 (no case_id) + 2*5 (docs) == 90.
    assert score_triage_batch(_batch(age_days=3, doc_count=2)) == 90
