"""Per-user triage ownership guards.

`require_triage_object_owner` is applied as a router-level dependency on the
triage router: for any triage request that references a document or batch by id
(in the path or form body), it 404s unless that object belongs to the current
user. This centralizes the ownership check so individual mutation routes don't
each have to repeat it.

Edge cases (batch_confirm/batch_assign 'keys', relationship rel_id) carry their
own inline guards in their route modules.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.models.database import Document, IngestBatch, User


def _owns_doc(db: Session, doc_id: int, user: User) -> bool:
    doc = db.query(Document.owner_id).filter(Document.id == doc_id).first()
    return doc is not None and doc[0] == user.id


def _owns_batch(db: Session, batch_id: int, user: User) -> bool:
    row = db.query(IngestBatch.owner_id).filter(IngestBatch.id == batch_id).first()
    return row is not None and row[0] == user.id


async def require_triage_object_owner(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """404 when the request targets a doc/batch the user doesn't own."""
    doc_id = request.path_params.get("doc_id") or request.query_params.get("doc_id")
    batch_id = request.path_params.get("batch_id") or request.query_params.get(
        "batch_id"
    )

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        try:
            form = await request.form()  # cached by Starlette; routes re-read safely
        except Exception:
            form = {}
        doc_id = doc_id or form.get("doc_id")
        batch_id = batch_id or form.get("batch_id")

    if doc_id is not None:
        try:
            did = int(doc_id)
        except (TypeError, ValueError):
            did = None
        if did is not None and not _owns_doc(db, did, user):
            raise HTTPException(status_code=404, detail="Not found")

    if batch_id is not None:
        try:
            bid = int(batch_id)
        except (TypeError, ValueError):
            bid = None
        if bid is not None and not _owns_batch(db, bid, user):
            raise HTTPException(status_code=404, detail="Not found")
