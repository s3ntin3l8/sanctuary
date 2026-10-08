"""Zero-document batches (#145) and the scan-folder processed/ gap (#155)."""

import os
import time
from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import patch

import pytest

import app.config
from app.core.timezone import now_utc
from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus
from app.services import auth_service


def _eml(subject="Hello", body=None, attachments=(), message_id="<m1@example.com>"):
    msg = EmailMessage()
    msg["From"] = "a@example.com"
    msg["To"] = "b@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 05 Oct 2026 10:00:00 +0000"
    if body is not None:
        msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


@pytest.fixture
def user(db_session):
    return auth_service.create_user(
        db_session, email="u@example.com", password=_PASSWORD
    )


_PASSWORD = "password123"  # pragma: allowlist secret


def _pdf_bytes(marker: str) -> bytes:
    """A real one-page PDF (PDFium must be able to open it); ``marker`` makes
    each one hash differently."""
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(200, 200)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    return buf.getvalue() + b"\n%" + marker.encode()


def _batches(db):
    db.expire_all()
    return db.query(IngestBatch).all()


# --- #145: an email that produces nothing ----------------------------------------------


@pytest.mark.integration
def test_empty_email_is_recorded_once_as_a_completed_no_document_batch(
    db_session, user
):
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    raw = _eml(body="")
    assert ingest_raw_email(db_session, raw, owner_id=user.id) is None
    first = _batches(db_session)

    # The Gmail sync watermark overlaps on purpose, so the same message returns.
    assert ingest_raw_email(db_session, raw, owner_id=user.id) is None
    second = _batches(db_session)

    assert len(first) == 1 and len(second) == 1
    assert second[0].id == first[0].id  # not deleted and recreated
    assert second[0].status == IngestBatchStatus.COMPLETED
    assert second[0].meta["reason"] == "no_new_documents"
    assert db_session.query(Document).count() == 0


@pytest.mark.integration
def test_all_duplicate_attachments_leave_a_tombstone_not_a_pending_batch(
    db_session, user, tmp_path, monkeypatch
):
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    monkeypatch.setattr(app.config, "DATA_DIR", tmp_path)
    pdf = _pdf_bytes("same attachment")
    first = ingest_raw_email(
        db_session,
        _eml(attachments=[("a.pdf", pdf)], message_id="<one@example.com>"),
        owner_id=user.id,
    )
    assert first is not None  # real document ingested

    again = ingest_raw_email(
        db_session,
        _eml(attachments=[("a.pdf", pdf)], message_id="<two@example.com>"),
        owner_id=user.id,
    )

    assert again is None
    tomb = [b for b in _batches(db_session) if b.message_id == "<two@example.com>"]
    assert len(tomb) == 1
    assert tomb[0].status == IngestBatchStatus.COMPLETED
    assert not any(b.status == IngestBatchStatus.PENDING for b in _batches(db_session))


@pytest.mark.integration
def test_gmail_index_treats_a_no_document_message_as_ingested(db_session, user):
    from sqlalchemy import select

    from app.models.database import GmailMessageIndex
    from app.services.gmail_index_service import _is_ingested
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    db_session.add(
        GmailMessageIndex(
            owner_id=user.id,
            gmail_id="g1",
            thread_id="t1",
            message_id="<m1@example.com>",
            subject="Hello",
            sent_at=now_utc(),
            indexed_at=now_utc(),
        )
    )
    db_session.commit()
    ingest_raw_email(db_session, _eml(body=""), owner_id=user.id)

    flagged = db_session.execute(
        select(GmailMessageIndex.gmail_id).where(_is_ingested())
    )
    assert [r[0] for r in flagged] == ["g1"]


# --- #145: counters and the sweep --------------------------------------------------------


def _batch(
    db, owner_id, status=IngestBatchStatus.PENDING, age_hours=0, docs=0, meta=None
):
    batch = IngestBatch(
        owner_id=owner_id,
        source_type=IngestBatchSourceType.EMAIL,
        status=status,
        subject="s",
        ingest_date=now_utc() - timedelta(hours=age_hours),
        meta=meta,
    )
    db.add(batch)
    db.flush()
    for i in range(docs):
        db.add(
            Document(
                title=f"d{i}",
                owner_id=owner_id,
                case_id="_TRIAGE",
                ingest_batch_id=batch.id,
                needs_review=True,
            )
        )
    db.commit()
    return batch


@pytest.mark.integration
def test_counters_ignore_document_less_and_dismissed_only_batches(db_session, user):
    from app.helpers import triage_inbox_count
    from app.models.enums import DocumentStatus
    from app.services.notifications_service import build_notifications

    _batch(db_session, user.id, docs=1)  # real bundle
    _batch(db_session, user.id, docs=0)  # invisible in the feed
    dismissed = _batch(db_session, user.id, docs=1)
    db_session.query(Document).filter(Document.ingest_batch_id == dismissed.id).update(
        {"status": DocumentStatus.DISMISSED}
    )
    db_session.commit()

    assert triage_inbox_count(db_session, user.id) == 1
    view = build_notifications(db_session, user)
    pending = [g for g in view.groups if g.kind == "pending_triage"]
    assert pending and pending[0].count == 1


@pytest.mark.integration
def test_empty_batch_sweep_deletes_stale_empties_but_spares_the_rest(db_session, user):
    from app.services.pipeline_status import recover_empty_batches

    stale_empty = _batch(db_session, user.id, age_hours=3)
    # COMPLETED says nothing about being a tombstone: only meta.reason does, so a
    # change to what COMPLETED means can never expose a tombstone to the sweep.
    completed_empty = _batch(
        db_session, user.id, status=IngestBatchStatus.COMPLETED, age_hours=3
    )
    fresh_empty = _batch(db_session, user.id, age_hours=0)  # upload still adding docs
    with_docs = _batch(db_session, user.id, age_hours=3, docs=1)
    tombstone = _batch(
        db_session,
        user.id,
        status=IngestBatchStatus.COMPLETED,
        age_hours=3,
        meta={"reason": "no_new_documents"},
    )
    tombstone_other_status = _batch(
        db_session,
        user.id,
        status=IngestBatchStatus.PENDING,
        age_hours=3,
        meta={"reason": "no_new_documents"},
    )
    slicing = _batch(
        db_session, user.id, status=IngestBatchStatus.AWAITING_SLICING, age_hours=3
    )

    result = recover_empty_batches(db_session)

    assert sorted(result["batch_ids"]) == sorted([stale_empty.id, completed_empty.id])
    remaining = {b.id for b in _batches(db_session)}
    assert remaining == {
        fresh_empty.id,
        with_docs.id,
        tombstone.id,
        tombstone_other_status.id,
        slicing.id,
    }


# --- #155: archived but never committed ----------------------------------------------------


@pytest.fixture
def scan_dirs(tmp_path, monkeypatch):
    dirs = {n: tmp_path / n for n in ("incoming", "processing", "processed", "failed")}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setattr(app.config, "DATA_DIR", tmp_path)
    for name, attr in (
        ("incoming", "SCAN_INCOMING_DIR"),
        ("processing", "SCAN_PROCESSING_DIR"),
        ("processed", "SCAN_PROCESSED_DIR"),
        ("failed", "SCAN_FAILED_DIR"),
    ):
        monkeypatch.setattr(f"app.services.ingestion.scan_folder.{attr}", dirs[name])
    return dirs


def _archived(scan_dirs, batch_id, *, age_seconds, owner=None):
    d = scan_dirs["processed"] / now_utc().date().isoformat() / batch_id
    d.mkdir(parents=True)
    (d / "original.pdf").write_bytes(_pdf_bytes(batch_id))
    if owner is not None:
        (d / ".owner").write_text(str(owner))
    old = time.time() - age_seconds
    os.utime(d, (old, old))
    return d


@pytest.mark.integration
def test_reconcile_ingests_an_archived_file_with_no_batch_row(
    db_session, user, scan_dirs
):
    from app.services.ingestion.scan_folder import reconcile_processed_orphans

    orphan = _archived(scan_dirs, "orphan-1", age_seconds=3600, owner=user.id)

    with patch("app.tasks.dispatch.dispatch_task"):
        created = reconcile_processed_orphans(db_session)

    assert created == 1
    batch = db_session.query(IngestBatch).one()
    assert batch.owner_id == user.id
    assert batch.raw_source_path.endswith("orphan-1/original.pdf")
    assert orphan.exists()  # re-ingested where it sits


@pytest.mark.integration
def test_reconcile_leaves_known_recent_and_already_ingested_files_alone(
    db_session, user, scan_dirs
):
    from app.core.paths import to_storage_path
    from app.services.ingestion.scan_folder import reconcile_processed_orphans

    known = _archived(scan_dirs, "known-1", age_seconds=3600, owner=user.id)
    db_session.add(
        IngestBatch(
            owner_id=user.id,
            source_type=IngestBatchSourceType.SCAN,
            raw_source_path=to_storage_path(known / "original.pdf"),
            status=IngestBatchStatus.PROCESSING,
            ingest_date=now_utc(),
        )
    )
    db_session.commit()
    _archived(scan_dirs, "recent-1", age_seconds=5, owner=user.id)  # still ingesting

    with patch("app.tasks.dispatch.dispatch_task"):
        created = reconcile_processed_orphans(db_session)

    assert created == 0
    assert db_session.query(IngestBatch).count() == 1


@pytest.mark.integration
def test_reconcile_ignores_date_folders_outside_the_lookback_window(
    db_session, user, scan_dirs
):
    from app.services.ingestion.scan_folder import reconcile_processed_orphans

    d = scan_dirs["processed"] / "2020-01-01" / "ancient"
    d.mkdir(parents=True)
    (d / "original.pdf").write_bytes(b"%PDF-1.4 old")
    old = time.time() - 10_000_000
    os.utime(d, (old, old))

    with patch("app.tasks.dispatch.dispatch_task"):
        assert reconcile_processed_orphans(db_session) == 0
    assert db_session.query(IngestBatch).count() == 0


@pytest.mark.integration
def test_ingest_one_records_the_owner_next_to_the_archived_file(
    db_session, user, scan_dirs
):
    from app.services.ingestion.scan_folder import _ingest_one

    src = scan_dirs["incoming"] / "doc.pdf"
    src.write_bytes(_pdf_bytes("hello"))
    old = time.time() - 60
    os.utime(src, (old, old))

    with (
        patch("app.tasks.dispatch.dispatch_task"),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
    ):
        assert _ingest_one(db_session, src, user.id) == 1

    sidecars = list(scan_dirs["processed"].glob("*/*/.owner"))
    assert len(sidecars) == 1 and sidecars[0].read_text() == str(user.id)


@pytest.mark.integration
def test_reconcile_failure_does_not_poison_the_tick_session(
    db_session, user, scan_dirs
):
    """scan_and_ingest runs the reconcile on its own session, so a recovered file
    that fails to ingest cannot break the same tick's incoming/ work."""
    from app.services.ingestion import scan_folder

    _archived(scan_dirs, "bad-1", age_seconds=3600, owner=user.id)
    (
        scan_dirs["processed"] / now_utc().date().isoformat() / "bad-1" / "original.pdf"
    ).write_bytes(b"not a pdf at all")
    good = scan_dirs["incoming"] / "good.pdf"
    good.write_bytes(_pdf_bytes("good"))
    old = time.time() - 60
    os.utime(good, (old, old))

    seen = []
    real = scan_folder.reconcile_processed_orphans

    def _spy(db, **kw):
        seen.append(db)
        return real(db, **kw)

    with (
        patch("app.tasks.dispatch.dispatch_task"),
        patch("app.services.ingestion.scan_folder._MTIME_GUARD_SECONDS", 0),
        patch("app.services.ingestion.scan_folder._last_reconcile_at", 0.0),
        patch.object(scan_folder, "reconcile_processed_orphans", side_effect=_spy),
    ):
        created = scan_folder.scan_and_ingest(db_session)

    assert seen and seen[0] is not db_session  # isolated session
    assert created == 1  # the good file still ingested
    assert (scan_dirs["failed"] / "bad-1").exists()  # the bad one was parked
