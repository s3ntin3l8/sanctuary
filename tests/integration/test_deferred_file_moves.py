"""Confirm-time file moves happen after the commit, not during flush (#167)."""

from unittest.mock import patch

import pytest

import app.config
from app.models.database import Case, Document
from app.models.enums import CaseStatus, Jurisdiction


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(app.config, "DATA_DIR", tmp_path)
    return tmp_path


def _case(db, case_id="MOVE-1"):
    case = Case(
        id=case_id,
        title=case_id,
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db.add(case)
    db.commit()
    return case


def _triage_doc(db, data_dir, title, name):
    src = data_dir / "_TRIAGE" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"%PDF-1.4 " + title.encode())
    doc = Document(
        title=title,
        case_id="_TRIAGE",
        needs_review=True,
        file_path=str(src.relative_to(data_dir)),
    )
    db.add(doc)
    db.commit()
    return doc, src


def _assign(doc, case):
    doc.case_id = case.id
    doc.needs_review = False


@pytest.mark.integration
def test_file_moves_only_after_the_commit(db_session, data_dir):
    case = _case(db_session)
    doc, src = _triage_doc(db_session, data_dir, "Order", "a.pdf")

    _assign(doc, case)
    db_session.flush()
    # Flushed but not committed: the row has its new path, the file has not moved.
    new_path = data_dir / doc.file_path
    assert src.exists() and not new_path.exists()

    db_session.commit()
    assert not src.exists() and new_path.exists()
    assert new_path.parent == data_dir / "MOVE-1"


@pytest.mark.integration
def test_rollback_leaves_file_and_row_in_agreement(db_session, data_dir):
    case = _case(db_session)
    doc, src = _triage_doc(db_session, data_dir, "Order", "a.pdf")
    old_rel = doc.file_path

    _assign(doc, case)
    db_session.flush()
    db_session.rollback()

    assert src.exists()
    db_session.refresh(doc)
    assert doc.file_path == old_rel and doc.case_id == "_TRIAGE"
    assert not (data_dir / "MOVE-1").exists()

    # A retry of the same work (retry_on_db_locked) then succeeds normally.
    _assign(doc, case)
    db_session.commit()
    assert not src.exists() and (data_dir / doc.file_path).exists()


@pytest.mark.integration
def test_savepoint_rollback_does_not_move_the_file(db_session, data_dir):
    case = _case(db_session)
    doc, src = _triage_doc(db_session, data_dir, "Order", "a.pdf")
    old_rel = doc.file_path

    nested = db_session.begin_nested()
    _assign(doc, case)
    db_session.flush()
    nested.rollback()
    db_session.commit()

    assert src.exists()
    db_session.refresh(doc)
    assert doc.file_path == old_rel


@pytest.mark.integration
def test_failed_move_reverts_the_row_so_it_never_points_at_a_missing_file(
    db_session, data_dir
):
    case = _case(db_session)
    doc, src = _triage_doc(db_session, data_dir, "Order", "a.pdf")
    old_rel = doc.file_path

    _assign(doc, case)
    with patch("app.models.database.shutil.move", side_effect=OSError("disk full")):
        db_session.commit()

    assert src.exists()
    db_session.expire_all()
    assert db_session.get(Document, doc.id).file_path == old_rel


@pytest.mark.integration
def test_two_documents_with_the_same_name_get_distinct_destinations(
    db_session, data_dir
):
    case = _case(db_session)
    a, src_a = _triage_doc(db_session, data_dir, "Same title", "a.pdf")
    b, src_b = _triage_doc(db_session, data_dir, "Same title", "b.pdf")

    _assign(a, case)
    _assign(b, case)
    db_session.commit()

    assert a.file_path != b.file_path
    assert (data_dir / a.file_path).exists() and (data_dir / b.file_path).exists()
    assert not src_a.exists() and not src_b.exists()


@pytest.mark.integration
def test_repeated_flushes_move_the_original_file_once(db_session, data_dir):
    case = _case(db_session, "MOVE-A")
    other = _case(db_session, "MOVE-B")
    doc, src = _triage_doc(db_session, data_dir, "Order", "a.pdf")

    _assign(doc, case)
    db_session.flush()
    doc.case_id = other.id  # reassigned again before the commit
    db_session.flush()
    db_session.commit()

    assert not src.exists()
    final = data_dir / doc.file_path
    assert final.exists() and final.parent == data_dir / "MOVE-B"
    assert not (data_dir / "MOVE-A").exists() or not any(
        (data_dir / "MOVE-A").iterdir()
    )


@pytest.mark.integration
def test_failure_while_reverting_a_failed_move_is_logged_not_raised(
    db_session, data_dir, caplog
):
    case = _case(db_session)
    doc, src = _triage_doc(db_session, data_dir, "Order", "a.pdf")
    _assign(doc, case)

    class _Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    real_session_local = app.config.SessionLocal
    calls = {"n": 0}

    def _session_local():
        calls["n"] += 1
        # 1st: the committed-row check; 2nd: the revert, which fails.
        return real_session_local() if calls["n"] == 1 else _Boom()

    with (
        patch("app.models.database.shutil.move", side_effect=OSError("disk full")),
        patch("app.config.SessionLocal", _session_local),
        caplog.at_level("ERROR"),
    ):
        db_session.commit()  # must not raise even though the revert failed

    assert any("Could not revert file_path" in r.message for r in caplog.records)
