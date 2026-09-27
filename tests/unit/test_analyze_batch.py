"""Tests for analyze_batch.py's failure-exhaustion branches.

Before PR2 (2026-09-27 ingestion audit), the httpx.ReadTimeout- and
httpx.ConnectError-exhausted branches marked every doc's BATCH_ANALYSIS
failed but never called _enrich_if_pending — only the generic Exception
branch did. Every doc in the batch was left stuck at ENRICH=PENDING after
either kind of timeout.
"""

from unittest.mock import patch

import httpx
import pytest
from celery.exceptions import Retry

from app.tasks.analyze_batch import analyze_batch_task


@pytest.fixture
def batch_with_docs(db_session, sample_case):
    from app.models.database import Document, IngestBatch
    from app.models.enums import (
        IngestBatchSourceType,
        IngestBatchStatus,
        OriginatorType,
    )
    from app.services.pipeline_status import initialize

    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL, status=IngestBatchStatus.PROCESSING
    )
    db_session.add(batch)
    db_session.flush()

    docs = []
    for i in range(2):
        doc = Document(
            title=f"Doc {i}",
            content="x",
            case_id=sample_case.id,
            ingest_batch_id=batch.id,
            originator_type=OriginatorType.COURT,
        )
        db_session.add(doc)
        db_session.flush()
        initialize(doc, batched=True, db=db_session)
        docs.append(doc)
    db_session.commit()
    return batch, docs


@pytest.mark.unit
def test_analyze_batch_retries_once_on_timeout_then_enqueues_enrich(
    db_session, batch_with_docs
):
    batch, docs = batch_with_docs
    retry_sentinel = Retry(exc=httpx.ReadTimeout("simulated"))

    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.batch_analyzer.analyze",
            side_effect=httpx.ReadTimeout("simulated"),
        ),
        patch.object(
            analyze_batch_task, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        analyze_batch_task.request.update({"retries": 0})
        try:
            with pytest.raises(Retry):
                analyze_batch_task.run(batch.id)
        finally:
            analyze_batch_task.request.clear()
        mock_retry.assert_called_once()

        with patch(
            "app.tasks.analyze_batch._enrich_if_pending"
        ) as mock_enrich_if_pending:
            analyze_batch_task.request.update({"retries": 1})
            try:
                result = analyze_batch_task.run(batch.id)
            finally:
                analyze_batch_task.request.clear()

    assert result["status"] == "failed"
    assert mock_enrich_if_pending.call_count == len(docs)
    mock_enrich_if_pending.assert_any_call(docs[0].id)
    mock_enrich_if_pending.assert_any_call(docs[1].id)


@pytest.mark.unit
def test_analyze_batch_retries_connect_error_then_enqueues_enrich(
    db_session, batch_with_docs
):
    batch, docs = batch_with_docs
    retry_sentinel = Retry(exc=httpx.ConnectError("simulated"))

    with (
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close"),
        patch(
            "app.services.intelligence.batch_analyzer.analyze",
            side_effect=httpx.ConnectError("simulated"),
        ),
        patch.object(
            analyze_batch_task, "retry", side_effect=retry_sentinel
        ) as mock_retry,
    ):
        analyze_batch_task.request.update({"retries": 0})
        try:
            with pytest.raises(Retry):
                analyze_batch_task.run(batch.id)
        finally:
            analyze_batch_task.request.clear()
        mock_retry.assert_called_once()

        with patch(
            "app.tasks.analyze_batch._enrich_if_pending"
        ) as mock_enrich_if_pending:
            analyze_batch_task.request.update(
                {"retries": analyze_batch_task.max_retries}
            )
            try:
                result = analyze_batch_task.run(batch.id)
            finally:
                analyze_batch_task.request.clear()

    assert result["status"] == "failed"
    assert mock_enrich_if_pending.call_count == len(docs)
