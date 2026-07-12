"""Triage feed reads + BundleView dataclass.

Free-function module. Defines `BundleView` (the per-row view-model used
across the triage feed) and the read functions that build BundleView lists
from the live document queue and hydrate them with action items, proof
edges, and case-metadata resolution.

See triage_confirmation.py for the confirmation flow, triage_subgroups.py for
sub-group CRUD, triage_reactions.py for reactions, and triage_dismissal.py
for dismiss/delete.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from app.constants import SIG_ORDER as _SIG_ORDER
from app.models.database import (
    BatchSubGroup,
    Case,
    Document,
    DocumentRelationship,
    IngestBatch,
    Proceeding,
)
from app.models.enums import (
    DocumentRole,
    DocumentStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    RelationshipType,
)
from app.services.pipeline_status import stages_dict
from app.services.triage_confirmation import _sanitize_case_title


@dataclass
class BundleView:
    """One row in the triage feed — either a real IngestBatch or a synthetic
    single-doc bundle for pre-batch-wiring documents."""

    key: str  # stable id for the bundle (e.g. "batch-7" or "loose-42")
    batch_id: int | None  # None for synthetic single-doc bundles
    source_type: IngestBatchSourceType
    subject: str | None
    sender_email: str | None
    received_at: datetime
    # Case chip state (§5a display rules)
    confirmed_case_id: str | None = None
    suggested_case_id: str | None = None
    suggested_case_title: str | None = None
    suggested_case_is_draft: bool = False
    suggested_case_exists: bool = False
    proceeding: Proceeding | None = None
    proof_doc_ids: set = field(default_factory=set)
    documents: list[Document] = field(default_factory=list)
    action_items: list = field(default_factory=list)
    sub_groups: list = field(default_factory=list)

    @property
    def doc_count(self) -> int:
        return len(self.documents)

    @property
    def total_pages(self) -> int:
        return sum(d.page_count or 0 for d in self.documents)

    @property
    def unresolved_review_count(self) -> int:
        """Docs with real review issues (low confidence, missing fields, etc.).

        Excludes docs whose only outstanding reason is `pending_confirmation`
        — those just need a human ratification click, not metadata fixes.
        """
        ignorable = {"pending_confirmation", "missing_parent"}
        return sum(
            1
            for d in self.documents
            if d.review_reasons and (set(d.review_reasons) - ignorable)
        )

    @property
    def to_confirm_count(self) -> int:
        """Docs whose only outstanding flag is `pending_confirmation`."""
        ignorable = {"pending_confirmation", "missing_parent"}
        return sum(
            1
            for d in self.documents
            if d.review_reasons
            and "pending_confirmation" in d.review_reasons
            and not (set(d.review_reasons) - ignorable)
        )

    @property
    def is_synthetic(self) -> bool:
        return self.batch_id is None

    @property
    def pipeline_summary(self) -> dict:
        """Aggregate pipeline_state counts for bundle header display."""
        from collections import Counter

        counts = Counter(
            (d.pipeline_state.value if d.pipeline_state else "pending")
            for d in self.documents
        )
        return {"total": len(self.documents), **counts}

    @property
    def pipeline_active_label(self) -> str | None:
        """Human label for current pipeline activity, or None if nothing in flight."""
        from app.services.pipeline_status import STAGE_REGISTRY

        order_map = {spec.stage.value: spec.order for spec in STAGE_REGISTRY.values()}
        running: list[str] = []
        pending: list[str] = []
        for doc in self.documents:
            for stage_name, info in stages_dict(doc).items():
                status = (info or {}).get("status")
                if status in {"running", "retrying"}:
                    running.append(stage_name)
                elif status == "pending":
                    pending.append(stage_name)

        if running:
            first = sorted(running, key=lambda n: order_map.get(n, 999))[0]
            return f"Processing: {first.replace('_', ' ')}"
        if pending:
            return "Queued"
        return None

    @property
    def has_unconfirmed_metadata(self) -> bool:
        """True if any doc has an explicitly low/medium confidence on a tracked field."""
        tracked = (
            "originator_type",
            "sender",
            "issued_date",
            "significance_tier",
            "document_type",
        )
        for doc in self.documents:
            conf = doc.extraction_confidence or {}
            for key in tracked:
                if conf.get(key) in ("low", "medium"):
                    return True
        return False

    @property
    def mock_status(self) -> str:
        """Filter-chip taxonomy for the redesigned triage page. Cached lazily."""
        if not hasattr(self, "_mock_status_cache"):
            from app.services.triage_view import mock_status

            self._mock_status_cache = mock_status(self)
        return self._mock_status_cache

    @property
    def sub_bundles(self) -> list:
        """Per parent-root subtree views for the inline expand + drawer spine.

        See `app.services.triage_view.build_sub_bundles`. Cached lazily.
        """
        if not hasattr(self, "_sub_bundles_cache"):
            from app.services.triage_view import build_sub_bundles

            self._sub_bundles_cache = build_sub_bundles(self)
        return self._sub_bundles_cache

    @property
    def parent_groups(self) -> list[list[tuple[int, Document]]]:
        """Group the bundle's documents by their parent-root subtree.

        Returns one list per parent-root, each entry a `(depth, doc)` tuple in
        BFS order so the template can indent enclosures consistently.

        A doc is a parent-root if its `parent_id` is None OR points to a
        document outside this bundle (orphaned child).
        """
        docs_by_id = {d.id: d for d in self.documents}
        children_of: dict[int, list[Document]] = {}
        roots: list[Document] = []
        for d in self.documents:
            if not d.parent_id or d.parent_id not in docs_by_id:
                roots.append(d)
            else:
                children_of.setdefault(d.parent_id, []).append(d)

        groups: list[list[tuple[int, Document]]] = []
        for root in roots:
            group: list[tuple[int, Document]] = [(0, root)]
            queue: list[tuple[int, Document]] = [
                (1, c) for c in children_of.get(root.id, [])
            ]
            while queue:
                depth, node = queue.pop(0)
                group.append((depth, node))
                queue.extend((depth + 1, c) for c in children_of.get(node.id, []))
            groups.append(group)
        return groups


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
    db: Session, include_sub_groups: bool = True, owner_id: int | None = None
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

    query = db.query(Document).outerjoin(
        IngestBatch, Document.ingest_batch_id == IngestBatch.id
    )
    if owner_id is not None:
        # Per-user triage: a user sees only documents they ingested. (Triage docs
        # have no case yet, so this is the right visibility lane — see the
        # Document.owner_id invariant.)
        query = query.filter(Document.owner_id == owner_id)
    docs = (
        query.options(*options)
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
                    Document.needs_review.is_(True),
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
                bundles[key] = BundleView(
                    key=key,
                    batch_id=batch.id,
                    source_type=batch.source_type,
                    subject=batch.subject,
                    sender_email=batch.sender_email,
                    received_at=batch.received_at,
                    confirmed_case_id=confirmed,
                    proceeding=batch.proceeding,
                    sub_groups=list(batch.sub_groups or [])
                    if include_sub_groups
                    else [],
                )
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
    enrich: bool = True,
    owner_id: int | None = None,
) -> list[BundleView]:
    """All triage documents grouped into bundles.

    When ``enrich`` is False, returns bundles without action items, proof
    edges, or resolved suggested-case metadata — only fields populated by
    `_build_bundles` (subject, documents, confirmed/suggested ids).
    Useful for header-stat callers that only need pipeline counts.

    ``owner_id`` restricts the feed to one user's ingested documents (the
    per-user triage inbox). None = unrestricted.
    """
    bundles = _build_bundles(db, include_sub_groups=True, owner_id=owner_id)

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

    visible = ordered[offset : offset + limit]
    if enrich:
        enrich_bundles(db, visible)
    return visible


def get_triage_filter_options(db: Session, owner_id: int | None = None) -> dict:
    """Return filter option lists derived from the live triage queue.

    Returns a dict with keys:
      case_options:       list of (case_id, label) sorted by case_id
      proceeding_options: list of (proceeding.id, label) sorted by label
      pipeline_options:   list of (value, display_label) for pipeline states
                          present in the queue, in canonical display order
    Only options that have at least one matching bundle are included.
    """
    bundles = _build_bundles(db, include_sub_groups=False, owner_id=owner_id)

    case_ids: dict[str, str] = {}
    proceeding_opts: dict[str, str] = {}
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
        key=lambda x: "" if x[0] == "unassigned" else x[1],
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


def enrich_bundles(db: Session, bundles: list[BundleView]) -> None:
    """Sort documents and resolve action items, proof edges, and case metadata
    in-place across many bundles in batched queries.

    One query each for BatchSubGroup presence, ActionItem, DocumentRelationship
    proof edges, and Case rows — regardless of bundle count.
    """
    if not bundles:
        return

    from app.models.database import ActionItem

    # --- Batch 1: BatchSubGroup presence by batch_id -----------------------
    batch_ids = [b.batch_id for b in bundles if b.batch_id]
    batches_with_groups: set[int] = set()
    if batch_ids:
        rows = (
            db.query(BatchSubGroup.batch_id)
            .filter(BatchSubGroup.batch_id.in_(batch_ids))
            .distinct()
            .all()
        )
        batches_with_groups = {r[0] for r in rows}

    # --- Batch 2: Action items by source document --------------------------
    all_doc_ids = [d.id for b in bundles for d in b.documents]
    action_items_by_doc: dict[int, list] = {}
    proof_doc_ids_set: set[int] = set()
    if all_doc_ids:
        items = (
            db.query(ActionItem)
            .filter(ActionItem.source_document_id.in_(all_doc_ids))
            .order_by(ActionItem.due_date.asc().nullslast())
            .all()
        )
        for item in items:
            # source_document_id can't be None here: the query above filters it
            # to be in all_doc_ids (a set of real document ids).
            if item.source_document_id is not None:
                action_items_by_doc.setdefault(item.source_document_id, []).append(item)

        # --- Batch 3: Proof-of relationships ------------------------------
        proof_rows = (
            db.query(DocumentRelationship.to_document_id)
            .filter(
                DocumentRelationship.to_document_id.in_(all_doc_ids),
                DocumentRelationship.relationship_type
                == RelationshipType.ATTACHES_AS_PROOF,
            )
            .all()
        )
        proof_doc_ids_set = {r[0] for r in proof_rows}

    # --- Batch 4: Cases (suggested + confirmed) ----------------------------
    case_ids_to_resolve: set[str] = set()
    for b in bundles:
        if b.suggested_case_id:
            case_ids_to_resolve.add(b.suggested_case_id)
        if b.confirmed_case_id:
            case_ids_to_resolve.add(b.confirmed_case_id)
    cases_by_id: dict[str, Case] = {}
    if case_ids_to_resolve:
        for c in db.query(Case).filter(Case.id.in_(case_ids_to_resolve)).all():
            cases_by_id[c.id] = c

    # --- Apply per-bundle in Python ---------------------------------------
    for bundle in bundles:
        has_manual_groups = bool(
            bundle.batch_id and bundle.batch_id in batches_with_groups
        )
        if not has_manual_groups:
            # Significance-first within the bundle (§5b), cover-letter as in-tier
            # tiebreaker so the user's eye lands on high-impact docs first.
            bundle.documents.sort(
                key=lambda d: (
                    _SIG_ORDER.get(d.significance_tier, 99)
                    if d.significance_tier is not None
                    else 99,
                    0 if d.role == DocumentRole.COVER_LETTER else 1,
                    d.ingest_date or datetime.min.replace(tzinfo=UTC),
                )
            )
        doc_ids = [d.id for d in bundle.documents]
        if doc_ids:
            bundle_items: list = []
            for did in doc_ids:
                bundle_items.extend(action_items_by_doc.get(did, []))
            # Maintain global due-date order: items came pre-sorted; merging
            # per-doc lists may interleave so resort.
            bundle_items.sort(key=lambda x: (x.due_date is None, x.due_date))
            bundle.action_items = bundle_items
            bundle.proof_doc_ids = {did for did in doc_ids if did in proof_doc_ids_set}

        # Resolve suggested case metadata for the single-button confirm UX.
        if bundle.suggested_case_id and not bundle.confirmed_case_id:
            _case = cases_by_id.get(bundle.suggested_case_id)
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
            _case = cases_by_id.get(bundle.confirmed_case_id)
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


def enrich_bundle(db: Session, bundle: BundleView) -> None:
    """Single-bundle wrapper around `enrich_bundles`."""
    enrich_bundles(db, [bundle])


def get_slicing_queue(db: Session, owner_id: int | None = None) -> list:
    """Batches awaiting document slicing review (per-user when owner_id given)."""
    query = db.query(IngestBatch).filter(
        IngestBatch.status == IngestBatchStatus.AWAITING_SLICING
    )
    if owner_id is not None:
        query = query.filter(IngestBatch.owner_id == owner_id)
    return query.order_by(IngestBatch.received_at.desc()).all()


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
