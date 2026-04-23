from unittest.mock import patch

import pytest

from app.tasks.document_processing import (
    process_document_task,
    reingest_all_documents_task,
)


@pytest.mark.unit
def test_process_document_task_success(db_session, sample_document):
    with (
        patch("app.tasks.document_processing.get_db_session") as mock_get_db_session,
        patch("app.services.ingestion.process_uploaded_document") as mock_process_doc,
        patch("app.tasks.document_processing._run_phase1_summary"),
        patch("app.tasks.enrich_document.enrich_document_task.delay"),
        patch("app.tasks.generate_embedding.generate_embedding_task.delay"),
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db_session.return_value = db_session

        result = process_document_task.run(sample_document.id)

        assert result["status"] == "success"
        mock_process_doc.assert_called_once()


@pytest.mark.unit
def test_reingest_all_documents_task(db_session, sample_document):
    with (
        patch("app.tasks.document_processing.get_db_session") as mock_get_db_session,
        patch(
            "app.tasks.document_processing.process_document_task.delay"
        ) as mock_delay,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_get_db_session.return_value = db_session

        result = reingest_all_documents_task.run(case_id=sample_document.case_id)

        assert result["status"] == "queued"
        assert result["count"] == 1
        mock_delay.assert_called_once_with(sample_document.id)
