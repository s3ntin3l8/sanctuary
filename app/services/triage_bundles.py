"""Triage feed reads + bundle hydration.

Free-function module (no class). Owns the queue-reading half of the former
TriageService: building BundleView lists from the live document queue and
hydrating them with action items, proof edges, and case-metadata resolution.

See triage_confirmation.py for the confirmation flow, triage_subgroups.py for
sub-group CRUD, triage_reactions.py for reactions, and triage_dismissal.py
for dismiss/delete.
"""

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from app.constants import SIG_ORDER as _SIG_ORDER
from app.models.database import (
    BatchSubGroup,
    Case,
    Document,
    DocumentRelationship,
    IngestBatch,
)
from app.models.enums import (
    DocumentRole,
    DocumentStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    RelationshipType,
)
from app.services.triage_service import BundleView, _sanitize_case_title


def _bundle_pipeline_label(bundle: BundleView) -> str:
    """Pipeline aggregate state — matches chip labels in _pipeline_aggregate.html.

    Returns: 'ready', 'review_metadata', 'processing', or 'failed'.
    """
    summary = bundle.pipeline_summary
    if summary.get("failed", 0) > 0:
        return "failed"
    if summary.get("running", 0) + summary.get("pending", 0) > 0:
        return "processing"
    if bundle.has_unconfirmed_metadata:
        return "review_metadata"
    return "ready"


def _build_bundles(
    db: Session, include_sub_groups: bool = True
) -> dict[str, BundleView]:
    """Shared bundle-construction logic for feed reads.

    Reads the triage-eligible document set (open batches, _TRIAGE docs, and
    needs_review docs whose batch is still open) and groups by ingest_batch_id
    or synthesises a one-doc bundle for unbatched documents.

    Set include_sub_groups=False when the caller only needs filter options
    (lighter query, no sub-group joinedload).
    """
    options = [
        contains_eager(Document.ingest_batch).joinedload(IngestBatch.proceeding),
        joinedload(Document.proceeding),
    ]
    if include_sub_groups:
        options.append(
            contains_eager(Document.ingest_batch).joinedload(IngestBatch.sub_groups)
        )
        options.append(joinedload(Document.sub_group))

    docs = (
        db.query(Document)
        .outerjoin(IngestBatch, Document.ingest_batch_id == IngestBatch.id)
        .options(*options)
        .filter(
            Document.status != DocumentStatus.DISMISSED,
            or_(
                and_(
                    IngestBatch.id.isnot(None),
                    IngestBatch.status != IngestBatchStatus.COMPLETED,
                    IngestBatch.status != IngestBatchStatus.AWAITING_SLICING,
                ),
                Document.case_id == "_TRIAGE",
                and_(
                    Document.needs_review,
                    or_(
                        IngestBatch.id.is_(None),
                        IngestBatch.status != IngestBatchStatus.COMPLETED,
                    ),
                ),
            ),
        )
        .order_by(Document.ingest_date.desc())
        .all()
    )

    bundles: dict[str, BundleView] = {}
    for doc in docs:
        if doc.ingest_batch_id and doc.ingest_batch:
            key = f"batch-{doc.ingest_batch_id}"
            if key not in bundles:
                batch = doc.ingest_batch
                confirmed = (
                    batch.case_id
                    if batch.case_id and batch.case_id != "_TRIAGE"
                    else None
                )
                bundle_kwargs = {
                    "key": key,
                    "batch_id": batch.id,
                    "source_type": batch.source_type,
                    "subject": batch.subject,
                    "sender_email": batch.sender_email,
                    "received_at": batch.received_at,
                    "confirmed_case_id": confirmed,
                    "proceeding": batch.proceeding,
                }
                if include_sub_groups:
                    bundle_kwargs["sub_groups"] = list(batch.sub_groups or [])
                bundles[key] = BundleView(**bundle_kwargs)
            bundles[key].documents.append(doc)
            bundle = bundles[key]
            if (
                not bundle.confirmed_case_id
                and doc.case_id
                and doc.case_id != "_TRIAGE"
            ):
                bundle.suggested_case_id = doc.case_id
        else:
            key = f"loose-{doc.id}"
            confirmed = (
                doc.case_id if doc.case_id and doc.case_id != "_TRIAGE" else None
            )
            bundles[key] = BundleView(
                key=key,
                batch_id=None,
                source_type=IngestBatchSourceType.MANUAL,
                subject=doc.title,
                sender_email=None,
                received_at=doc.ingest_date or datetime.now(),
                confirmed_case_id=confirmed,
                proceeding=doc.proceeding,
                documents=[doc],
            )

    return bundles


def get_triage_bundles(
    db: Session,
    limit: int = 50,
    offset: int = 0,
    sort: str = "received",
    direction: str = "desc",
    case_ids: list[str] | None = None,
    proceeding_ids: list[str] | None = None,
    pipeline_filters: list[str] | None = None,
) -> list[BundleView]:
    """All triage documents grouped into bundles."""
    bundles = _build_bundles(db, include_sub_groups=True)

    _STATUS_ORDER = {
        "stuck": 0,
        "needs_classification": 1,
        "needs_review": 2,
        "processing": 3,
    }

    if sort == "docs":
        ordered = sorted(
            bundles.values(),
            key=lambda b: b.doc_count,
            reverse=(direction == "desc"),
        )
    elif sort == "status":
        ordered = sorted(
            bundles.values(),
            key=lambda b: _STATUS_ORDER.get(b.mock_status, 99),
            reverse=(direction == "desc"),
        )
    else:
        # "received" (default) — urgency-first, recency as tiebreaker.
        ordered = sorted(
            bundles.values(),
            key=lambda b: (
                0 if (b.unresolved_review_count > 0 or b.to_confirm_count > 0) else 1,
                -(b.received_at.timestamp() if b.received_at else 0),
            ),
            reverse=(direction == "asc"),
        )

    if case_ids:
        ordered = [
            b
            for b in ordered
            if b.confirmed_case_id in case_ids or b.suggested_case_id in case_ids
        ]
    if proceeding_ids:
        unassigned = "unassigned" in proceeding_ids
        pid_ints = {int(p) for p in proceeding_ids if p != "unassigned"}
        ordered = [
            b
            for b in ordered
            if (unassigned and not b.proceeding)
            or (b.proceeding and b.proceeding.id in pid_ints)
        ]
    if pipeline_filters:
        ordered = [b for b in ordered if _bundle_pipeline_label(b) in pipeline_filters]

    for bundle in ordered:
        enrich_bundle(db, bundle)

    return ordered[offset : offset + limit]


def get_triage_filter_options(db: Session) -> dict:
    """Return filter option lists derived from the live triage queue.

    Returns a dict with keys:
      case_options:       list of (case_id, label) sorted by case_id
      proceeding_options: list of (proceeding.id, label) sorted by label
      pipeline_options:   list of (value, display_label) for pipeline states
                          present in the queue, in canonical display order
    Only options that have at least one matching bundle are included.
    """
    bundles = _build_bundles(db, include_sub_groups=False)

    case_ids: dict[str, str] = {}
    proceeding_opts: dict[int, str] = {}
    pipeline_labels_present: set[str] = set()

    for b in bundles.values():
        cid = b.confirmed_case_id or b.suggested_case_id
        if cid and cid != "_TRIAGE":
            case_ids[cid] = cid

        if b.proceeding:
            proc = b.proceeding
            label = (
                f"{proc.court_name} · {proc.az_court}"
                if proc.az_court
                else proc.court_name
            )
            proceeding_opts[str(proc.id)] = label
        else:
            proceeding_opts["unassigned"] = "Unassigned"

        pipeline_labels_present.add(_bundle_pipeline_label(b))

    case_options = sorted(case_ids.items(), key=lambda x: x[0])
    proceeding_options = sorted(
        proceeding_opts.items(),
        key=lambda x: ("" if x[0] == "unassigned" else x[1]),
    )
    canonical_pipeline = [
        ("ready", "✓ ready"),
        ("review_metadata", "review metadata"),
        ("processing", "processing"),
        ("failed", "failed"),
    ]
    pipeline_options = [
        (value, label)
        for value, label in canonical_pipeline
        if value in pipeline_labels_present
    ]

    return {
        "case_options": case_options,
        "proceeding_options": proceeding_options,
        "pipeline_options": pipeline_options,
    }


def enrich_bundle(db: Session, bundle: BundleView) -> None:
    """Sort documents and resolve action items, proof edges, and case metadata in-place."""
    from app.models.database import ActionItem

    has_manual_groups = bundle.batch_id and (
        db.query(BatchSubGroup)
        .filter(BatchSubGroup.batch_id == bundle.batch_id)
        .limit(1)
        .count()
        > 0
    )
    if not has_manual_groups:
        # Significance-first within the bundle (§5b), cover-letter as in-tier
        # tiebreaker so the user's eye lands on high-impact docs first.
        bundle.documents.sort(
            key=lambda d: (
                _SIG_ORDER.get(d.significance_tier, 99),
                0 if d.role == DocumentRole.COVER_LETTER else 1,
                d.ingest_date or datetime.min,
            )
        )
    doc_ids = [d.id for d in bundle.documents]
    if doc_ids:
        bundle.action_items = (
            db.query(ActionItem)
            .filter(ActionItem.source_document_id.in_(doc_ids))
            .order_by(ActionItem.due_date.asc())
            .all()
        )
        proof_rels = (
            db.query(DocumentRelationship)
            .filter(
                DocumentRelationship.to_document_id.in_(doc_ids),
                DocumentRelationship.relationship_type
                == RelationshipType.ATTACHES_AS_PROOF,
            )
            .all()
        )
        bundle.proof_doc_ids = {r.to_document_id for r in proof_rels}

    # Resolve suggested case metadata for the single-button confirm UX.
    if bundle.suggested_case_id and not bundle.confirmed_case_id:
        _case = db.query(Case).filter(Case.id == bundle.suggested_case_id).first()
        if _case:
            bundle.suggested_case_exists = True
            bundle.suggested_case_title = _sanitize_case_title(
                _case.title, bundle.suggested_case_id, bundle.subject
            )
            bundle.suggested_case_is_draft = bool(_case.is_draft)

    # When AI auto-created a draft case and cascaded it to the batch,
    # confirmed_case_id is set but is_draft=True — it hasn't been ratified.
    # Re-cast it as suggested so the footer shows "Confirm case <ID>" and
    # the modal opens pre-filled rather than as a blank create-new form.
    if bundle.confirmed_case_id and not bundle.suggested_case_id:
        _case = db.query(Case).filter(Case.id == bundle.confirmed_case_id).first()
        if _case and _case.is_draft:
            bundle.suggested_case_id = bundle.confirmed_case_id
            bundle.suggested_case_title = _sanitize_case_title(
                _case.title, bundle.confirmed_case_id, bundle.subject
            )
            bundle.suggested_case_is_draft = True
            bundle.suggested_case_exists = True
            bundle.confirmed_case_id = None
        elif _case and not _case.is_draft:
            bundle.suggested_case_exists = True
            bundle.suggested_case_title = _sanitize_case_title(
                _case.title, bundle.confirmed_case_id, bundle.subject
            )


def get_slicing_queue(db: Session) -> list:
    """Batches awaiting document slicing review."""
    return (
        db.query(IngestBatch)
        .filter(IngestBatch.status == IngestBatchStatus.AWAITING_SLICING)
        .order_by(IngestBatch.received_at.desc())
        .all()
    )


def get_bundle_by_batch_id(db: Session, batch_id: int) -> BundleView | None:
    """Return a BundleView for a single batch without rebuilding the full triage feed."""
    batch = (
        db.query(IngestBatch)
        .options(
            joinedload(IngestBatch.proceeding),
            joinedload(IngestBatch.sub_groups),
        )
        .filter(IngestBatch.id == batch_id)
        .first()
    )
    if not batch:
        return None

    docs = (
        db.query(Document)
        .options(
            joinedload(Document.proceeding),
            joinedload(Document.sub_group),
        )
        .filter(Document.ingest_batch_id == batch_id)
        .order_by(Document.ingest_date.desc())
        .all()
    )

    confirmed = batch.case_id if batch.case_id and batch.case_id != "_TRIAGE" else None
    bundle = BundleView(
        key=f"batch-{batch.id}",
        batch_id=batch.id,
        source_type=batch.source_type,
        subject=batch.subject,
        sender_email=batch.sender_email,
        received_at=batch.received_at,
        confirmed_case_id=confirmed,
        proceeding=batch.proceeding,
        documents=docs,
        sub_groups=list(batch.sub_groups or []),
    )
    for doc in docs:
        if not bundle.confirmed_case_id and doc.case_id and doc.case_id != "_TRIAGE":
            bundle.suggested_case_id = doc.case_id

    enrich_bundle(db, bundle)
    return bundle
