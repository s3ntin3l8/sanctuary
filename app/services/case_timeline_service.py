"""CaseTimelineService — builds the unified chronological event stream for the timeline tab.

Merges Document / ActionItem / LegalCost / Proceeding rows into a sorted list of
TimelineEvent dataclasses plus pre-computed bucketing metadata (month counts, quiet gaps).
No persistence — pure aggregation + sort.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.timezone import naive_utc_now
from app.models.database import (
    ActionItem,
    ClaimEvidence,
    Document,
    DocumentRelationship,
    LegalCost,
    Proceeding,
)
from app.models.enums import (
    ActionItemType,
    CostCategory,
    CostStatus,
    DocumentType,
)
from app.services.case_graph_service import _lane_for

logger = logging.getLogger(__name__)

# Cost categories that represent money flowing *out* (debits)
_DEBIT_CATEGORIES = {
    CostCategory.GERICHTSKOSTEN,
    CostCategory.ANWALTSKOSTEN,
    CostCategory.SACHVERSTAENDIGER,
    CostCategory.VORSCHUSS,
    CostCategory.VOLLSTRECKUNG,
    CostCategory.AUSLAGEN,
}

# DocumentType → timeline kind
_DOC_KIND: dict[DocumentType, str] = {
    DocumentType.RULING: "order",
    DocumentType.MOTION: "filing",
    DocumentType.STATEMENT: "statement",
    DocumentType.REPORT: "report",
    DocumentType.RELAY: "relay",
    DocumentType.INVOICE: "payment",
}

# Fallback kind per actor lane when DocumentType gives no direct mapping
_ACTOR_KIND_FALLBACK: dict[str, str] = {
    "own": "filing",
    "court": "order",
    "opposing": "statement",
    "third": "report",
    "unknown": "filing",
}


@dataclass(frozen=True)
class TimelineEvent:
    id: str  # globally unique within case, e.g. "doc-42", "action-7", "cost-3"
    date: datetime  # sort key; naive UTC like all DB datetimes
    actor: str  # own | court | opposing | third | unknown
    kind: str  # filing | order | statement | report | relay | payment
    #           | hearing | deadline | pending | milestone
    title: str
    sig: (
        str | None
    )  # critical | significant | informational | administrative | milestone
    source_document_id: int | None  # set → row is HUD-clickable
    note: str | None  # short subtext (relay description, etc.)
    amount_eur: float | None  # payment events
    direction: str | None  # debit | credit
    is_overdue: bool  # ActionItem: OPEN + due_date < today
    is_future: bool  # date > today
    claim_count: int = 0  # distinct claims linked to this document
    rel_count: int = 0  # document relationships (from + to)


def _kind_for_doc(doc: Document, actor: str) -> str:
    if doc.document_type is not None:
        mapped = _DOC_KIND.get(doc.document_type)
        if mapped is not None:
            return mapped
    return _ACTOR_KIND_FALLBACK.get(actor, "filing")


def _date_for_doc(doc: Document) -> datetime | None:
    return doc.issued_date or doc.ingest_date


def _sig_for_doc(doc: Document) -> str | None:
    if doc.significance_tier is None:
        return None
    tier = doc.significance_tier
    return tier.value if hasattr(tier, "value") else str(tier)


class CaseTimelineService:
    def __init__(self, db: Session):
        self.db = db

    def build_payload(self, case_id: str) -> dict:
        """Build the complete timeline payload for the given case.

        Returns a dict with:
          events        — list[TimelineEvent] sorted ascending by date
          month_buckets — list[dict] with keys: key, label, total, critical, future, max_total
          today         — datetime (naive UTC, midnight)
          total_count   — int
          quiet_gaps    — dict[event_id, int] gap in days before this event in same month
        """
        today = naive_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        events: list[TimelineEvent] = []

        self._add_document_events(case_id, today, events)
        self._add_action_item_events(case_id, today, events)
        self._add_cost_events(case_id, today, events)
        self._add_proceeding_milestones(case_id, today, events)

        events.sort(key=lambda e: e.date)

        month_buckets = self._build_month_buckets(events)
        quiet_gaps = self._build_quiet_gaps(events, today)

        return {
            "events": events,
            "month_buckets": month_buckets,
            "today": today,
            "total_count": len(events),
            "quiet_gaps": quiet_gaps,
        }

    # ------------------------------------------------------------------
    # Document events
    # ------------------------------------------------------------------

    def _add_document_events(
        self, case_id: str, today: datetime, out: list[TimelineEvent]
    ) -> None:
        docs = (
            self.db.query(Document)
            .filter(Document.case_id == case_id)
            .order_by(Document.issued_date.asc().nullslast(), Document.id.asc())
            .all()
        )
        doc_ids = [d.id for d in docs]

        # Bulk-query claim counts (distinct claims per document)
        claim_rows = (
            (
                self.db.query(
                    ClaimEvidence.document_id,
                    func.count(func.distinct(ClaimEvidence.claim_id)),
                )
                .filter(ClaimEvidence.document_id.in_(doc_ids))
                .group_by(ClaimEvidence.document_id)
                .all()
            )
            if doc_ids
            else []
        )
        claim_counts: dict[int, int] = {row[0]: row[1] for row in claim_rows}

        # Bulk-query relationship counts (both directions)
        from_rows = (
            (
                self.db.query(DocumentRelationship.from_document_id, func.count())
                .filter(DocumentRelationship.from_document_id.in_(doc_ids))
                .group_by(DocumentRelationship.from_document_id)
                .all()
            )
            if doc_ids
            else []
        )
        to_rows = (
            (
                self.db.query(DocumentRelationship.to_document_id, func.count())
                .filter(DocumentRelationship.to_document_id.in_(doc_ids))
                .group_by(DocumentRelationship.to_document_id)
                .all()
            )
            if doc_ids
            else []
        )
        rel_counts: dict[int, int] = {}
        for doc_id, cnt in from_rows:
            rel_counts[doc_id] = rel_counts.get(doc_id, 0) + cnt
        for doc_id, cnt in to_rows:
            rel_counts[doc_id] = rel_counts.get(doc_id, 0) + cnt

        # Build an index of child docs by parent_id for relay expansion
        children_by_parent: dict[int, list[Document]] = {}
        for doc in docs:
            if doc.parent_id is not None:
                children_by_parent.setdefault(doc.parent_id, []).append(doc)

        seen_ids: set[int] = set()

        for doc in docs:
            if doc.id in seen_ids:
                continue
            date = _date_for_doc(doc)
            if date is None:
                continue

            if doc.court_relay:
                # Emit relay parent as a court event
                seen_ids.add(doc.id)
                note = f"Court-certified service of: {doc.title}"
                out.append(
                    TimelineEvent(
                        id=f"doc-{doc.id}",
                        date=date,
                        actor="court",
                        kind="relay",
                        title=doc.title,
                        sig=_sig_for_doc(doc),
                        source_document_id=doc.id,
                        note=note,
                        amount_eur=None,
                        direction=None,
                        is_overdue=False,
                        is_future=date > today,
                        claim_count=claim_counts.get(doc.id, 0),
                        rel_count=rel_counts.get(doc.id, 0),
                    )
                )
                # Emit each non-trivial child with its own actor/kind
                for child in children_by_parent.get(doc.id, []):
                    child_date = _date_for_doc(child) or date
                    child_actor = _lane_for(child)
                    # Skip children that are pure transport noise (no attributed_originator, no content)
                    if not child.attributed_originator and not child.content:
                        seen_ids.add(child.id)
                        continue
                    seen_ids.add(child.id)
                    out.append(
                        TimelineEvent(
                            id=f"doc-{child.id}",
                            date=child_date,
                            actor=child_actor,
                            kind=_kind_for_doc(child, child_actor),
                            title=child.title,
                            sig=_sig_for_doc(child),
                            source_document_id=child.id,
                            note=None,
                            amount_eur=None,
                            direction=None,
                            is_overdue=False,
                            is_future=child_date > today,
                            claim_count=claim_counts.get(child.id, 0),
                            rel_count=rel_counts.get(child.id, 0),
                        )
                    )
            else:
                seen_ids.add(doc.id)
                actor = _lane_for(doc)
                out.append(
                    TimelineEvent(
                        id=f"doc-{doc.id}",
                        date=date,
                        actor=actor,
                        kind=_kind_for_doc(doc, actor),
                        title=doc.title,
                        sig=_sig_for_doc(doc),
                        source_document_id=doc.id,
                        note=None,
                        amount_eur=None,
                        direction=None,
                        is_overdue=False,
                        is_future=date > today,
                        claim_count=claim_counts.get(doc.id, 0),
                        rel_count=rel_counts.get(doc.id, 0),
                    )
                )

    # ------------------------------------------------------------------
    # ActionItem events
    # ------------------------------------------------------------------

    def _add_action_item_events(
        self, case_id: str, today: datetime, out: list[TimelineEvent]
    ) -> None:
        items = self.db.query(ActionItem).filter(ActionItem.case_id == case_id).all()
        for item in items:
            if item.due_date is None:
                continue
            date = item.due_date
            is_court = item.action_type == ActionItemType.COURT_DATE
            actor = "court" if is_court else "own"
            kind = "hearing" if is_court else "deadline"
            status_val = (
                item.status.value if hasattr(item.status, "value") else str(item.status)
            )
            is_open = status_val == "open"
            is_overdue = is_open and date < today
            out.append(
                TimelineEvent(
                    id=f"action-{item.id}",
                    date=date,
                    actor=actor,
                    kind=kind,
                    title=item.title,
                    sig="critical" if is_overdue else None,
                    source_document_id=item.source_document_id,
                    note=item.description if item.description else None,
                    amount_eur=None,
                    direction=None,
                    is_overdue=is_overdue,
                    is_future=date > today,
                )
            )

    # ------------------------------------------------------------------
    # LegalCost events
    # ------------------------------------------------------------------

    def _add_cost_events(
        self, case_id: str, today: datetime, out: list[TimelineEvent]
    ) -> None:
        costs = self.db.query(LegalCost).filter(LegalCost.case_id == case_id).all()
        for cost in costs:
            date = cost.paid_at or cost.due_at
            if date is None:
                continue
            status_val = (
                cost.status.value if hasattr(cost.status, "value") else str(cost.status)
            )
            direction = "credit" if status_val == CostStatus.ERSTATTET else "debit"
            cat = cost.category
            if cat not in _DEBIT_CATEGORIES and status_val != CostStatus.ERSTATTET:
                direction = "debit"  # default to debit for unknown categories
            out.append(
                TimelineEvent(
                    id=f"cost-{cost.id}",
                    date=date,
                    actor="own",
                    kind="payment",
                    title=cost.title,
                    sig=None,
                    source_document_id=cost.source_document_id,
                    note=None,
                    amount_eur=cost.amount_gross,
                    direction=direction,
                    is_overdue=False,
                    is_future=date > today,
                )
            )

    # ------------------------------------------------------------------
    # Proceeding milestones
    # ------------------------------------------------------------------

    def _add_proceeding_milestones(
        self, case_id: str, today: datetime, out: list[TimelineEvent]
    ) -> None:
        proceedings = (
            self.db.query(Proceeding).filter(Proceeding.case_id == case_id).all()
        )
        for p in proceedings:
            label = f"{p.court_name} · {p.az_court}" if p.az_court else p.court_name
            if p.started_at:
                out.append(
                    TimelineEvent(
                        id=f"proc-{p.id}-open",
                        date=p.started_at,
                        actor="court",
                        kind="milestone",
                        title=f"Proceeding opened — {label}",
                        sig="milestone",
                        source_document_id=None,
                        note=None,
                        amount_eur=None,
                        direction=None,
                        is_overdue=False,
                        is_future=p.started_at > today,
                    )
                )
            if p.ended_at:
                out.append(
                    TimelineEvent(
                        id=f"proc-{p.id}-close",
                        date=p.ended_at,
                        actor="court",
                        kind="milestone",
                        title=f"Proceeding closed — {label}",
                        sig="milestone",
                        source_document_id=None,
                        note=None,
                        amount_eur=None,
                        direction=None,
                        is_overdue=False,
                        is_future=p.ended_at > today,
                    )
                )

    # ------------------------------------------------------------------
    # Bucket + gap helpers
    # ------------------------------------------------------------------

    def _build_month_buckets(self, events: list[TimelineEvent]) -> list[dict]:
        bucket_map: dict[str, dict] = {}
        for ev in events:
            key = ev.date.strftime("%Y-%m")
            if key not in bucket_map:
                bucket_map[key] = {
                    "key": key,
                    "label": ev.date.strftime("%b %Y"),
                    "total": 0,
                    "critical": 0,
                    "future": 0,
                }
            bucket_map[key]["total"] += 1
            if ev.sig == "critical" or ev.is_overdue:
                bucket_map[key]["critical"] += 1
            if ev.is_future:
                bucket_map[key]["future"] += 1

        buckets = sorted(bucket_map.values(), key=lambda b: b["key"])
        max_total = max((b["total"] for b in buckets), default=1)
        for b in buckets:
            b["max_total"] = max_total
        return buckets

    def _build_quiet_gaps(
        self, events: list[TimelineEvent], today: datetime
    ) -> dict[str, int]:
        """Return {event_id: gap_days} for past events preceded by a ≥14-day silence."""
        gaps: dict[str, int] = {}
        last_by_month: dict[str, datetime] = {}
        for ev in events:
            if ev.is_future:
                continue
            key = ev.date.strftime("%Y-%m")
            if key in last_by_month:
                delta = int((ev.date - last_by_month[key]).total_seconds() / 86400)
                if delta >= 14:
                    gaps[ev.id] = delta
            last_by_month[key] = ev.date
        return gaps
