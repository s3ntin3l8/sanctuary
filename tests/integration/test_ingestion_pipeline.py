import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.documents import process_document_background
from app.main import app
from app.models.database import Case, CaseStatus, Document, IngestStatus

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
        "legal_significance": "High",
        "required_action": "Review",
        "financial_impact": "500 EUR",
    }
    mock_embedding = [0.1] * 768

    def mock_sum_sync_impl(doc_id, db):
        doc = db.get(Document, doc_id)
        doc.ai_summary = mock_summary
        doc.ai_summary_status = "generated"
        db.commit()

    async def mock_emb_async_impl(doc_id):
        # We need to use the test engine to update the doc
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=test_engine)
        db = Session()
        try:
            doc = db.get(Document, doc_id)
            doc.content_embedding = json.dumps(mock_embedding)
            db.commit()
        finally:
            db.close()

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
    ):
        # 3. Upload Document
        file_content = b"PDF dummy content"
        response = client.post(
            "/api/v1/upload",
            files=[("files", ("test.pdf", file_content, "application/pdf"))],
            data={"case_id": "PIPE-001"},
            headers={"hx-request": "true"},
        )
        assert response.status_code == 200
        assert "queued" in response.text

        # 4. Get the created document
        doc = db_session.query(Document).filter(Document.case_id == "ADV-123-K").first()
        assert doc is not None
        # TestClient runs background tasks synchronously, so it might already be COMPLETED
        # or still in a transition state depending on the mock behavior.
        # Let's just ensure we have it.

        # 5. Manually run the background task (to ensure it runs exactly as we expect)
        # In a real app, this is triggered via background_tasks.add_task
        process_document_background(doc.id, db_session)

        # 6. Verify Results
        db_session.refresh(doc)
        assert doc.ingest_status == IngestStatus.COMPLETED
        assert doc.content == mock_markdown
        assert doc.ai_summary == mock_summary
        assert doc.ai_summary_status == "generated"
        assert doc.content_embedding is not None
        assert json.loads(doc.content_embedding) == mock_embedding

        # Verify extractions
        assert doc.cost_candidates is not None
        assert len(doc.cost_candidates) > 0
        assert doc.cost_candidates[0]["value"] == 500.0
