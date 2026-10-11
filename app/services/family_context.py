"""A document does not stand alone: it sits in a family (sub-group, or a
parent/child tree). This module answers questions about that family and
propagates what one member establishes to the others.

Two rules govern the propagation:

* the **case** cascades through the family: a letter filed into a case brings
  its exhibits with it;
* the **proceeding** cascades only to members that print no Aktenzeichen of
  their own. A document's own AZ always beats an inherited proceeding
  ("observed beats inherited"): an exhibit that is a copy of an old ruling
  from another matter must not be filed into the live proceeding.

Where a case came from is kept in ``extraction_confidence["case_id"]``:
``"inherited"`` (set here), ``"user_set"`` (a person filed it) or absent (the
document's own reference resolved it).

Nothing here commits.
"""

from typing import Literal

from sqlalchemy.orm import Session

from app.models.database import Document, IngestBatch, Proceeding
from app.models.enums import DocumentRole, DocumentStatus, DocumentType, OriginatorType

CaseSource = Literal["own", "inherited", "user"]

_TRIAGE = "_TRIAGE"


def is_relay(doc: Document) -> bool:
    """True when the document is a court wrapper around someone else's filing.

    A relay is a pure transmission: its substance lives in what it forwards,
    so it should not own the identity of the group it heads.
    """
    return doc.document_type == DocumentType.RELAY or bool(doc.court_relay)


def is_exhibit(doc: Document) -> bool:
    """An enclosure of a party's filing: evidence the party attached, not
    correspondence addressed to us. What a court relays is not an exhibit."""
    parent = doc.parent
    return (
        doc.role == DocumentRole.ENCLOSURE
        and parent is not None
        and parent.originator_type != OriginatorType.COURT
    )


def has_case(doc: Document) -> bool:
    return bool(doc.case_id) and doc.case_id != _TRIAGE


def case_source(doc: Document) -> CaseSource | None:
    """Where the document's case came from; None while it has none."""
    if not has_case(doc):
        return None
    marker = (doc.extraction_confidence or {}).get("case_id")
    if marker == "user_set":
        return "user"
    if marker == "inherited":
        return "inherited"
    return "own"


def mark_case(doc: Document, source: Literal["inherited", "user_set"] | None) -> None:
    """Record the provenance of ``doc.case_id``; None clears it (own reference)."""
    conf = dict(doc.extraction_confidence or {})
    if source is None:
        conf.pop("case_id", None)
    else:
        conf["case_id"] = source
    doc.extraction_confidence = conf


def own_az(doc: Document) -> str | None:
    from app.services.ingestion.extractors import normalize_az_court

    return normalize_az_court(doc.az_court)


def family_root(doc: Document) -> Document:
    """Follow ``parent_id`` up to the document that heads the family."""
    seen = {doc.id}
    current = doc
    while current.parent is not None and current.parent.id not in seen:
        current = current.parent
        seen.add(current.id)
    return current


def family_members(db: Session, doc: Document) -> list[Document]:
    """The documents that travel together with ``doc``: its sub-group, else the
    parent/child tree it is part of. Dismissed documents never take part."""
    root = family_root(doc)
    anchor = root.sub_group_id or doc.sub_group_id
    if anchor is not None:
        members = (
            db.query(Document)
            .filter(
                Document.sub_group_id == anchor,
                Document.status != DocumentStatus.DISMISSED,
            )
            .order_by(Document.sub_group_sort_order, Document.id)
            .all()
        )
        if members:
            return members

    members = [root]
    seen = {root.id}
    frontier = [root.id]
    while frontier:
        children = (
            db.query(Document)
            .filter(
                Document.parent_id.in_(frontier),
                Document.status != DocumentStatus.DISMISSED,
            )
            .order_by(Document.id)
            .all()
        )
        frontier = []
        for child in children:
            if child.id not in seen:
                seen.add(child.id)
                members.append(child)
                frontier.append(child.id)
    return members


def _roots(members: list[Document]) -> list[Document]:
    ids = {m.id for m in members}
    return [m for m in members if m.parent_id is None or m.parent_id not in ids]


def _family_case(members: list[Document]) -> str | None:
    """The family's case: a root's own case first, else the single case the
    members' own/user filings agree on. None when there is none, or they differ."""
    for root in _roots(members):
        if has_case(root):
            return root.case_id
    held = {
        m.case_id for m in members if has_case(m) and case_source(m) in ("own", "user")
    }
    return held.pop() if len(held) == 1 else None


def _family_proceeding(
    db: Session, members: list[Document], case_id: str
) -> Proceeding | None:
    """The proceeding the family's head is in (inside ``case_id``)."""
    for root in _roots(members):
        if root.proceeding_id is None or root.case_id != case_id:
            continue
        proc = db.get(Proceeding, root.proceeding_id)
        if proc is not None and proc.case_id == case_id:
            return proc
    return None


def propagate(db: Session, doc: Document) -> list[Document]:
    """Spread the family's case and proceeding to the members that lack their own.

    Returns the documents that changed. Documents a person has confirmed keep
    their proceeding; a case a person or the document's own reference
    established is never overwritten.
    """
    from app.services.ingestion.service import apply_review_reasons
    from app.services.proceeding_rematch import fill_proceeding_from_az

    members = family_members(db, doc)
    case_id = _family_case(members)
    if case_id is None:
        return []

    changed: dict[int, Document] = {}

    for m in members:
        if not has_case(m) or (case_source(m) == "inherited" and m.case_id != case_id):
            m.case_id = case_id
            mark_case(m, "inherited")
            changed[m.id] = m

    batch = db.get(IngestBatch, doc.ingest_batch_id) if doc.ingest_batch_id else None
    if batch is not None and (not batch.case_id or batch.case_id == _TRIAGE):
        batch.case_id = case_id

    family_proc = _family_proceeding(db, members, case_id)
    root_ids = {r.id for r in _roots(members)}
    for m in members:
        if m.case_id != case_id:
            continue
        before = m.proceeding_id
        current = db.get(Proceeding, m.proceeding_id) if m.proceeding_id else None
        if current is not None and current.case_id != case_id:
            m.proceeding_id = None
            current = None
        az = own_az(m)
        if az:
            # Observed beats inherited: a proceeding a member did not earn by
            # its own AZ is dropped (unless a person confirmed it), then the one
            # its AZ names is used if the case has it. A root is the source of
            # the family's proceeding, never a recipient.
            if (
                current is not None
                and m.id not in root_ids
                and current.az_court != az
                and m.confirmed_at is None
            ):
                m.proceeding_id = None
            fill_proceeding_from_az(db, m)
        elif (
            family_proc is not None
            and m.proceeding_id != family_proc.id
            and m.confirmed_at is None
        ):
            m.proceeding_id = family_proc.id
        if m.proceeding_id != before:
            changed[m.id] = m

    for m in changed.values():
        apply_review_reasons(m)
    db.flush()
    return list(changed.values())


def propagate_batch(db: Session, batch_id: int) -> list[Document]:
    """Run :func:`propagate` for every family of a batch.

    Families that found no case of their own take the batch's case when the
    families that did all agree on one; two letters filed into two cases leave
    the rest alone. Then draft proceedings that only an exhibit's own AZ had
    seeded are removed, and the bundle follows its documents.
    """
    from app.services.ingestion.service import apply_review_reasons
    from app.services.proceeding_rematch import is_unused_draft, sync_batch_proceeding

    batch = db.get(IngestBatch, batch_id)
    docs = (
        db.query(Document)
        .filter(
            Document.ingest_batch_id == batch_id,
            Document.status != DocumentStatus.DISMISSED,
        )
        .order_by(Document.id)
        .all()
    )
    if batch is None or not docs:
        return []

    changed: dict[int, Document] = {}
    families: dict[int, list[Document]] = {}
    done: set[int] = set()
    for d in docs:
        if d.id in done:
            continue
        members = family_members(db, d)
        done.update(m.id for m in members)
        families[d.id] = members
        for m in propagate(db, d):
            changed[m.id] = m

    cases = {_family_case(members) for members in families.values()} - {None}
    if len(cases) == 1:
        (case_id,) = cases
        for members in families.values():
            if _family_case(members) is not None:
                continue
            for m in members:
                if not has_case(m):
                    m.case_id = case_id
                    mark_case(m, "inherited")
                    changed[m.id] = m
        if not batch.case_id or batch.case_id == _TRIAGE:
            batch.case_id = case_id

    # An exhibit's own AZ may have seeded a draft proceeding at METADATA time,
    # before the exhibit was known to be one.
    drafts: dict[int, Proceeding] = {}
    for d in docs:
        if d.proceeding_id is None or not is_exhibit(d) or d.confirmed_at:
            continue
        proc = db.get(Proceeding, d.proceeding_id)
        if proc is not None and proc.is_draft and proc.az_court == own_az(d):
            drafts[proc.id] = proc
            d.proceeding_id = None
            changed[d.id] = d
    db.flush()
    for proc in drafts.values():
        if is_unused_draft(proc, db, batch_id):
            if batch.proceeding_id == proc.id:
                batch.proceeding_id = None
            db.delete(proc)

    for m in changed.values():
        apply_review_reasons(m)
    db.flush()
    sync_batch_proceeding(db, batch)
    return list(changed.values())
