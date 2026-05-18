"""Triage sub-group CRUD + cover-letter management.

Free-function module (no class). Owns the batch sub-group lifecycle:
- ensure_sub_groups_initialized: lazy materialisation on first user mutation
- create/rename/reorder/delete/reset sub-groups
- set_cover_letter (lives here because cover-letter is a sub-group-scoped role)

Sub-groups are explicit BatchSubGroup rows that override the implicit
parent-root grouping when the user manually reorganises a bundle. The lazy
initialisation pattern means: until the user touches a bundle, it stays in
"auto mode" (no rows); the first mutation materialises rows mirroring the
auto layout, then subsequent mutations operate on the explicit rows.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import SIG_ORDER as _SIG_ORDER
from app.models.database import BatchSubGroup, Document, IngestBatch
from app.models.enums import DocumentRole


def ensure_sub_groups_initialized(db: Session, batch_id: int) -> list[BatchSubGroup]:
    """Materialize BatchSubGroup rows for a batch on first user mutation.

    Idempotent: if rows already exist, returns them as-is. On first call,
    mirrors the auto parent_groups hierarchy into explicit BatchSubGroup rows
    and assigns each doc its sub_group_id + sub_group_sort_order.
    """
    existing = (
        db.query(BatchSubGroup)
        .filter(BatchSubGroup.batch_id == batch_id)
        .order_by(BatchSubGroup.sort_order)
        .all()
    )
    if existing:
        return existing

    batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
    if not batch:
        return []

    docs = db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
    docs_by_id = {d.id: d for d in docs}
    children_of: dict[int, list] = {}
    roots: list[Document] = []
    for d in docs:
        if not d.parent_id or d.parent_id not in docs_by_id:
            roots.append(d)
        else:
            children_of.setdefault(d.parent_id, []).append(d)

    roots.sort(key=lambda d: (_SIG_ORDER.get(d.significance_tier, 99), d.id or 0))

    if not roots:
        roots = list(docs)

    # Flat bundle: all docs are roots with no children → collapse into one group.
    # Creating one group per doc produces a confusing N-group UI with no useful structure.
    if len(roots) == len(docs):
        sg = BatchSubGroup(batch_id=batch_id, label=None, sort_order=0)
        db.add(sg)
        db.flush()
        for order, doc in enumerate(roots):
            doc.sub_group_id = sg.id
            doc.sub_group_sort_order = order
        db.flush()
        return [sg]

    sub_groups: list[BatchSubGroup] = []
    for group_idx, root in enumerate(roots):
        sg = BatchSubGroup(batch_id=batch_id, label=None, sort_order=group_idx)
        db.add(sg)
        db.flush()

        sort_order = 0
        queue = [root]
        visited: set[int] = set()
        while queue:
            node = queue.pop(0)
            if node.id in visited:
                continue
            visited.add(node.id)
            node.sub_group_id = sg.id
            node.sub_group_sort_order = sort_order
            sort_order += 1
            queue.extend(children_of.get(node.id, []))

        sub_groups.append(sg)

    db.flush()
    return sub_groups


def set_cover_letter(db: Session, doc_id: int, batch_id: int) -> Document:
    """Mark doc_id as COVER_LETTER in its sub-group; clear previous cover in the same group."""
    ensure_sub_groups_initialized(db, batch_id)

    doc = (
        db.query(Document)
        .filter(Document.id == doc_id, Document.ingest_batch_id == batch_id)
        .first()
    )
    if not doc:
        raise ValueError(f"Document {doc_id} not in batch {batch_id}")

    if doc.sub_group_id is not None:
        db.query(Document).filter(
            Document.sub_group_id == doc.sub_group_id,
            Document.role == DocumentRole.COVER_LETTER,
        ).update({"role": DocumentRole.ENCLOSURE})
    else:
        db.query(Document).filter(
            Document.ingest_batch_id == batch_id,
            Document.sub_group_id.is_(None),
            Document.role == DocumentRole.COVER_LETTER,
        ).update({"role": DocumentRole.ENCLOSURE})

    doc.role = DocumentRole.COVER_LETTER
    db.flush()
    return doc


def create_sub_group(db: Session, batch_id: int) -> BatchSubGroup:
    """Create a new empty sub-group at the end of the batch's group list."""
    ensure_sub_groups_initialized(db, batch_id)

    max_order = (
        db.query(func.max(BatchSubGroup.sort_order))
        .filter(BatchSubGroup.batch_id == batch_id)
        .scalar()
    ) or 0

    sg = BatchSubGroup(batch_id=batch_id, label=None, sort_order=max_order + 1)
    db.add(sg)
    db.flush()
    return sg


def rename_sub_group(
    db: Session,
    sub_group_id: int | None,
    batch_id: int,
    label: str,
    lead_doc_id: int | None = None,
) -> BatchSubGroup:
    """Set explicit label on a sub-group. Empty string clears to auto-derived.

    When sub_group_id is None (auto mode), lazy-initializes BatchSubGroup rows
    and identifies the target group via lead_doc_id.
    """
    if sub_group_id is not None:
        sg = (
            db.query(BatchSubGroup)
            .filter(
                BatchSubGroup.id == sub_group_id, BatchSubGroup.batch_id == batch_id
            )
            .first()
        )
        if not sg:
            raise ValueError(f"SubGroup {sub_group_id} not found in batch {batch_id}")
    else:
        ensure_sub_groups_initialized(db, batch_id)
        sg = None
        if lead_doc_id is not None:
            doc = db.get(Document, lead_doc_id)
            if doc and doc.sub_group_id:
                sg = (
                    db.query(BatchSubGroup)
                    .filter(
                        BatchSubGroup.id == doc.sub_group_id,
                        BatchSubGroup.batch_id == batch_id,
                    )
                    .first()
                )
        if not sg:
            raise ValueError(f"Cannot identify sub-group in batch {batch_id}")
    sg.label = label.strip() or None
    db.flush()
    return sg


def reorder_documents(
    db: Session,
    batch_id: int,
    ordered_doc_ids: list[int],
    target_sub_group_id: int | None,
    lead_doc_id: int | None = None,
) -> None:
    """Assign docs to target_sub_group_id with sequential sub_group_sort_order.

    Called once per sub-group after drag-drop completes. The frontend sends
    each sub-group's current doc order as a separate POST.
    When target_sub_group_id is None (auto mode), lazy-initializes BatchSubGroup
    rows and identifies the target via lead_doc_id.
    """
    groups = ensure_sub_groups_initialized(db, batch_id)

    if target_sub_group_id is not None:
        sg = next((g for g in groups if g.id == target_sub_group_id), None)
        if not sg:
            raise ValueError(f"SubGroup {target_sub_group_id} not in batch {batch_id}")
    else:
        if lead_doc_id is not None:
            doc = db.get(Document, lead_doc_id)
            if doc and doc.sub_group_id:
                target_sub_group_id = doc.sub_group_id
        if target_sub_group_id is None:
            target_sub_group_id = groups[0].id if groups else None
        if not target_sub_group_id:
            raise ValueError(f"No sub-groups in batch {batch_id}")

    for order, doc_id in enumerate(ordered_doc_ids):
        db.query(Document).filter(
            Document.id == doc_id,
            Document.ingest_batch_id == batch_id,
        ).update({"sub_group_id": target_sub_group_id, "sub_group_sort_order": order})

    db.flush()


def delete_sub_group(
    db: Session,
    sub_group_id: int | None,
    batch_id: int,
    lead_doc_id: int | None = None,
) -> None:
    """Delete a sub-group; reassign its docs to the next remaining sub-group.

    When sub_group_id is None (auto mode), lazy-initializes BatchSubGroup rows
    and identifies the target group via lead_doc_id. If no other sub-groups
    remain, docs' sub_group_id falls back to NULL via the SET NULL cascade
    and the batch reverts to auto mode.
    """
    groups = ensure_sub_groups_initialized(db, batch_id)

    if sub_group_id is not None:
        sg = next((g for g in groups if g.id == sub_group_id), None)
    else:
        sg = None
        if lead_doc_id is not None:
            doc = db.get(Document, lead_doc_id)
            if doc and doc.sub_group_id:
                sg = next((g for g in groups if g.id == doc.sub_group_id), None)
    if not sg:
        raise ValueError(f"Cannot identify sub-group to delete in batch {batch_id}")

    remaining = sorted((g for g in groups if g.id != sg.id), key=lambda g: g.sort_order)
    target = remaining[0] if remaining else None
    if target is not None:
        max_row = (
            db.query(Document.sub_group_sort_order)
            .filter(Document.sub_group_id == target.id)
            .order_by(Document.sub_group_sort_order.desc())
            .first()
        )
        next_order = (max_row[0] + 1) if max_row and max_row[0] is not None else 0
        moved = (
            db.query(Document)
            .filter(Document.sub_group_id == sg.id)
            .order_by(Document.sub_group_sort_order)
            .all()
        )
        for offset, doc in enumerate(moved):
            doc.sub_group_id = target.id
            doc.sub_group_sort_order = next_order + offset
        db.flush()

    db.delete(sg)
    db.flush()


def reset_sub_groups(db: Session, batch_id: int) -> None:
    """Remove all BatchSubGroup rows for this batch, reverting to auto mode.

    Clears sub_group_id and sub_group_sort_order from all docs in the batch.
    The batch_sub_groups rows are cascade-deleted when the BatchSubGroup ORM
    objects are deleted.
    """
    db.query(Document).filter(Document.ingest_batch_id == batch_id).update(
        {"sub_group_id": None, "sub_group_sort_order": None}
    )
    db.query(BatchSubGroup).filter(BatchSubGroup.batch_id == batch_id).delete()
    db.flush()
