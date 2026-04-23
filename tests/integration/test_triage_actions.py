"""Integration tests for triage document actions."""

from unittest.mock import patch

import pytest

from app.models.database import Document


@pytest.mark.integration
def test_retry_ai_action(app_client, db_session):
    # 1. Setup a doc in failed state
    doc = Document(
        title="Failed AI Doc",
        ai_summary={"error": "Ollama offline"},
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    # 2. Mock process_document_task.delay
    with patch(
        "app.tasks.document_processing.process_document_task.delay"
    ) as mock_delay:
        # 3. Call the retry-ai endpoint
        response = app_client.post(f"/triage/document/{doc.id}/retry-ai")

        assert response.status_code == 200
        # Check if the task was queued
        mock_delay.assert_called_once_with(doc.id)
