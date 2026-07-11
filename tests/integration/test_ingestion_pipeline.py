from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import Case, CaseStatus, Document
from app.tasks.document_processing import metadata_task, process_document_task

client = TestClient(app)


@pytest.mark.integration
def test_full_ingestion_pipeline(db_session, test_engine):
    # 1. Setup - Create a case
    # Note: The extraction logic in ingest_file prefers extracted case IDs over provided ones
    case = Case(id="ADV-123-K", title="Pipeline Test Case", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()

    # 2. Mocks
    mock_markdown = (
        "# Test Document\n\n"
        + "This is a legal document about ADV-123-K. " * 5
        + "\nAmount: 500 EUR."
    )
    mock_summary = {
        "legal_significance": "Significant",
        "required_action": "Reply within 2 weeks",
        "financial_impact": "500 EUR",
    }

    def mock_sum_sync_impl(doc_id, db):
        doc = db.get(Document, doc_id)
        doc.ai_summary = mock_summary
        db.commit()

    async def mock_emb_async_impl(doc_id):
        pass  # chunk embeddings stored in document_chunk_vectors; mocked out here

    mock_res = {"content": mock_markdown, "metadata": {"pages": 1}, "chunks": []}

    with (
        patch("app.services.ingestion.converters.convert_file", return_value=mock_res),
        patch("app.services.ingestion.service.convert_file", return_value=mock_res),
        patch(
            "app.services.ingestion.service.is_valid_docling_output", return_value=True
        ),
        patch(
            "app.services.ai_summary._summarize_document_sync",
            side_effect=mock_sum_sync_impl,
        ),
        patch(
            "app.services.embeddings.generate_embedding",
            side_effect=mock_emb_async_impl,
        ),
        # process_document_task now dispatches metadata_task on the ai queue
        # instead of running METADATA inline. No-op the dispatch so we drive
        # metadata_task ourselves below, keeping the test free of Redis.
        patch("app.tasks.document_processing.metadata_task.delay"),
    ):
        # 3. Upload Document
        file_content = b"PDF dummy content"
        response = client.post(
            "/upload",
            files=[("files", ("test.pdf", file_content, "application/pdf"))],
            data={"case_id": "ADV-123-K"},
            headers={"hx-request": "true"},
        )
        assert response.status_code == 200
        assert "queued" in response.text

        # 4. Get the created document
        doc = db_session.query(Document).filter(Document.case_id == "ADV-123-K").first()
        assert doc is not None

        # 5. Manually run the background tasks (to ensure they run exactly as we expect).
        # Pipeline is now split: process_document_task → EXTRACT only,
        # metadata_task → METADATA + downstream fan-out.
        process_document_task(doc.id)
        metadata_task(doc.id)

        # 6. Verify Results
        db_session.refresh(doc)
        assert doc.content == mock_markdown
        assert doc.ai_summary == mock_summary
        # Embedding storage is mocked; verified separately in test_embeddings.py

        # Verify extractions
        assert doc.cost_candidates is not None
        assert len(doc.cost_candidates) > 0
        assert doc.cost_candidates[0]["value"] == 500.0
