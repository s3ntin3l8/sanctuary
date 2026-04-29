"""Regression test: ingest_file() must reject case_id values that would
escape DATA_DIR via path traversal."""

import io

import pytest
from fastapi import HTTPException, UploadFile

from app.services.ingestion.service import ingest_file


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_file_rejects_path_traversal_case_id(db_session):
    file = UploadFile(filename="payload.pdf", file=io.BytesIO(b"%PDF-1.4 dummy"))

    with pytest.raises(HTTPException) as exc:
        await ingest_file(file, case_id="../../../tmp/pwn", db=db_session)

    assert exc.value.status_code == 400
    assert "case" in exc.value.detail.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_file_rejects_absolute_path_case_id(db_session):
    file = UploadFile(filename="payload.pdf", file=io.BytesIO(b"%PDF-1.4 dummy"))

    with pytest.raises(HTTPException) as exc:
        await ingest_file(file, case_id="/etc/pwn", db=db_session)

    assert exc.value.status_code == 400
