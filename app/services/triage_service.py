"""TriageService — bundle-aware triage queue and batch confirmation.

Groups triage documents by `ingest_batch_id`, falling back to one synthetic
bundle per unbatched document (for historical data created before IngestBatch
wiring landed). Owns the single-doc and whole-bundle confirmation transactions,
and the user-reaction upsert used by the Reaction Bar.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.database import (
    Document,
    DocumentRelationship,
    IngestBatch,
    UserReaction,
)
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    RelationshipType,
    SignificanceTier,
    UserReactionType,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.document import DocumentRepository
from app.repositories.ingest_batch import IngestBatchRepository
from app.repositories.user_reaction import UserReactionRepository

_SIG_ORDER: dict = {
    SignificanceTier.CRITICAL: 0,
    SignificanceTier.SIGNIFICANT: 1,
    SignificanceTier.INFORMATIONAL: 2,
    SignificanceTier.ADMINISTRATIVE: 3,
}


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
    proceeding: object | None = None  # Proceeding ORM instance if known
    # Set of doc IDs that are targets of ATTACHES_AS_PROOF edges (→ [proof] pill)
    proof_doc_ids: set = field(default_factory=set)
    documents: list[Document] = field(default_factory=list)
    action_items: list = field(default_factory=list)

    @property
    def doc_count(self) -> int:
        return len(self.documents)

    @property
    def needs_review_count(self) -> int:
        return sum(1 for d in self.documents if d.needs_review)

    @property
    def is_synthetic(self) -> bool:
        return self.batch_id is None

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


class TriageService:
    def __init__(self, db: Session):
        self.db = db
        self.doc_repo = DocumentRepository(db)
        self.batch_repo = IngestBatchRepository(db)
        self.reaction_repo = UserReactionRepository(db)
        self.action_repo = ActionItemRepository(db)

    # --- reads ----------------------------------------------------------------

    def get_triage_bundles(self, limit: int = 50, offset: int = 0) -> list[BundleView]:
        """All triage documents grouped into bundles."""
        from sqlalchemy import or_

        from app.models.database import ActionItem, IngestBatch, IngestBatchStatus

        # Batch subquery: any batch that is NOT completed and NOT awaiting slicing is in triage.
        # AWAITING_SLICING batches have no Documents yet and appear in the slicing queue instead.
        unresolved_batches_subq = (
            self.db.query(IngestBatch.id)
            .filter(
                IngestBatch.status != IngestBatchStatus.COMPLETED,
                IngestBatch.status != IngestBatchStatus.AWAITING_SLICING,
            )
            .scalar_subquery()
        )

        docs = (
            self.db.query(Document)
            .options(
                joinedload(Document.ingest_batch).joinedload(IngestBatch.proceeding),
                joinedload(Document.proceeding),
            )
            .filter(
                or_(
                    Document.ingest_batch_id.in_(unresolved_batches_subq),
                    Document.case_id == "_TRIAGE",
                    Document.needs_review,
                )
            )
            .order_by(Document.created_at.desc())
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
                    received_at=doc.created_at or datetime.now(),
                    confirmed_case_id=confirmed,
                    proceeding=doc.proceeding,
                    documents=[doc],
                )

        # Urgency-first: bundles with more review flags float to the top.
        # Triage is a strategy session — surface what needs attention before
        # what's just recent. Ties broken by recency.
        ordered = sorted(
            bundles.values(),
            key=lambda b: (
                0 if b.needs_review_count > 0 else 1,
                -b.received_at.timestamp(),
            ),
        )

        for bundle in ordered:
            # Significance-first within the bundle (§5b), cover-letter as in-tier
            # tiebreaker so the user's eye lands on high-impact docs first.
            bundle.documents.sort(
                key=lambda d: (
                    _SIG_ORDER.get(d.significance_tier, 99),
                    0 if d.role == DocumentRole.COVER_LETTER else 1,
                    d.created_at or datetime.min,
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
                # Build proof_doc_ids: docs that are the *target* of an
                # ATTACHES_AS_PROOF edge within this bundle.
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

        return ordered[offset : offset + limit]

    def get_slicing_queue(self) -> list:
        """Batches awaiting document slicing review."""
        from app.models.database import IngestBatch, IngestBatchStatus

        return (
            self.db.query(IngestBatch)
            .filter(IngestBatch.status == IngestBatchStatus.AWAITING_SLICING)
            .order_by(IngestBatch.received_at.desc())
            .all()
        )

    def get_reactions(self, document_id: int) -> Sequence[UserReaction]:
        return self.reaction_repo.get_by_document(document_id)

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
                .order_by(Document.created_at.asc())
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
        received_date: datetime | None = None,
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
        if received_date is not None:
            doc.received_date = received_date

        from app.services.ingestion.service import compute_review_reasons

        reasons = compute_review_reasons(doc)
        doc.review_reasons = reasons
        actionable = [r for r in reasons if r != "missing_parent"]
        doc.needs_review = bool(actionable)

        self.db.commit()
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
        from app.models.database import ActionItem
        from app.services.ingestion.service import compute_review_reasons

        doc_ids = [doc.id for doc in docs]
        for doc in docs:
            doc.case_id = case_id
            if proceeding_id is not None:
                doc.proceeding_id = proceeding_id
            reasons = compute_review_reasons(doc)
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
        self.db.refresh(batch)
        return batch

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
