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

from app.constants import SIG_ORDER as _SIG_ORDER  # noqa: E402
from app.models.database import (
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

        for bundle in ordered:
            self.enrich_bundle(bundle)

        return ordered[offset : offset + limit]

    def enrich_bundle(self, bundle: BundleView) -> None:
        """Sort documents and resolve action items, proof edges, and case metadata in-place."""
        from app.models.database import ActionItem

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
            .options(joinedload(IngestBatch.proceeding))
            .filter(IngestBatch.id == batch_id)
            .first()
        )
        if not batch:
            return None

        docs = (
            self.db.query(Document)
            .options(joinedload(Document.proceeding))
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
