"""Regression tests: ingest_file() must not leak partial files on failure."""

import io
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile

from app.services.ingestion import service as ingestion_service
from app.services.ingestion.service import ingest_file


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
async def test_oversize_upload_413_does_not_leak_partial_file(db_session):
    """Regression: the 413 branch previously re-raised via a bare
    `except HTTPException: raise` with no cleanup, leaving the partially
    written file behind — unlike every other rejection path in ingest_file,
    which explicitly os.remove()s before raising."""
    filename = "oversize_regression_probe.pdf"
    file = UploadFile(filename=filename, file=io.BytesIO(b"%PDF-1.4 " + b"x" * 100))

    # Read ingestion_service.DATA_DIR dynamically (not `from app.config import
    # DATA_DIR` at module scope) — the session-scoped isolate_data_dir fixture
    # monkeypatches app.services.ingestion.service.DATA_DIR at test-setup
    # time, *after* this test module was already collected/imported, so a
    # module-level import here would silently bind to the real ./data dir
    # instead of the session tmp dir ingest_file actually writes to.
    case_dir = ingestion_service.DATA_DIR / "_TRIAGE"
    case_dir.mkdir(parents=True, exist_ok=True)
    before = set(case_dir.glob(f"*{filename}"))

    with (
        patch("app.services.ingestion.service.MAX_FILE_SIZE", 10),
        patch(
            "app.services.ingestion.service.aiofiles.open",
            side_effect=lambda path, mode: _AsyncFile(path, mode),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await ingest_file(file, db=db_session, skip_processing=True)

    assert exc.value.status_code == 413
    after = set(case_dir.glob(f"*{filename}"))
    assert after == before, f"partial file leaked: {after - before}"
