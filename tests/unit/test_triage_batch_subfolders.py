from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_migration_moves_files_and_updates_db(tmp_path):
    """Migration moves _TRIAGE/file.pdf → _TRIAGE/ib-42/file.pdf
    and updates document.file_path (absolute path format, as stored by batch_orchestrator)."""
    triage = tmp_path / "_TRIAGE"
    triage.mkdir()
    abs_a = triage / "doc_a.pdf"
    abs_b = triage / "doc_b.pdf"
    abs_a.write_bytes(b"%PDF")
    abs_b.write_bytes(b"%PDF")

    docs = [
        MagicMock(id=1, file_path=str(abs_a), ingest_batch_id=42),
        MagicMock(id=2, file_path=str(abs_b), ingest_batch_id=42),
    ]

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = docs

    with (
        patch("scripts.migrate_triage_to_batch_dirs.DATA_DIR", tmp_path),
        patch("scripts.migrate_triage_to_batch_dirs.get_db_session", return_value=db),
    ):
        from scripts.migrate_triage_to_batch_dirs import run_migration

        moved, skipped = run_migration(dry_run=False)

    assert moved == 2
    assert skipped == 0
    assert (triage / "ib-42" / "doc_a.pdf").exists()
    assert (triage / "ib-42" / "doc_b.pdf").exists()
    assert not abs_a.exists()
    assert docs[0].file_path == str(triage / "ib-42" / "doc_a.pdf")
    assert docs[1].file_path == str(triage / "ib-42" / "doc_b.pdf")
    db.commit.assert_called_once()


def test_migration_dry_run_does_not_move_files(tmp_path):
    """dry_run=True reports would-move count but does not move files or commit."""
    triage = tmp_path / "_TRIAGE"
    triage.mkdir()
    abs_path = triage / "doc_a.pdf"
    abs_path.write_bytes(b"%PDF")

    docs = [MagicMock(id=1, file_path=str(abs_path), ingest_batch_id=42)]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = docs

    with (
        patch("scripts.migrate_triage_to_batch_dirs.DATA_DIR", tmp_path),
        patch("scripts.migrate_triage_to_batch_dirs.get_db_session", return_value=db),
    ):
        from scripts.migrate_triage_to_batch_dirs import run_migration

        moved, skipped = run_migration(dry_run=True)

    assert moved == 1
    assert skipped == 0
    assert abs_path.exists()  # NOT moved
    db.commit.assert_not_called()


def test_migration_skips_already_in_subfolder(tmp_path):
    """Documents already inside an ib-* subfolder are counted as skipped."""
    triage = tmp_path / "_TRIAGE" / "ib-42"
    triage.mkdir(parents=True)
    already = triage / "doc.pdf"
    already.write_bytes(b"%PDF")

    docs = [MagicMock(id=1, file_path=str(already), ingest_batch_id=42)]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = docs

    with (
        patch("scripts.migrate_triage_to_batch_dirs.DATA_DIR", tmp_path),
        patch("scripts.migrate_triage_to_batch_dirs.get_db_session", return_value=db),
    ):
        from scripts.migrate_triage_to_batch_dirs import run_migration

        moved, skipped = run_migration(dry_run=False)

    assert moved == 0
    assert skipped == 1
    assert already.exists()  # untouched


def test_migration_skips_missing_file(tmp_path, capsys):
    """Documents whose file does not exist on disk are skipped with a WARN message."""
    (tmp_path / "_TRIAGE").mkdir()
    missing_path = tmp_path / "_TRIAGE" / "ghost.pdf"
    # deliberately do NOT create the file

    docs = [MagicMock(id=1, file_path=str(missing_path), ingest_batch_id=42)]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = docs

    with (
        patch("scripts.migrate_triage_to_batch_dirs.DATA_DIR", tmp_path),
        patch("scripts.migrate_triage_to_batch_dirs.get_db_session", return_value=db),
    ):
        from scripts.migrate_triage_to_batch_dirs import run_migration

        moved, skipped = run_migration(dry_run=False)

    assert moved == 0
    assert skipped == 1
    captured = capsys.readouterr()
    assert "WARN" in captured.out
    db.commit.assert_called_once()  # still commits (nothing to commit, but no crash)


@pytest.mark.asyncio
async def test_ingest_file_uses_batch_subfolder_in_triage(tmp_path):
    """Files uploaded to _TRIAGE land in _TRIAGE/ib-{batch_id}/."""
    from app.services.ingestion.service import ingest_file

    pdf_bytes = b"%PDF-1.4 fake"
    upload = MagicMock()
    upload.filename = "letter.pdf"
    # Return data once then empty bytes so the read loop terminates
    upload.read = AsyncMock(side_effect=[pdf_bytes, b""])
    upload.seek = AsyncMock()

    db = MagicMock()
    # No duplicate found
    db.query.return_value.filter.return_value.first.return_value = None

    with (
        patch("app.services.ingestion.service.DATA_DIR", tmp_path),
        patch("app.services.ingestion.service.validate_file_magic", return_value=None),
        patch("app.services.ingestion.service._create_document") as mock_create,
    ):
        mock_create.return_value = MagicMock(
            id=99, file_path=str(tmp_path / "_TRIAGE" / "ib-5" / "letter.pdf")
        )

        await ingest_file(
            file=upload,
            db=db,
            case_id=None,
            ingest_batch_id=5,
            skip_processing=True,
        )

    # The directory _TRIAGE/ib-5 must have been created on disk
    assert (tmp_path / "_TRIAGE" / "ib-5").is_dir()
    # _create_document was called with a file_path inside ib-5
    assert "ib-5" in str(mock_create.call_args.kwargs["file_path"])


@pytest.mark.asyncio
async def test_ingest_file_without_batch_id_uses_flat_triage(tmp_path):
    """Files uploaded to _TRIAGE WITHOUT a batch_id stay in flat _TRIAGE/ (no subdir)."""
    from app.services.ingestion.service import ingest_file

    pdf_bytes = b"%PDF-1.4 fake"
    upload = MagicMock()
    upload.filename = "orphan.pdf"
    # Return data once then empty bytes so the read loop terminates
    upload.read = AsyncMock(side_effect=[pdf_bytes, b""])
    upload.seek = AsyncMock()

    db = MagicMock()
    # No duplicate found
    db.query.return_value.filter.return_value.first.return_value = None

    with (
        patch("app.services.ingestion.service.DATA_DIR", tmp_path),
        patch("app.services.ingestion.service.validate_file_magic", return_value=None),
        patch("app.services.ingestion.service._create_document") as mock_create,
    ):
        mock_create.return_value = MagicMock(
            id=100, file_path=str(tmp_path / "_TRIAGE" / "orphan.pdf")
        )

        await ingest_file(
            file=upload,
            db=db,
            case_id=None,
            ingest_batch_id=None,
            skip_processing=True,
        )

    # Should land in flat _TRIAGE/, NOT in a ib-* subfolder
    assert (tmp_path / "_TRIAGE").is_dir()
    saved_path = str(mock_create.call_args.kwargs["file_path"])
    assert "ib-" not in saved_path
