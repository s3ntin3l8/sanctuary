"""Route triage bundles and loose documents to a case.

The glue around ``triage_confirmation``: resolve (or create) the target case,
check the actor may edit it, then confirm/assign a batch or a single document —
promoting draft cases/proceedings, attaching the proceeding from the document's
Aktenzeichen and re-running enrichment for documents that just left triage.
Shared by the triage routes and the MCP ``assign_to_case`` tool; knows nothing
about HTTP, failures raise :class:`RoutingError`.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from app.models.database import Case, Document, Proceeding, User
from app.services import access_service, family_context
from app.services.triage_confirmation import (
    confirm_bundle,
    confirm_document,
    reset_and_reenrich,
)


class RoutingError(Exception):
    """A routing failure carrying the HTTP-equivalent status and stable code."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def require_editable_target(db: Session, user: User, case_id: str) -> Case | None:
    if case_id == "_TRIAGE":
        return None
    case = db.get(Case, case_id)
    # Same 404 for "missing" and "not yours": a 403 here would confirm that a
    # case the caller cannot see exists.
    if case is None or not access_service.can_view_case(db, user, case):
        raise RoutingError(404, "not_found", f"Case {case_id} not found.")
    if not access_service.can_edit_case(db, user, case):
        raise RoutingError(403, "forbidden", "You cannot assign to that case.")
    return case


def check_proceeding(db: Session, proceeding_id: int | None, case_id: str) -> None:
    if proceeding_id is None:
        return
    proc = db.get(Proceeding, proceeding_id)
    if proc is None or proc.case_id != case_id:
        raise RoutingError(
            422, "proceeding_mismatch", "proceeding_id does not belong to case_id."
        )


def resolve_case(
    db: Session,
    user: User,
    *,
    case_id: str | None,
    new_case_id: str | None,
    new_case_title: str | None,
) -> tuple[str, Literal["created", "assigned"]]:
    """Return (case_id, action) creating the case when ``new_case_id`` is set."""
    from app.services.case_service import get_or_create_case_from_reference

    if new_case_id:
        case, _, created = get_or_create_case_from_reference(
            db,
            internal_id=new_case_id.strip(),
            batch_subject=(new_case_title or "").strip() or None,
            is_draft=False,
            owner_id=user.id,
        )
        db.flush()
        return case.id, "created" if created else "assigned"
    if not case_id:
        raise RoutingError(422, "validation_error", "case_id is required.")
    return case_id, "assigned"


def route_batch(
    db: Session, batch_id: int, case_id: str, proceeding_id: int | None, finalize: bool
) -> None:
    pre_triage = (
        db.query(Document)
        .filter(Document.ingest_batch_id == batch_id, Document.case_id == "_TRIAGE")
        .all()
    )
    batch = confirm_bundle(
        db, batch_id, case_id=case_id, proceeding_id=proceeding_id, finalize=finalize
    )
    if batch is None:
        raise RoutingError(404, "not_found", "Bundle not found.")
    if case_id != "_TRIAGE" and pre_triage:
        for d in pre_triage:
            db.refresh(d)
        reset_and_reenrich(db, pre_triage)


def route_document(
    db: Session, doc_id: int, case_id: str, proceeding_id: int | None, finalize: bool
) -> None:
    pre_case = db.query(Document.case_id).filter(Document.id == doc_id).scalar()
    doc = confirm_document(db, doc_id, case_id=case_id, finalize=finalize)
    if doc is None:
        raise RoutingError(404, "not_found", "Document not found.")
    # Filing into a draft case makes it real, as confirm_bundle does.
    target = db.get(Case, case_id) if case_id != "_TRIAGE" else None
    promoted = target is not None and target.is_draft
    if target is not None and promoted:
        target.is_draft = False
    if proceeding_id is not None:
        doc.proceeding_id = proceeding_id
        proc = db.get(Proceeding, proceeding_id)
        if proc and proc.is_draft:
            proc.is_draft = False
    # METADATA doesn't re-run when a case is assigned here, so a document whose
    # Aktenzeichen names a proceeding of the case is attached to it now.
    from app.services.proceeding_rematch import (
        fill_proceeding_from_az,
        sync_batch_proceeding,
    )

    filled = proceeding_id is None and fill_proceeding_from_az(db, doc)
    # Members of the document's family without an AZ of their own follow it.
    followed = family_context.propagate(db, doc)
    if proceeding_id is not None or filled or followed:
        sync_batch_proceeding(db, doc.ingest_batch)
    # Otherwise confirm_document's own commit above already persisted everything.
    if proceeding_id is not None or promoted or filled or followed:
        db.commit()
        db.refresh(doc)
    if (not pre_case or pre_case == "_TRIAGE") and case_id != "_TRIAGE":
        reset_and_reenrich(db, [doc])
