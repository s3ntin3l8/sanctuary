"""Unit tests for the scan folder ingest driver."""

import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
def test_non_pdf_file_goes_to_failed(tmp_path):
    """Non-PDF files (jpg, heic, docx) must be moved to failed/ with an error log."""
    from app.services.ingestion.scan_folder import scan_and_ingest

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    for d in (incoming, processing, processed, failed):
        d.mkdir()

    (incoming / "photo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    with (
        patch("app.services.ingestion.scan_folder.SCAN_INCOMING_DIR", incoming),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSED_DIR", processed),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
    ):
        count = scan_and_ingest(MagicMock())

    assert count == 0
    failed_dirs = list(failed.iterdir())
    assert len(failed_dirs) == 1
    error_log = failed_dirs[0] / "error.log"
    assert error_log.exists()
    assert "only .pdf" in error_log.read_text().lower()


@pytest.mark.unit
def test_non_pdf_varieties_all_rejected(tmp_path):
    """All common non-PDF formats are rejected."""
    from app.services.ingestion.scan_folder import scan_and_ingest

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    for d in (incoming, processing, processed, failed):
        d.mkdir()

    for name in ("scan.heic", "doc.docx", "image.png", "sheet.xlsx"):
        (incoming / name).write_bytes(b"\x00" * 10)

    with (
        patch("app.services.ingestion.scan_folder.SCAN_INCOMING_DIR", incoming),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSED_DIR", processed),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
    ):
        count = scan_and_ingest(MagicMock())

    assert count == 0
    assert len(list(failed.iterdir())) == 4


@pytest.mark.unit
def test_dotfiles_and_temp_files_ignored(tmp_path):
    """Hidden files and temp-write suffixes are skipped."""
    from app.services.ingestion.scan_folder import scan_and_ingest

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    for d in (incoming, processing, processed, failed):
        d.mkdir()

    (incoming / ".DS_Store").write_bytes(b"\x00")
    (incoming / "upload.part").write_bytes(b"\x00")
    (incoming / "transfer.tmp").write_bytes(b"\x00")
    (incoming / "chrome.crdownload").write_bytes(b"\x00")

    with (
        patch("app.services.ingestion.scan_folder.SCAN_INCOMING_DIR", incoming),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSED_DIR", processed),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
    ):
        count = scan_and_ingest(MagicMock())

    # Nothing picked up — nothing in incoming remaining either (not moved to failed)
    assert count == 0
    assert not list(failed.iterdir())
    assert len(list(incoming.iterdir())) == 4  # untouched


@pytest.mark.unit
def test_mtime_guard_skips_recent_file(tmp_path):
    """Files with mtime < guard seconds are skipped (still being written)."""
    from app.services.ingestion.scan_folder import scan_and_ingest

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    for d in (incoming, processing, processed, failed):
        d.mkdir()

    pdf_file = incoming / "fresh.pdf"
    pdf_file.write_bytes(b"%PDF-1.4")
    # touch mtime to now — should be skipped
    now = time.time()
    import os

    os.utime(pdf_file, (now, now))

    with (
        patch("app.services.ingestion.scan_folder.SCAN_INCOMING_DIR", incoming),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSED_DIR", processed),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
    ):
        count = scan_and_ingest(MagicMock())

    assert count == 0
    assert pdf_file.exists()  # not moved


@pytest.mark.unit
def test_atomic_move_race_silently_skipped(tmp_path):
    """If shutil.move raises FileNotFoundError another worker claimed the file — skip silently."""
    from app.services.ingestion.scan_folder import scan_and_ingest

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    for d in (incoming, processing, processed, failed):
        d.mkdir()

    (incoming / "doc.pdf").write_bytes(b"%PDF-1.4")

    with (
        patch("app.services.ingestion.scan_folder.SCAN_INCOMING_DIR", incoming),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSED_DIR", processed),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
        patch("shutil.move", side_effect=FileNotFoundError("already moved")),
    ):
        count = scan_and_ingest(MagicMock())

    assert count == 0
    assert not list(failed.iterdir())


@pytest.mark.unit
def test_pdf_is_ingested_from_archived_path(tmp_path):
    """DB paths should point at the stable processed/archive location."""
    from app.services.ingestion.scan_folder import scan_and_ingest

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    for d in (incoming, processing, processed, failed):
        d.mkdir()

    (incoming / "doc.pdf").write_bytes(b"%PDF-1.4")
    seen_paths = []

    def fake_ingest(_db, pdf_path, _batch_id, _source_hash, owner_id=None):
        seen_paths.append(pdf_path)
        assert pdf_path.exists()
        assert processed in pdf_path.parents
        assert processing not in pdf_path.parents
        return object()

    with (
        patch("app.services.ingestion.scan_folder.SCAN_INCOMING_DIR", incoming),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSED_DIR", processed),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
        patch(
            "app.services.ingestion.scan_folder.ingest_scanned_file",
            side_effect=fake_ingest,
        ),
    ):
        count = scan_and_ingest(MagicMock())

    assert count == 1
    assert len(seen_paths) == 1


@pytest.mark.unit
def test_ingest_failure_rolls_back_poisoned_session_for_next_file(db_session, tmp_path):
    """A DB-level failure (not just an application-level exception) leaves the
    shared session's transaction in a failed state where every later query
    raises until it's rolled back. scan_and_ingest shares one session across
    every file in a tick's incoming/ listing — without a rollback in
    _ingest_one's except handler, one bad file poisons the session and every
    later file in the same tick gets wrongly marked failed too, even though
    nothing was actually wrong with them."""
    from sqlalchemy import text as _sql_text

    from app.services.ingestion.scan_folder import scan_and_ingest

    incoming = tmp_path / "incoming"
    processing = tmp_path / "processing"
    processed = tmp_path / "processed"
    failed = tmp_path / "failed"
    for d in (incoming, processing, processed, failed):
        d.mkdir()

    # Sorted processing order: "a_bad" before "b_good".
    (incoming / "a_bad.pdf").write_bytes(b"%PDF-1.4")
    (incoming / "b_good.pdf").write_bytes(b"%PDF-1.4")

    calls: list[str] = []

    def fake_ingest(db, pdf_path, batch_id, source_hash, owner_id=None):
        calls.append(pdf_path.name)
        if len(calls) == 1:
            # A plain `raise` wouldn't reproduce the bug -- SQLAlchemy only
            # enters a failed-transaction state after a genuine DB-level
            # error, not an ordinary Python exception.
            db.execute(_sql_text("SELECT 1/0"))
            return None  # unreachable -- the execute above always raises
        # Second file: proves the session is usable again by running a real
        # query against it. Without the fix, this itself raises, and this
        # file would be wrongly failed too.
        db.execute(_sql_text("SELECT 1"))
        return object()

    with (
        patch("app.services.ingestion.scan_folder.SCAN_INCOMING_DIR", incoming),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSED_DIR", processed),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
        patch(
            "app.services.ingestion.scan_folder.ingest_scanned_file",
            side_effect=fake_ingest,
        ),
    ):
        count = scan_and_ingest(db_session)

    assert calls == ["original.pdf", "original.pdf"]
    # Only b_good.pdf (the second file) should have succeeded.
    assert count == 1
    assert len(list(failed.iterdir())) == 1


@pytest.mark.unit
def test_sweep_stale_processing_dirs_moves_abandoned_claims_to_failed(tmp_path):
    """A processing/<batch_id>/ dir left behind by a crashed worker (claimed
    via the atomic rename, never reaching archive or fail) must be swept to
    failed/ rather than sitting invisible forever."""
    import os
    import time as _time

    from app.services.ingestion.scan_folder import sweep_stale_processing_dirs

    processing = tmp_path / "processing"
    failed = tmp_path / "failed"
    processing.mkdir()
    failed.mkdir()

    stale_dir = processing / "abandoned-batch"
    stale_dir.mkdir()
    (stale_dir / "original.pdf").write_bytes(b"%PDF-1.4")
    old_time = _time.time() - 3600
    os.utime(stale_dir, (old_time, old_time))

    fresh_dir = processing / "in-progress-batch"
    fresh_dir.mkdir()
    (fresh_dir / "original.pdf").write_bytes(b"%PDF-1.4")

    with (
        patch("app.services.ingestion.scan_folder.SCAN_PROCESSING_DIR", processing),
        patch("app.services.ingestion.scan_folder.SCAN_FAILED_DIR", failed),
    ):
        swept = sweep_stale_processing_dirs(stale_after_seconds=600)

    assert swept == 1
    assert not stale_dir.exists()
    assert (failed / "abandoned-batch" / "original.pdf").exists()
    assert (failed / "abandoned-batch" / "error.log").exists()
    # The still-in-progress claim must be left alone.
    assert fresh_dir.exists()
