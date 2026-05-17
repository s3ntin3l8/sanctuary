"""TriageService — bundle-aware triage queue and batch confirmation.

Groups triage documents by `ingest_batch_id`, falling back to one synthetic
bundle per unbatched document (for historical data created before IngestBatch
wiring landed). Owns the single-doc and whole-bundle confirmation transactions,
and the user-reaction upsert used by the Reaction Bar.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.constants import SIG_ORDER as _SIG_ORDER
from app.models.database import (
    BatchSubGroup,
    Case,
    Document,
    DocumentRelationship,
    IngestBatch,
    UserReaction,
)
from app.models.enums import (
    ActionItemStatus,
    DocumentRole,
    DocumentStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    RelationshipType,
    UserReactionType,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.document import DocumentRepository
from app.repositories.ingest_batch import IngestBatchRepository
from app.repositories.user_reaction import UserReactionRepository


def _sanitize_case_title(
    title: str | None, case_id: str, bundle_subject: str | None
) -> str | None:
    """Return a display-worthy case title, or None when the raw title is useless.

    Discards titles that are identical to the case_id (AI echo-back) and
    re-derives from the bundle subject via the existing helper, falling back to
    None so the modal field is left blank for the user to fill in.
    """
    from app.services.case_service import _derive_case_title_from_subject

    if title and title.strip() != case_id:
        return title
    derived = _derive_case_title_from_subject(bundle_subject, case_id)
    return derived or None


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
    confirmed_case_id: str | None = None  # set after batch cascade
    suggested_case_id: str | None = None  # AI-suggested, awaiting confirmation
    suggested_case_title: str | None = (
        None  # title of the suggested case (or AI-derived)
    )
    suggested_case_is_draft: bool = False  # True when the suggested case is an AI draft
    suggested_case_exists: bool = (
        False  # True when a Case row exists for suggested_case_id
    )
    proceeding: object | None = None  # Proceeding ORM instance if known
    # Set of doc IDs that are targets of ATTACHES_AS_PROOF edges (→ [proof] pill)
    proof_doc_ids: set = field(default_factory=set)
    documents: list[Document] = field(default_factory=list)
    action_items: list = field(default_factory=list)
    sub_groups: list = field(default_factory=list)  # BatchSubGroup ORM rows

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
        """Human label for current pipeline activity, or None if nothing in flight.

        - 'Processing: <stage>' — any stage running or retrying anywhere in the
          bundle (lowest-order stage wins so the label tracks "what's happening
          right now" rather than what's furthest along).
        - 'Queued' — no stage running but stages remain pending (between-stage
          gap, or worker backlog).
        - None — fully terminal; caller decides what to render.
        """
        from app.services.pipeline_status import STAGE_REGISTRY

        order_map = {spec.stage.value: spec.order for spec in STAGE_REGISTRY.values()}
        running: list[str] = []
        pending: list[str] = []
        for doc in self.documents:
            for stage_name, info in (doc.pipeline_stages or {}).items():
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
        """True if any doc has an explicitly low/medium confidence on a tracked field.

        Uses explicit-only matching (no default fallback) so fields absent from
        extraction_confidence — e.g. significance_tier and document_type on older
        documents — don't wrongly trigger the 'review metadata' badge.
        """
        tracked = (
            "originator",
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
        """Filter-chip taxonomy for the redesigned triage page.

        See `app.services.triage_view.mock_status` for precedence rules.
        Cached lazily on first access.
        """
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

        Vision.md §1 shows one email delivering multiple "bundles" — each
        being a cover letter with its own enclosures. In our model that's
        multiple parent_id=None docs within a single IngestBatch. This
        property returns one list per parent-root, each entry a `(depth,
        doc)` tuple in BFS order so the template can indent enclosures
        consistently no matter how deep the tree goes.

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


def ensure_sub_groups_initialized(batch_id: int, db: Session) -> list[BatchSubGroup]:
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


def _reset_and_reenrich(db: Session, docs: list) -> None:
    """Reset ENRICH (and its downstream) to pending, then dispatch enrichment.

    Called whenever docs transition from _TRIAGE to a real case so that
    relationship/claims/entities stages run with the correct case context.
    Only processes docs whose METADATA completed successfully (failed metadata
    means no enrichment output would be meaningful).
    """
    from app.models.enums import PipelineStage
    from app.services.pipeline_status import reset_stage
    from app.tasks.enrich_document import enrich_document_task

    for doc in docs:
        metadata_status = (doc.pipeline_stages or {}).get("metadata", {}).get("status")
        if metadata_status != "completed":
            continue
        reset_stage(doc.id, PipelineStage.ENRICH, db)
        enrich_document_task.delay(doc.id)


def _bundle_pipeline_label(bundle: "BundleView") -> str:
    """Pipeline aggregate state — matches the chip labels in _pipeline_aggregate.html.

    Returns: 'ready', 'review_metadata', 'processing', or 'failed'
    """
    summary = bundle.pipeline_summary
    if summary.get("failed", 0) > 0:
        return "failed"
    if summary.get("running", 0) + summary.get("pending", 0) > 0:
        return "processing"
    if bundle.has_unconfirmed_metadata:
        return "review_metadata"
    return "ready"


class TriageService:
    def __init__(self, db: Session):
        self.db = db
        self.doc_repo = DocumentRepository(db)
        self.batch_repo = IngestBatchRepository(db)
        self.reaction_repo = UserReactionRepository(db)
        self.action_repo = ActionItemRepository(db)

    # --- reads ----------------------------------------------------------------

    def get_triage_bundles(
        self,
        limit: int = 50,
        offset: int = 0,
        sort: str = "received",
        direction: str = "desc",
        case_ids: list[str] | None = None,
        proceeding_ids: list[str] | None = None,
        pipeline_filters: list[str] | None = None,
    ) -> list[BundleView]:
        """All triage documents grouped into bundles."""
        from sqlalchemy import and_, or_
        from sqlalchemy.orm import contains_eager

        from app.models.database import IngestBatch, IngestBatchStatus

        # A doc shows up in triage when at least one of these holds:
        #   1. its batch is still open (not COMPLETED, not AWAITING_SLICING)
        #   2. it's still parked under _TRIAGE
        #   3. it has actionable review reasons AND its batch (if any) is still
        #      open. The batch-status guard on (3) is the bug fix: previously
        #      `needs_review` alone pulled docs back into the feed even after
        #      the user clicked "Confirm bundle" (which set batch.status =
        #      COMPLETED). The needs_review flag still earns its keep on
        #      case-view UI; it just no longer overrides an explicit
        #      bundle-confirm in the triage feed.
        docs = (
            self.db.query(Document)
            .outerjoin(IngestBatch, Document.ingest_batch_id == IngestBatch.id)
            .options(
                contains_eager(Document.ingest_batch).joinedload(
                    IngestBatch.proceeding
                ),
                contains_eager(Document.ingest_batch).joinedload(
                    IngestBatch.sub_groups
                ),
                joinedload(Document.proceeding),
                joinedload(Document.sub_group),
            )
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
                    # Derive case chip state: confirmed = batch was cascaded to a
                    # real case; suggested = a doc already has an AI-extracted case_id
                    # that hasn't been cascaded to the batch yet.
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
                        sub_groups=list(batch.sub_groups or []),
                    )
                bundles[key].documents.append(doc)
                # Populate suggested_case_id from AI-extracted doc case_ids not yet cascaded
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
            # desc (default): reverse=False keeps urgency=0 first and newest-first via -timestamp key.
            # asc: reverse=True flips to oldest-first.
            ordered = sorted(
                bundles.values(),
                key=lambda b: (
                    0
                    if (b.unresolved_review_count > 0 or b.to_confirm_count > 0)
                    else 1,
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
            ordered = [
                b for b in ordered if _bundle_pipeline_label(b) in pipeline_filters
            ]

        for bundle in ordered:
            self.enrich_bundle(bundle)

        return ordered[offset : offset + limit]

    def get_triage_filter_options(self) -> dict:
        """Return filter option lists derived from the live triage queue.

        Returns a dict with keys:
          case_options:       list of (case_id, label) sorted by case_id
          proceeding_options: list of (proceeding.id, label) sorted by label
          pipeline_options:   list of (value, display_label) for pipeline states
                              present in the queue, in canonical display order
        Only options that have at least one matching bundle are included.
        """
        from sqlalchemy import and_, or_
        from sqlalchemy.orm import contains_eager

        from app.models.database import IngestBatch, IngestBatchStatus

        docs = (
            self.db.query(Document)
            .outerjoin(IngestBatch, Document.ingest_batch_id == IngestBatch.id)
            .options(
                contains_eager(Document.ingest_batch).joinedload(
                    IngestBatch.proceeding
                ),
                joinedload(Document.proceeding),
            )
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
                    bundles[key] = BundleView(
                        key=key,
                        batch_id=batch.id,
                        source_type=batch.source_type,
                        subject=batch.subject,
                        sender_email=batch.sender_email,
                        received_at=batch.received_at,
                        confirmed_case_id=confirmed,
                        proceeding=batch.proceeding,
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

        # Extract unique case options
        case_ids: dict[str, str] = {}
        proceeding_opts: dict[int, str] = {}
        pipeline_labels_present: set[str] = set()

        for b in bundles.values():
            # Case options
            cid = b.confirmed_case_id or b.suggested_case_id
            if cid and cid != "_TRIAGE":
                case_ids[cid] = cid

            # Proceeding options — key is str for URL compat; "unassigned" for no proceeding
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

            # Pipeline options
            pipeline_labels_present.add(_bundle_pipeline_label(b))

        # Build sorted case options
        case_options = sorted(case_ids.items(), key=lambda x: x[0])

        # Build sorted proceeding options — "Unassigned" first, then alphabetical by label
        proceeding_options = sorted(
            proceeding_opts.items(),
            key=lambda x: ("" if x[0] == "unassigned" else x[1]),
        )

        # Build pipeline options in canonical order (only those present)
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

    def enrich_bundle(self, bundle: BundleView) -> None:
        """Sort documents and resolve action items, proof edges, and case metadata in-place."""
        from app.models.database import ActionItem

        # Preserve manual sub_group_sort_order when manual groups exist.
        has_manual_groups = bundle.batch_id and (
            self.db.query(BatchSubGroup)
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
                self.db.query(ActionItem)
                .filter(ActionItem.source_document_id.in_(doc_ids))
                .order_by(ActionItem.due_date.asc())
                .all()
            )
            proof_rels = (
                self.db.query(DocumentRelationship)
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
            _case = (
                self.db.query(Case).filter(Case.id == bundle.suggested_case_id).first()
            )
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
            _case = (
                self.db.query(Case).filter(Case.id == bundle.confirmed_case_id).first()
            )
            if _case and _case.is_draft:
                bundle.suggested_case_id = bundle.confirmed_case_id
                bundle.suggested_case_title = _sanitize_case_title(
                    _case.title, bundle.confirmed_case_id, bundle.subject
                )
                bundle.suggested_case_is_draft = True
                bundle.suggested_case_exists = True
                bundle.confirmed_case_id = None
            elif _case and not _case.is_draft:
                # Case is already ratified — flag as existing so the footer
                # shows "Confirm case <ID>" rather than "Create case <ID>".
                bundle.suggested_case_exists = True
                bundle.suggested_case_title = _sanitize_case_title(
                    _case.title, bundle.confirmed_case_id, bundle.subject
                )

    def set_cover_letter(self, doc_id: int, batch_id: int) -> Document:
        """Mark doc_id as COVER_LETTER in its sub-group; clear previous cover in the same group."""
        ensure_sub_groups_initialized(batch_id, self.db)

        doc = (
            self.db.query(Document)
            .filter(Document.id == doc_id, Document.ingest_batch_id == batch_id)
            .first()
        )
        if not doc:
            raise ValueError(f"Document {doc_id} not in batch {batch_id}")

        if doc.sub_group_id is not None:
            self.db.query(Document).filter(
                Document.sub_group_id == doc.sub_group_id,
                Document.role == DocumentRole.COVER_LETTER,
            ).update({"role": DocumentRole.ENCLOSURE})
        else:
            self.db.query(Document).filter(
                Document.ingest_batch_id == batch_id,
                Document.sub_group_id.is_(None),
                Document.role == DocumentRole.COVER_LETTER,
            ).update({"role": DocumentRole.ENCLOSURE})

        doc.role = DocumentRole.COVER_LETTER
        self.db.flush()
        return doc

    def create_sub_group(self, batch_id: int) -> BatchSubGroup:
        """Create a new empty sub-group at the end of the batch's group list."""
        ensure_sub_groups_initialized(batch_id, self.db)

        max_order = (
            self.db.query(func.max(BatchSubGroup.sort_order))
            .filter(BatchSubGroup.batch_id == batch_id)
            .scalar()
        ) or 0

        sg = BatchSubGroup(batch_id=batch_id, label=None, sort_order=max_order + 1)
        self.db.add(sg)
        self.db.flush()
        return sg

    def rename_sub_group(
        self,
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
                self.db.query(BatchSubGroup)
                .filter(
                    BatchSubGroup.id == sub_group_id, BatchSubGroup.batch_id == batch_id
                )
                .first()
            )
            if not sg:
                raise ValueError(
                    f"SubGroup {sub_group_id} not found in batch {batch_id}"
                )
        else:
            ensure_sub_groups_initialized(batch_id, self.db)
            sg = None
            if lead_doc_id is not None:
                doc = self.db.get(Document, lead_doc_id)
                if doc and doc.sub_group_id:
                    sg = (
                        self.db.query(BatchSubGroup)
                        .filter(
                            BatchSubGroup.id == doc.sub_group_id,
                            BatchSubGroup.batch_id == batch_id,
                        )
                        .first()
                    )
            if not sg:
                raise ValueError(f"Cannot identify sub-group in batch {batch_id}")
        sg.label = label.strip() or None
        self.db.flush()
        return sg

    def reorder_documents(
        self,
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
        groups = ensure_sub_groups_initialized(batch_id, self.db)

        if target_sub_group_id is not None:
            sg = next((g for g in groups if g.id == target_sub_group_id), None)
            if not sg:
                raise ValueError(
                    f"SubGroup {target_sub_group_id} not in batch {batch_id}"
                )
        else:
            if lead_doc_id is not None:
                doc = self.db.get(Document, lead_doc_id)
                if doc and doc.sub_group_id:
                    target_sub_group_id = doc.sub_group_id
            if target_sub_group_id is None:
                target_sub_group_id = groups[0].id if groups else None
            if not target_sub_group_id:
                raise ValueError(f"No sub-groups in batch {batch_id}")

        for order, doc_id in enumerate(ordered_doc_ids):
            self.db.query(Document).filter(
                Document.id == doc_id,
                Document.ingest_batch_id == batch_id,
            ).update(
                {"sub_group_id": target_sub_group_id, "sub_group_sort_order": order}
            )

        self.db.flush()

    def delete_sub_group(
        self,
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
        groups = ensure_sub_groups_initialized(batch_id, self.db)

        if sub_group_id is not None:
            sg = next((g for g in groups if g.id == sub_group_id), None)
        else:
            sg = None
            if lead_doc_id is not None:
                doc = self.db.get(Document, lead_doc_id)
                if doc and doc.sub_group_id:
                    sg = next((g for g in groups if g.id == doc.sub_group_id), None)
        if not sg:
            raise ValueError(f"Cannot identify sub-group to delete in batch {batch_id}")

        remaining = sorted(
            (g for g in groups if g.id != sg.id), key=lambda g: g.sort_order
        )
        target = remaining[0] if remaining else None
        if target is not None:
            max_row = (
                self.db.query(Document.sub_group_sort_order)
                .filter(Document.sub_group_id == target.id)
                .order_by(Document.sub_group_sort_order.desc())
                .first()
            )
            next_order = (max_row[0] + 1) if max_row and max_row[0] is not None else 0
            moved = (
                self.db.query(Document)
                .filter(Document.sub_group_id == sg.id)
                .order_by(Document.sub_group_sort_order)
                .all()
            )
            for offset, doc in enumerate(moved):
                doc.sub_group_id = target.id
                doc.sub_group_sort_order = next_order + offset
            self.db.flush()

        self.db.delete(sg)
        self.db.flush()

    def reset_sub_groups(self, batch_id: int) -> None:
        """Remove all BatchSubGroup rows for this batch, reverting to auto mode.

        Clears sub_group_id and sub_group_sort_order from all docs in the batch.
        The batch_sub_groups rows are cascade-deleted when the BatchSubGroup ORM
        objects are deleted.
        """
        self.db.query(Document).filter(Document.ingest_batch_id == batch_id).update(
            {"sub_group_id": None, "sub_group_sort_order": None}
        )
        self.db.query(BatchSubGroup).filter(BatchSubGroup.batch_id == batch_id).delete()
        self.db.flush()

    def get_slicing_queue(self) -> list:
        """Batches awaiting document slicing review."""
        from app.models.database import IngestBatch, IngestBatchStatus

        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.status == IngestBatchStatus.AWAITING_SLICING)
            .order_by(IngestBatch.received_at.desc())
            .all()
        )

    def get_bundle_by_batch_id(self, batch_id: int) -> BundleView | None:
        """Return a BundleView for a single batch without rebuilding the full triage feed."""
        from app.models.database import IngestBatch

        batch = (
            self.db.query(IngestBatch)
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
            self.db.query(Document)
            .options(
                joinedload(Document.proceeding),
                joinedload(Document.sub_group),
            )
            .filter(Document.ingest_batch_id == batch_id)
            .order_by(Document.ingest_date.desc())
            .all()
        )

        confirmed = (
            batch.case_id if batch.case_id and batch.case_id != "_TRIAGE" else None
        )
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
            if (
                not bundle.confirmed_case_id
                and doc.case_id
                and doc.case_id != "_TRIAGE"
            ):
                bundle.suggested_case_id = doc.case_id

        self.enrich_bundle(bundle)
        return bundle

    def get_reactions(self, document_id: int) -> Sequence[UserReaction]:
        return self.reaction_repo.get_by_document(document_id)

    def get_reactions_by_doc_ids(
        self, document_ids: list[int]
    ) -> dict[int, set[UserReactionType]]:
        """Bulk variant of get_reactions for triage feed/bundle render.

        Returns ``{doc_id: {reaction, ...}}``. Docs with no reactions are absent
        from the dict — callers should default to ``set()``.
        """
        reactions = self.reaction_repo.get_by_document_ids(document_ids)
        out: dict[int, set[UserReactionType]] = {}
        for r in reactions:
            out.setdefault(r.document_id, set()).add(r.reaction)
        return out

    def get_action_items(self, document_id: int) -> list:
        return list(self.action_repo.get_by_source_document(document_id))

    def find_next_review_doc(self, after_doc_id: int) -> Document | None:
        """Find the next triage doc needing review after the given one.

        Sibling-first: prefer another doc in the same bundle. Otherwise, the
        first doc in the next bundle. Returns None when the queue is clear.
        """
        current = self.doc_repo.get(after_doc_id)
        if not current:
            return None

        # Sibling-first: same batch, still needs review, not the current doc.
        if current.ingest_batch_id:
            sibling = (
                self.db.query(Document)
                .filter(
                    Document.ingest_batch_id == current.ingest_batch_id,
                    Document.id != after_doc_id,
                    or_(Document.case_id == "_TRIAGE", Document.needs_review),
                )
                .order_by(Document.ingest_date.asc())
                .first()
            )
            if sibling:
                return sibling

        # Fall back to the next doc in the next bundle in the feed.
        bundles = self.get_triage_bundles()
        seen_current_bundle = False
        for bundle in bundles:
            # Skip the bundle we just cleared.
            if any(d.id == after_doc_id for d in bundle.documents):
                seen_current_bundle = True
                continue
            # Once we're past the current bundle, any needs_review doc works.
            if seen_current_bundle:
                for d in bundle.documents:
                    if d.needs_review or d.case_id == "_TRIAGE":
                        return d

        # If nothing after, fall back to the first needs_review doc anywhere
        # (in case we skipped over earlier bundles — rare but possible if
        # sort has changed).
        for bundle in bundles:
            for d in bundle.documents:
                if d.id != after_doc_id and (d.needs_review or d.case_id == "_TRIAGE"):
                    return d
        return None

    # --- writes ---------------------------------------------------------------

    def confirm_document(
        self,
        doc_id: int,
        *,
        title: str | None = None,
        case_id: str | None = None,
        originator_type=None,
        sender: str | None = None,
        internal_id: str | None = None,
        issued_date: datetime | None = None,
        received_date: datetime | None = None,
        significance_tier=None,
        document_type=None,
        finalize: bool = False,
    ) -> Document | None:
        """Apply metadata patch; optionally remove from triage."""
        doc = self.doc_repo.get(doc_id)
        if not doc:
            return None

        if title is not None:
            doc.title = title
        if case_id is not None:
            doc.case_id = case_id
        if originator_type is not None:
            doc.originator_type = originator_type
        if sender is not None:
            doc.sender = sender
        if internal_id is not None:
            doc.internal_id = internal_id or None
        if issued_date is not None:
            doc.issued_date = issued_date
        if received_date is not None:
            doc.received_date = received_date
        if significance_tier is not None:
            doc.significance_tier = significance_tier
        if document_type is not None:
            doc.document_type = document_type

        if finalize:
            conf = dict(doc.extraction_confidence or {})
            field_map = {
                "originator": originator_type,
                "sender": sender,
                "issued_date": issued_date,
                "significance_tier": significance_tier,
                "document_type": document_type,
            }
            for key, val in field_map.items():
                if val is not None:
                    conf[key] = "user_set"
            doc.extraction_confidence = conf

        from app.services.ingestion.service import compute_review_reasons

        reasons = compute_review_reasons(doc, confirmed=finalize)
        doc.review_reasons = reasons
        # Only clear needs_review if there are no reasons left (including pending_confirmation)
        # except for missing_parent which is non-blocking for triage removal.
        actionable = [r for r in reasons if r != "missing_parent"]
        doc.needs_review = bool(actionable)

        self.db.commit()
        # Sweep drafts whose last doc just moved away.
        self.cleanup_orphaned_drafts()
        self.db.refresh(doc)
        return doc

    def confirm_bundle(
        self,
        batch_id: int,
        case_id: str,
        proceeding_id: int | None = None,
        finalize: bool = False,
    ) -> IngestBatch | None:
        """Cascade case/proceeding assignment to every doc in the bundle.

        finalize=True marks the batch COMPLETED unconditionally (used by the
        explicit "Confirm bundle" action). finalize=False (the default, used by
        "Assign case") never touches batch status — the bundle stays in triage
        for further per-doc review.
        """
        batch = self.batch_repo.get(batch_id)
        if not batch:
            return None

        docs = (
            self.db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
        )
        from app.models.database import ActionItem, Case, Proceeding
        from app.services.ingestion.service import compute_review_reasons

        # If assigning to an AI-suggested draft case, promote it to a real case.
        case = self.db.query(Case).filter(Case.id == case_id).first()
        if case and case.is_draft:
            case.is_draft = False

        # Same for the chosen proceeding — once the user confirms it, it's no
        # longer a draft.
        if proceeding_id is not None:
            proc = (
                self.db.query(Proceeding).filter(Proceeding.id == proceeding_id).first()
            )
            if proc and proc.is_draft:
                proc.is_draft = False

        doc_ids = [doc.id for doc in docs]
        for doc in docs:
            doc.case_id = case_id
            if proceeding_id is not None:
                doc.proceeding_id = proceeding_id
            reasons = compute_review_reasons(doc, confirmed=finalize)
            doc.review_reasons = reasons
            actionable = [r for r in reasons if r != "missing_parent"]
            doc.needs_review = bool(actionable)

        # Cascade case/proceeding to ActionItems created during ingestion (Phase 4)
        # that are still parked under _TRIAGE pending bundle confirmation.
        if doc_ids:
            orphaned = self.db.query(ActionItem).filter(
                ActionItem.source_document_id.in_(doc_ids),
                ActionItem.case_id == "_TRIAGE",
            )
            for item in orphaned:
                item.case_id = case_id
                if proceeding_id is not None and item.proceeding_id is None:
                    item.proceeding_id = proceeding_id

        batch.case_id = case_id
        if proceeding_id is not None:
            batch.proceeding_id = proceeding_id

        # Mark batch completed only when explicitly finalizing.
        if finalize:
            batch.status = IngestBatchStatus.COMPLETED

        self.db.commit()
        # Sweep drafts whose last doc just moved away.
        self.cleanup_orphaned_drafts()
        self.db.refresh(batch)
        return batch

    def cleanup_orphaned_drafts(self) -> int:
        """Delete draft Case rows whose last document has moved away.

        Drafts are created at the METADATA pipeline stage when an AI-extracted
        internal_id can't be matched to an existing case. If the user later
        assigns the bundle elsewhere, the draft is left orphaned — invisible
        in the picker (filtered) but still cluttering the data. Cascades
        through to any proceedings the AI created alongside the draft.

        Returns the number of drafts deleted. Caller is responsible for the
        commit *after* their own changes — this method commits its own deletes.
        """
        from app.models.database import Case, Document

        # SQL: find drafts with zero remaining documents.
        orphaned = (
            self.db.query(Case)
            .outerjoin(Document, Document.case_id == Case.id)
            .filter(Case.is_draft.is_(True))
            .group_by(Case.id)
            .having(func.count(Document.id) == 0)
            .all()
        )
        for case in orphaned:
            self.db.delete(case)
        if orphaned:
            self.db.commit()
        return len(orphaned)

    def get_bundle_suggestion(
        self, batch_id: int | None = None, doc_id: int | None = None
    ) -> tuple[str | None, int | None]:
        """Return (suggested_case_id, suggested_proceeding_id) for a bundle.

        Used by batch confirm to obtain per-bundle suggestions without rebuilding
        the full triage feed. Returns (None, None) when no suggestion exists.
        """
        from app.models.database import IngestBatch

        if batch_id:
            batch = self.db.get(IngestBatch, batch_id)
            if not batch:
                return None, None
            # Suggested case: batch.case_id if it's a real (non-triage) case or a draft
            case_id = (
                batch.case_id if batch.case_id and batch.case_id != "_TRIAGE" else None
            )
            if not case_id:
                # Fall back to doc-level extraction that hasn't cascaded to batch yet
                doc = (
                    self.db.query(Document)
                    .filter(
                        Document.ingest_batch_id == batch_id,
                        Document.case_id.isnot(None),
                        Document.case_id != "_TRIAGE",
                    )
                    .first()
                )
                case_id = doc.case_id if doc else None
            proceeding_id = batch.proceeding_id if batch.proceeding_id else None
            return case_id, proceeding_id
        elif doc_id:
            doc = self.db.get(Document, doc_id)
            if not doc:
                return None, None
            case_id = doc.case_id if doc.case_id and doc.case_id != "_TRIAGE" else None
            proceeding_id = doc.proceeding_id if doc.proceeding_id else None
            return case_id, proceeding_id
        return None, None

    def toggle_reaction(
        self,
        document_id: int,
        reaction: UserReactionType,
        notes: str | None = None,
    ) -> UserReaction | None:
        """Idempotent: create if absent (returns new row), delete if present
        and notes is None (returns None), update notes if present and notes is
        provided (returns updated row)."""
        existing = self.reaction_repo.find(document_id, reaction)
        if existing and notes is None:
            self.reaction_repo.clear_reaction(document_id, reaction)
            self.db.commit()
            return None

        result = self.reaction_repo.set_reaction(document_id, reaction, notes)
        self.db.commit()
        return result

    def clear_reaction(self, document_id: int, reaction: UserReactionType) -> bool:
        cleared = self.reaction_repo.clear_reaction(document_id, reaction)
        if cleared:
            self.db.commit()
        return cleared

    def dismiss_bundle(
        self, batch_id: int | None = None, doc_id: int | None = None
    ) -> bool:
        """Mark a batch or loose document (and children) as DISMISSED."""
        from app.models.database import ActionItem, Document, IngestBatch
        from app.models.enums import DocumentStatus, IngestBatchStatus

        if batch_id:
            batch = self.db.get(IngestBatch, batch_id)
            if batch:
                batch.status = IngestBatchStatus.DISMISSED
                # Dismiss associated docs
                self.db.query(Document).filter(
                    Document.ingest_batch_id == batch_id
                ).update(
                    {"status": DocumentStatus.DISMISSED}, synchronize_session=False
                )
                # Dismiss associated ActionItems
                doc_ids = (
                    self.db.query(Document.id)
                    .filter(Document.ingest_batch_id == batch_id)
                    .all()
                )
                doc_id_list = [d[0] for d in doc_ids]
                if doc_id_list:
                    self.db.query(ActionItem).filter(
                        ActionItem.source_document_id.in_(doc_id_list)
                    ).update(
                        {"status": ActionItemStatus.DISMISSED},
                        synchronize_session=False,
                    )
                self.db.commit()
                return True
        elif doc_id:
            doc = self.db.get(Document, doc_id)
            if doc:
                doc.status = DocumentStatus.DISMISSED
                # Dismiss associated ActionItems
                self.db.query(ActionItem).filter(
                    ActionItem.source_document_id == doc_id
                ).update(
                    {"status": ActionItemStatus.DISMISSED}, synchronize_session=False
                )
                self.db.commit()
                return True
        return False

    def delete_bundle(
        self, batch_id: int | None = None, doc_id: int | None = None
    ) -> bool:
        """Hard-delete a batch (and all children + files) or a loose document.

        Raises ValueError when the batch is mid-flight (PROCESSING or
        AWAITING_SLICING). Caller maps to HTTP 409.
        """
        import logging
        import os

        from app.models.database import ActionItem, IngestBatch
        from app.services.document_service import DocumentService

        logger = logging.getLogger(__name__)

        if batch_id:
            batch = self.db.get(IngestBatch, batch_id)
            if not batch:
                return False
            if batch.status in (
                IngestBatchStatus.PROCESSING,
                IngestBatchStatus.AWAITING_SLICING,
            ):
                raise ValueError(
                    f"Cannot delete batch {batch_id} in {batch.status.value} state. "
                    "Wait for processing to finish, or retry the bundle first."
                )

            # Snapshot before per-doc loop: delete_document auto-removes the
            # batch row when it deletes the last document, so batch.* lookups
            # would fail on the final iteration.
            raw_source_path = batch.raw_source_path
            # Children-first order. Document.children carries
            # cascade="all, delete-orphan", so deleting a parent first triggers
            # an ORM cascade DELETE on its children before our manual
            # UserReaction / DocumentPin / DocumentRelationship cleanup runs
            # for them — tripping the FK guard with `FOREIGN KEY constraint
            # failed` on documents.id IN (...). Processing children before
            # their parent makes each delete_document call self-contained:
            # by the time the parent is deleted, no live children remain in
            # the session for the cascade to act on.
            sorted_docs = sorted(
                batch.documents, key=lambda d: (d.parent_id is None, d.id)
            )
            doc_id_list = [d.id for d in sorted_docs]

            # Hard-delete ActionItems sourced from this batch's docs while we
            # can still find them — delete_document nulls source_document_id.
            if doc_id_list:
                self.db.query(ActionItem).filter(
                    ActionItem.source_document_id.in_(doc_id_list)
                ).delete(synchronize_session=False)
                self.db.commit()

            doc_service = DocumentService(self.db)
            for did in doc_id_list:
                doc_service.delete_document(did)

            # Defensive: if the batch had zero docs, the per-doc loop never ran
            # and the batch row is still present. Drop it explicitly.
            if not doc_id_list:
                self.db.query(IngestBatch).filter(IngestBatch.id == batch_id).delete(
                    synchronize_session=False
                )
                self.db.commit()

            if raw_source_path and os.path.exists(raw_source_path):
                try:
                    os.remove(raw_source_path)
                except OSError as e:
                    logger.warning(
                        f"Failed to delete batch raw source {raw_source_path}: {e}"
                    )
            return True

        elif doc_id:
            return DocumentService(self.db).delete_document(doc_id)

        return False
