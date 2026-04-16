from unittest.mock import patch

import pytest

from app.models.database import Document, IngestStatus
from app.tasks.document_processing import (
    process_document_task,
    reingest_all_documents_task,
)


@pytest.mark.unit
def test_process_document_task_success(db_session, sample_document):
    with (
        patch("app.tasks.document_processing.SessionLocal") as mock_session_local,
        patch("app.services.ingestion.process_uploaded_document") as mock_process_doc,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_session_local.return_value = db_session

        # In some Celery versions/setups, .run is already bound or __wrapped__ behaves differently.
        # Let's try calling the function directly from the module if possible,
        # or just use .run and handle the arguments.

        # Try calling with ONE argument if it's already bound
        result = process_document_task.run(sample_document.id)

        assert result["status"] == "success"
        db_session.expire_all()
        doc = db_session.get(Document, sample_document.id)
        assert doc.ingest_status == IngestStatus.COMPLETED
        mock_process_doc.assert_called_once()


@pytest.mark.unit
def test_reingest_all_documents_task(db_session, sample_document):
    with (
        patch("app.tasks.document_processing.SessionLocal") as mock_session_local,
        patch(
            "app.tasks.document_processing.process_document_task.delay"
        ) as mock_delay,
        patch.object(db_session, "close", return_value=None),
    ):
        mock_session_local.return_value = db_session

        result = reingest_all_documents_task.run(case_id=sample_document.case_id)

        assert result["status"] == "queued"
        assert result["count"] == 1
        mock_delay.assert_called_once_with(sample_document.id)
