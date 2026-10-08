"""Entry points that open their own session via a function-local SessionLocal.

These run against ``app.config.SessionLocal`` (repointed at the test DB by the
conftest fixture) — the point of the function-local import, see
``test_hygiene_guards.py``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
def test_ocr_concurrency_main_prints_and_applies_limit(db_session, capsys):
    from app.cli import ocr_concurrency

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
        patch.object(ocr_concurrency, "get_ocr_concurrency", return_value=3),
        patch.object(ocr_concurrency, "set_limit") as set_limit,
    ):
        ocr_concurrency.main()

    set_limit.assert_called_once_with(3)
    assert capsys.readouterr().out.strip() == "3"


@pytest.mark.unit
def test_worker_concurrency_main_prints_setting(db_session, capsys):
    from app.cli import worker_concurrency

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
        patch.object(worker_concurrency, "get_worker_concurrency", return_value=5),
    ):
        worker_concurrency.main()

    assert capsys.readouterr().out.strip() == "5"


@pytest.mark.unit
def test_thread_open_scan_task_reports_closed_count(db_session):
    from app.tasks.thread_open_scan import thread_open_scan_task

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.thread_open_scanner.scan_and_close_threads",
            return_value=2,
        ),
    ):
        result = thread_open_scan_task()

    assert result == {"status": "success", "closed": 2}


@pytest.mark.unit
def test_claim_dedup_task_success_and_failure(db_session):
    from app.tasks import claim_dedup

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
        patch.object(claim_dedup, "user_settings_service", MagicMock()),
        patch.object(
            claim_dedup,
            "find_duplicates_for_case",
            AsyncMock(return_value={"merged": 1}),
        ),
    ):
        assert claim_dedup.claim_dedup_task("ADV-1") == {"status": "done", "merged": 1}

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
        patch.object(claim_dedup, "user_settings_service", MagicMock()),
        patch.object(
            claim_dedup,
            "find_duplicates_for_case",
            AsyncMock(side_effect=ValueError("x")),
        ),
    ):
        assert claim_dedup.claim_dedup_task("ADV-1") == {
            "status": "failed",
            "error": "x",
        }


@pytest.mark.unit
def test_slicer_prepare_ignores_unknown_batch(db_session):
    from app.services.ingestion import slicer

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
    ):
        assert slicer.prepare(999_999) is None


@pytest.mark.unit
def test_entity_extract_reports_missing_document(db_session):
    from app.services.intelligence import entity_extractor

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
    ):
        assert entity_extractor.extract(999_999) == "document not found"


@pytest.mark.unit
def test_case_brief_generate_rejects_unknown_case(db_session):
    from app.services.intelligence import case_brief_generator

    with (
        patch("app.config.SessionLocal", lambda: db_session),
        patch.object(db_session, "close"),
        pytest.raises(ValueError, match="not found"),
    ):
        case_brief_generator.generate("NO-SUCH-CASE")
