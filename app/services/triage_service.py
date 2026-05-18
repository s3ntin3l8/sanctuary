"""TriageService — bundle-aware triage queue and batch confirmation.

Groups triage documents by `ingest_batch_id`, falling back to one synthetic
bundle per unbatched document (for historical data created before IngestBatch
wiring landed). Owns the single-doc and whole-bundle confirmation transactions,
and the user-reaction upsert used by the Reaction Bar.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import SIG_ORDER as _SIG_ORDER
from app.models.database import (
    BatchSubGroup,
    Document,
    IngestBatch,
    UserReaction,
)
from app.models.enums import (
    ActionItemStatus,
    DocumentRole,
    DocumentStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    UserReactionType,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.document import DocumentRepository
from app.repositories.ingest_batch import IngestBatchRepository
from app.repositories.user_reaction import UserReactionRepository
from app.services.pipeline_status import stages_dict


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
    """Deprecated shim — use triage_confirmation.reset_and_reenrich."""
    from app.services.triage_confirmation import reset_and_reenrich

    return reset_and_reenrich(db, docs)


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
        from app.services.triage_bundles import get_triage_bundles as _impl

        return _impl(
            self.db,
            limit=limit,
            offset=offset,
            sort=sort,
            direction=direction,
            case_ids=case_ids,
            proceeding_ids=proceeding_ids,
            pipeline_filters=pipeline_filters,
        )

    def get_triage_filter_options(self) -> dict:
        from app.services.triage_bundles import get_triage_filter_options as _impl

        return _impl(self.db)

    def enrich_bundle(self, bundle: BundleView) -> None:
        from app.services.triage_bundles import enrich_bundle as _impl

        return _impl(self.db, bundle)

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
        from app.services.triage_bundles import get_slicing_queue as _impl

        return _impl(self.db)

    def get_bundle_by_batch_id(self, batch_id: int) -> BundleView | None:
        from app.services.triage_bundles import get_bundle_by_batch_id as _impl

        return _impl(self.db, batch_id)

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

    # --- writes ---------------------------------------------------------------

    def find_next_review_doc(self, after_doc_id: int) -> Document | None:
        from app.services.triage_confirmation import find_next_review_doc as _impl

        return _impl(self.db, after_doc_id)

    def confirm_document(self, doc_id: int, **kwargs) -> Document | None:
        from app.services.triage_confirmation import confirm_document as _impl

        return _impl(self.db, doc_id, **kwargs)

    def confirm_bundle(
        self,
        batch_id: int,
        case_id: str,
        proceeding_id: int | None = None,
        finalize: bool = False,
    ) -> IngestBatch | None:
        from app.services.triage_confirmation import confirm_bundle as _impl

        return _impl(
            self.db, batch_id, case_id, proceeding_id=proceeding_id, finalize=finalize
        )

    def cleanup_orphaned_drafts(self) -> int:
        from app.services.triage_confirmation import cleanup_orphaned_drafts as _impl

        return _impl(self.db)

    def get_bundle_suggestion(
        self, batch_id: int | None = None, doc_id: int | None = None
    ) -> tuple[str | None, int | None]:
        from app.services.triage_confirmation import get_bundle_suggestion as _impl

        return _impl(self.db, batch_id=batch_id, doc_id=doc_id)

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
        from app.models.enums import IngestBatchStatus

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
