"""Pin: deleting a Case sets Document.case_id to NULL (back to Triage Inbox).

Before the FK existed, `Document.case_id` was a free string — deleting a Case
would strand the document with a ghost case_id pointing at nothing. With the
FK + ondelete=SET NULL, the document is automatically returned to the inbox
(`case_id IS NULL` is the canonical Triage Inbox marker per CLAUDE.md).

Postgres enforces foreign keys natively (no PRAGMA toggle needed), so this
uses the shared `db_session` fixture like any other test.
"""

import pytest

from app.models.database import Case, Document, IngestBatch
from app.models.enums import IngestBatchSourceType


@pytest.mark.integration
def test_case_delete_sets_document_case_id_null(db_session):
    case = Case(id="FK-CASCADE-1", title="To be deleted")
    db_session.add(case)
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()

    doc = Document(
        title="Doc tied to case",
        content="x",
        ingest_batch_id=batch.id,
        case_id=case.id,
    )
    db_session.add(doc)
    db_session.commit()
    doc_id = doc.id

    db_session.delete(case)
    db_session.commit()

    db_session.expire_all()
    refreshed = db_session.get(Document, doc_id)
    assert refreshed is not None, "Document should survive case deletion"
    assert refreshed.case_id is None, (
        f"Expected case_id to be NULL after case delete (Triage Inbox), "
        f"got {refreshed.case_id!r}"
    )
