"""Pin: deleting a Case sets Document.case_id to NULL (back to Triage Inbox).

Before the FK existed, `Document.case_id` was a free string — deleting a Case
would strand the document with a ghost case_id pointing at nothing. With the
FK + ondelete=SET NULL, the document is automatically returned to the inbox
(`case_id IS NULL` is the canonical Triage Inbox marker per CLAUDE.md).

Uses a *dedicated* in-memory SQLite engine so we can flip
PRAGMA foreign_keys=ON without polluting the shared test_engine connection
pool — that PRAGMA persists per-connection and would carry over to other tests.
"""

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Case, Document, IngestBatch
from app.models.enums import IngestBatchSourceType


@pytest.fixture
def fk_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.mark.integration
def test_case_delete_sets_document_case_id_null(fk_engine):
    Session = sessionmaker(bind=fk_engine)
    db = Session()
    try:
        # Confirm FK enforcement is actually on for this connection
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1

        case = Case(id="FK-CASCADE-1", title="To be deleted")
        db.add(case)
        batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
        db.add(batch)
        db.commit()

        doc = Document(
            title="Doc tied to case",
            content="x",
            ingest_batch_id=batch.id,
            case_id=case.id,
        )
        db.add(doc)
        db.commit()
        doc_id = doc.id

        db.delete(case)
        db.commit()

        db.expire_all()
        refreshed = db.get(Document, doc_id)
        assert refreshed is not None, "Document should survive case deletion"
        assert refreshed.case_id is None, (
            f"Expected case_id to be NULL after case delete (Triage Inbox), "
            f"got {refreshed.case_id!r}"
        )
    finally:
        db.close()
