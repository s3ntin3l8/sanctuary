"""Verifier tasks (``ai`` queue): a second model checks the local AI's output.

Both tasks re-check the verifier's switch and scope when they run (it may have
changed since they were queued); out of scope means no request. They never
raise: a failed verification only leaves fewer findings, never a failed stage.
"""

from __future__ import annotations

import logging

from app.core.safe_error import describe_exception
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.verify.verify_document_task")
def verify_document_task(doc_id: int) -> dict:
    from app.dependencies import get_db_session
    from app.services.intelligence.verifier import verify_document

    db = get_db_session()
    try:
        result = verify_document(db, doc_id)
    except Exception as exc:
        db.rollback()
        logger.error(
            "Doc #%d: verification failed: %s", doc_id, describe_exception(exc)
        )
        return {"status": "failed", "doc_id": doc_id}
    finally:
        db.close()
    return {
        "status": "done" if result.called else "skipped",
        "doc_id": doc_id,
        "recorded": result.recorded,
    }


@celery_app.task(name="app.tasks.verify.verify_case_task")
def verify_case_task(case_id: str) -> dict:
    """The case brief, then each of the case's documents (one task each)."""
    from app.dependencies import get_db_session
    from app.services.ai_config import DOCUMENT_VERIFIER_GROUPS, verifier_for_case
    from app.services.intelligence.verifier import case_document_ids, verify_case_brief

    db = get_db_session()
    try:
        brief = verify_case_brief(db, case_id)
        doc_ids = (
            case_document_ids(db, case_id)
            if verifier_for_case(db, case_id, DOCUMENT_VERIFIER_GROUPS)
            else []
        )
    except Exception as exc:
        db.rollback()
        logger.error(
            "Case %s: verification failed: %s", case_id, describe_exception(exc)
        )
        return {"status": "failed", "case_id": case_id}
    finally:
        db.close()
    for doc_id in doc_ids:
        verify_document_task.delay(doc_id)
    return {
        "status": "done",
        "case_id": case_id,
        "brief_checked": brief.called,
        "documents": len(doc_ids),
    }


def maybe_verify_document(doc_id: int) -> None:
    """After CLAIMS: queue the document's verification when it is in scope.

    Checked here too (not only in the task), so an out-of-scope document is
    never even queued. Never raises: the claims stage must finish regardless.
    """
    from app.dependencies import get_db_session
    from app.models.database import Document
    from app.services.ai_config import DOCUMENT_VERIFIER_GROUPS, verifier_for_case

    try:
        db = get_db_session()
        try:
            doc = db.get(Document, doc_id)
            in_scope = (
                doc is not None
                and verifier_for_case(db, doc.case_id, DOCUMENT_VERIFIER_GROUPS)
                is not None
            )
        finally:
            db.close()
        if in_scope:
            verify_document_task.delay(doc_id)
    except Exception as exc:
        logger.error(
            "Doc #%d: could not queue verification: %s",
            doc_id,
            describe_exception(exc),
        )
