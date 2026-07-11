"""Unit tests for worker_control.apply_ai_concurrency/apply_ocr_concurrency
(live pool resize).

celery_app.control is fully mocked — these assert the inspect → delta →
pool_grow/pool_shrink logic and that each function targets only its own
node prefix (ai@ vs ingest@).
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.worker_control import apply_ai_concurrency, apply_ocr_concurrency


def _mock_celery(stats):
    """Build a mock celery_app whose control.inspect().stats() returns `stats`."""
    celery = MagicMock()
    celery.control.inspect.return_value.stats.return_value = stats
    return celery


@pytest.mark.unit
def test_no_worker_reports_not_live():
    """stats() is None (no worker answered) → live False, no resize calls."""
    celery = _mock_celery(None)
    with patch("app.tasks.celery_app.celery_app", celery):
        res = apply_ai_concurrency(4)
    assert res == {"live": False, "nodes": []}
    celery.control.pool_grow.assert_not_called()
    celery.control.pool_shrink.assert_not_called()


@pytest.mark.unit
def test_grow_when_target_above_current():
    celery = _mock_celery({"ai@host": {"pool": {"max-concurrency": 2}}})
    with patch("app.tasks.celery_app.celery_app", celery):
        res = apply_ai_concurrency(5)
    assert res["live"] is True
    assert res["nodes"] == [{"node": "ai@host", "from": 2, "to": 5}]
    celery.control.pool_grow.assert_called_once_with(3, destination=["ai@host"])
    celery.control.pool_shrink.assert_not_called()


@pytest.mark.unit
def test_shrink_when_target_below_current():
    celery = _mock_celery({"ai@host": {"pool": {"max-concurrency": 6}}})
    with patch("app.tasks.celery_app.celery_app", celery):
        apply_ai_concurrency(2)
    celery.control.pool_shrink.assert_called_once_with(4, destination=["ai@host"])
    celery.control.pool_grow.assert_not_called()


@pytest.mark.unit
def test_noop_when_target_equals_current_still_live():
    """delta == 0 → reported live (worker present) but no grow/shrink call."""
    celery = _mock_celery({"ai@host": {"pool": {"max-concurrency": 3}}})
    with patch("app.tasks.celery_app.celery_app", celery):
        res = apply_ai_concurrency(3)
    assert res["live"] is True
    assert res["nodes"] == [{"node": "ai@host", "from": 3, "to": 3}]
    celery.control.pool_grow.assert_not_called()
    celery.control.pool_shrink.assert_not_called()


@pytest.mark.unit
def test_ignores_non_ai_nodes():
    """Only ai@ nodes are resized; the ingest worker is left untouched."""
    celery = _mock_celery(
        {
            "ingest@host": {"pool": {"max-concurrency": 1}},
            "ai@host": {"pool": {"max-concurrency": 2}},
        }
    )
    with patch("app.tasks.celery_app.celery_app", celery):
        res = apply_ai_concurrency(4)
    assert [n["node"] for n in res["nodes"]] == ["ai@host"]
    celery.control.pool_grow.assert_called_once_with(2, destination=["ai@host"])


# ── apply_ocr_concurrency (ingest@ nodes) ────────────────────────────────────


@pytest.mark.unit
def test_ocr_no_worker_reports_not_live():
    celery = _mock_celery(None)
    with patch("app.tasks.celery_app.celery_app", celery):
        res = apply_ocr_concurrency(4)
    assert res == {"live": False, "nodes": []}
    celery.control.pool_grow.assert_not_called()
    celery.control.pool_shrink.assert_not_called()


@pytest.mark.unit
def test_ocr_grow_when_target_above_current():
    celery = _mock_celery({"ingest@host": {"pool": {"max-concurrency": 1}}})
    with patch("app.tasks.celery_app.celery_app", celery):
        res = apply_ocr_concurrency(4)
    assert res["live"] is True
    assert res["nodes"] == [{"node": "ingest@host", "from": 1, "to": 4}]
    celery.control.pool_grow.assert_called_once_with(3, destination=["ingest@host"])
    celery.control.pool_shrink.assert_not_called()


@pytest.mark.unit
def test_ocr_shrink_when_target_below_current():
    celery = _mock_celery({"ingest@host": {"pool": {"max-concurrency": 6}}})
    with patch("app.tasks.celery_app.celery_app", celery):
        apply_ocr_concurrency(2)
    celery.control.pool_shrink.assert_called_once_with(4, destination=["ingest@host"])
    celery.control.pool_grow.assert_not_called()


@pytest.mark.unit
def test_ocr_ignores_non_ingest_nodes():
    """Only ingest@ nodes are resized; the ai worker is left untouched."""
    celery = _mock_celery(
        {
            "ai@host": {"pool": {"max-concurrency": 2}},
            "ingest@host": {"pool": {"max-concurrency": 1}},
        }
    )
    with patch("app.tasks.celery_app.celery_app", celery):
        res = apply_ocr_concurrency(4)
    assert [n["node"] for n in res["nodes"]] == ["ingest@host"]
    celery.control.pool_grow.assert_called_once_with(3, destination=["ingest@host"])
