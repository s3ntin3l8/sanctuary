"""ingest_bytes / upload_bytes_to_case: the byte-oriented twins of the upload path."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.models.database import Document, IngestBatch, User
from app.services.ingestion.service import ingest_bytes, upload_bytes_to_case

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_bytes_creates_a_document(db_session):
    doc = await ingest_bytes("letter.pdf", PDF, db=db_session)
    assert doc.case_id == "_TRIAGE"
    assert doc.original_filename == "letter.pdf"
    assert db_session.get(Document, doc.id) is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_bytes_rejects_bad_extension_and_duplicates(db_session):
    with pytest.raises(HTTPException) as bad:
        await ingest_bytes("evil.exe", PDF, db=db_session)
    assert bad.value.status_code == 400
    owner = db_session.query(User).first().id
    await ingest_bytes("a.pdf", PDF, db=db_session, owner_id=owner)
    with pytest.raises(HTTPException) as dup:
        await ingest_bytes("b.pdf", PDF, db=db_session, owner_id=owner)
    assert dup.value.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upload_bytes_to_case_queues_and_cleans_up(db_session):
    user = db_session.query(User).first()
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        doc, batch_id = await upload_bytes_to_case(
            db_session, filename="letter.pdf", content=PDF, owner_id=user.id
        )
    dispatch.assert_called_once()
    assert doc.ingest_batch_id == batch_id

    # A rejected file leaves no empty batch behind.
    before = db_session.query(IngestBatch).count()
    with pytest.raises(HTTPException):
        await upload_bytes_to_case(
            db_session, filename="bad.exe", content=PDF, owner_id=user.id
        )
    assert db_session.query(IngestBatch).count() == before
