"""Regression: ingest_raw_email must not leak files written to disk when a
later step in the same call raises before the final commit.

Body/attachment files are written to disk as each Document is created, but
the whole batch is only committed once at the end. A failure partway through
(e.g. the second of two attachments) previously left the first attachment's
file — and any others already written — orphaned on disk with no DB row
pointing at them once the transaction rolled back.
"""

import email.message

import pytest

from app.models.database import Document, IngestBatch
from app.services.ingestion.batch_orchestrator import ingest_raw_email


def _build_email_with_two_attachments(message_id: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Subject"] = "Zwei Anlagen"
    msg["Message-ID"] = message_id
    msg.set_content("Anbei zwei Unterlagen.")
    msg.add_attachment(
        b"%PDF-1.4 first", maintype="application", subtype="pdf", filename="a.pdf"
    )
    msg.add_attachment(
        b"%PDF-1.4 second", maintype="application", subtype="pdf", filename="b.pdf"
    )
    return msg.as_bytes()


@pytest.mark.integration
def test_mid_ingest_failure_removes_already_written_attachment_files(
    db_session, sample_user, monkeypatch
):
    from app.services import pipeline_status

    calls = {"n": 0}
    real_initialize = pipeline_status.initialize

    def _init_boom_on_second_call(doc, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom on second doc")
        return real_initialize(doc, **kwargs)

    monkeypatch.setattr(pipeline_status, "initialize", _init_boom_on_second_call)
    # batch_orchestrator imports `initialize as _pipeline_init` *inside* the
    # function body on every call, so patching the source module is enough —
    # no separate patch of batch_orchestrator's own namespace needed.

    raw = _build_email_with_two_attachments("<two-atts@example.com>")

    with pytest.raises(RuntimeError, match="boom on second doc"):
        ingest_raw_email(db_session, raw, owner_id=sample_user.id)

    db_session.expire_all()
    assert (
        db_session.query(IngestBatch)
        .filter(IngestBatch.message_id == "<two-atts@example.com>")
        .first()
        is None
    )
    assert (
        db_session.query(Document).filter(Document.original_filename == "a.pdf").first()
        is None
    )
    assert (
        db_session.query(Document).filter(Document.original_filename == "b.pdf").first()
        is None
    )

    from app.config import DATA_DIR

    leaked = list((DATA_DIR / "_TRIAGE").glob("*a.pdf")) + list(
        (DATA_DIR / "_TRIAGE").glob("*b.pdf")
    )
    assert leaked == [], f"partial attachment files leaked: {leaked}"
