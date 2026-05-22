from unittest.mock import MagicMock, patch


def test_migration_moves_files_and_updates_db(tmp_path):
    """Migration moves _TRIAGE/file.pdf → _TRIAGE/ib-42/file.pdf
    and updates document.file_path."""
    triage = tmp_path / "_TRIAGE"
    triage.mkdir()
    (triage / "doc_a.pdf").write_bytes(b"%PDF")
    (triage / "doc_b.pdf").write_bytes(b"%PDF")

    docs = [
        MagicMock(id=1, file_path="_TRIAGE/doc_a.pdf", ingest_batch_id=42),
        MagicMock(id=2, file_path="_TRIAGE/doc_b.pdf", ingest_batch_id=42),
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
    assert not (triage / "doc_a.pdf").exists()
    assert docs[0].file_path == "_TRIAGE/ib-42/doc_a.pdf"
    assert docs[1].file_path == "_TRIAGE/ib-42/doc_b.pdf"
    db.commit.assert_called_once()
