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

from app.models.database import Document, IngestBatch, UserReaction
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    UserReactionType,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.document import DocumentRepository
from app.repositories.ingest_batch import IngestBatchRepository
from app.repositories.user_reaction import UserReactionRepository


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
        docs = (
            self.db.query(Document)
            .options(joinedload(Document.ingest_batch))
            .filter(or_(Document.case_id == "_TRIAGE", Document.needs_review))
            .order_by(Document.created_at.desc())
            .all()
        )

        bundles: dict[str, BundleView] = {}
        for doc in docs:
            if doc.ingest_batch_id and doc.ingest_batch:
                key = f"batch-{doc.ingest_batch_id}"
                if key not in bundles:
                    batch = doc.ingest_batch
                    bundles[key] = BundleView(
                        key=key,
                        batch_id=batch.id,
                        source_type=batch.source_type,
                        subject=batch.subject,
                        sender_email=batch.sender_email,
                        received_at=batch.received_at,
                    )
                bundles[key].documents.append(doc)
            else:
                key = f"loose-{doc.id}"
                bundles[key] = BundleView(
                    key=key,
                    batch_id=None,
                    source_type=IngestBatchSourceType.MANUAL,
                    subject=doc.title,
                    sender_email=None,
                    received_at=doc.created_at or datetime.now(),
                    documents=[doc],
                )

        # Urgency-first: bundles with more review flags float to the top.
        # Triage is a strategy session — surface what needs attention before
        # what's just recent. Ties broken by recency.
        ordered = sorted(
            bundles.values(),
            key=lambda b: (-b.needs_review_count, -b.received_at.timestamp()),
        )

        for bundle in ordered:
            bundle.documents.sort(
                key=lambda d: (
                    0 if d.role == DocumentRole.COVER_LETTER else 1,
                    d.created_at or datetime.min,
                )
            )
            doc_ids = [d.id for d in bundle.documents]
            if doc_ids:
                from app.models.database import ActionItem

                bundle.action_items = (
                    self.db.query(ActionItem)
                    .filter(ActionItem.source_document_id.in_(doc_ids))
                    .order_by(ActionItem.due_date.asc())
                    .all()
                )

        return ordered[offset : offset + limit]

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

        has_real_case = bool(doc.case_id and doc.case_id != "_TRIAGE")
        if finalize and has_real_case:
            doc.needs_review = False
            doc.review_reasons = []
        else:
            doc.needs_review = len(reasons) > 0

        self.db.commit()
        self.db.refresh(doc)
        return doc

    def confirm_bundle(
        self,
        batch_id: int,
        case_id: str,
        proceeding_id: int | None = None,
    ) -> IngestBatch | None:
        """Cascade case/proceeding assignment to every doc in the bundle."""
        batch = self.batch_repo.get(batch_id)
        if not batch:
            return None

        docs = (
            self.db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
        )
        for doc in docs:
            doc.case_id = case_id
            if proceeding_id is not None:
                doc.proceeding_id = proceeding_id
            doc.needs_review = False
            doc.review_reasons = []

        batch.case_id = case_id
        if proceeding_id is not None:
            batch.proceeding_id = proceeding_id
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
