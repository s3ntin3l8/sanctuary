"""TriageService — bundle-aware triage queue and batch confirmation.

Groups triage documents by `ingest_batch_id`, falling back to one synthetic
bundle per unbatched document (for historical data created before IngestBatch
wiring landed). Owns the single-doc and whole-bundle confirmation transactions,
and the user-reaction upsert used by the Reaction Bar.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import (
    BatchSubGroup,
    Document,
    IngestBatch,
    UserReaction,
)
from app.models.enums import (
    IngestBatchSourceType,
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

    # --- sub-groups -----------------------------------------------------------

    def set_cover_letter(self, doc_id: int, batch_id: int) -> Document:
        from app.services.triage_subgroups import set_cover_letter as _impl

        return _impl(self.db, doc_id, batch_id)

    def create_sub_group(self, batch_id: int) -> BatchSubGroup:
        from app.services.triage_subgroups import create_sub_group as _impl

        return _impl(self.db, batch_id)

    def rename_sub_group(
        self,
        sub_group_id: int | None,
        batch_id: int,
        label: str,
        lead_doc_id: int | None = None,
    ) -> BatchSubGroup:
        from app.services.triage_subgroups import rename_sub_group as _impl

        return _impl(self.db, sub_group_id, batch_id, label, lead_doc_id=lead_doc_id)

    def reorder_documents(
        self,
        batch_id: int,
        ordered_doc_ids: list[int],
        target_sub_group_id: int | None,
        lead_doc_id: int | None = None,
    ) -> None:
        from app.services.triage_subgroups import reorder_documents as _impl

        return _impl(
            self.db,
            batch_id,
            ordered_doc_ids,
            target_sub_group_id,
            lead_doc_id=lead_doc_id,
        )

    def delete_sub_group(
        self,
        sub_group_id: int | None,
        batch_id: int,
        lead_doc_id: int | None = None,
    ) -> None:
        from app.services.triage_subgroups import delete_sub_group as _impl

        return _impl(self.db, sub_group_id, batch_id, lead_doc_id=lead_doc_id)

    def reset_sub_groups(self, batch_id: int) -> None:
        from app.services.triage_subgroups import reset_sub_groups as _impl

        return _impl(self.db, batch_id)

    def get_slicing_queue(self) -> list:
        from app.services.triage_bundles import get_slicing_queue as _impl

        return _impl(self.db)

    def get_bundle_by_batch_id(self, batch_id: int) -> BundleView | None:
        from app.services.triage_bundles import get_bundle_by_batch_id as _impl

        return _impl(self.db, batch_id)

    def get_reactions(self, document_id: int) -> Sequence[UserReaction]:
        from app.services.triage_reactions import get_reactions as _impl

        return _impl(self.db, document_id)

    def get_reactions_by_doc_ids(
        self, document_ids: list[int]
    ) -> dict[int, set[UserReactionType]]:
        from app.services.triage_reactions import get_reactions_by_doc_ids as _impl

        return _impl(self.db, document_ids)

    def get_action_items(self, document_id: int) -> list:
        from app.services.triage_reactions import get_action_items as _impl

        return _impl(self.db, document_id)

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
        from app.services.triage_reactions import toggle_reaction as _impl

        return _impl(self.db, document_id, reaction, notes=notes)

    def clear_reaction(self, document_id: int, reaction: UserReactionType) -> bool:
        from app.services.triage_reactions import clear_reaction as _impl

        return _impl(self.db, document_id, reaction)

    # --- dismissal ------------------------------------------------------------

    def dismiss_bundle(
        self, batch_id: int | None = None, doc_id: int | None = None
    ) -> bool:
        from app.services.triage_dismissal import dismiss_bundle as _impl

        return _impl(self.db, batch_id=batch_id, doc_id=doc_id)

    def delete_bundle(
        self, batch_id: int | None = None, doc_id: int | None = None
    ) -> bool:
        from app.services.triage_dismissal import delete_bundle as _impl

        return _impl(self.db, batch_id=batch_id, doc_id=doc_id)
