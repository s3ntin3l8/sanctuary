"""Regression test: ingest_file() must reject case_id values that would
escape DATA_DIR via path traversal."""

import io
from tempfile import SpooledTemporaryFile
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile

from app.services.ingestion.service import ingest_file


def _upload(name: str, content: bytes) -> UploadFile:
    f = SpooledTemporaryFile()  # noqa: SIM115 - UploadFile owns the open handle
    f.write(content)
    f.seek(0)
    return UploadFile(file=f, filename=name)


class _AsyncFile:
    def __init__(self, path, mode):
        self._path = path
        self._mode = mode
        self._file = None

    async def __aenter__(self):
        self._file = open(self._path, self._mode)  # noqa: SIM115
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._file.close()

    async def write(self, content: bytes):
        return self._file.write(content)


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_file_preserves_existing_file_with_same_name(db_session):
    first = _upload("payload.pdf", b"%PDF-1.4 first")
    second = _upload("payload.pdf", b"%PDF-1.4 second")

    with patch(
        "app.services.ingestion.service.aiofiles.open",
        side_effect=lambda path, mode: _AsyncFile(path, mode),
    ):
        doc1 = await ingest_file(first, db=db_session, skip_processing=True)
        doc2 = await ingest_file(second, db=db_session, skip_processing=True)

    assert doc1.file_path != doc2.file_path
    with open(doc1.file_path, "rb") as f:
        assert f.read() == b"%PDF-1.4 first"
    with open(doc2.file_path, "rb") as f:
        assert f.read() == b"%PDF-1.4 second"
