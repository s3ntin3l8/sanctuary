from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.database import ActionItem, Case, Document, IngestBatch
from app.models.enums import (
    ActionItemStatus,
    CaseStatus,
    IngestBatchStatus,
    SignificanceTier,
)
from app.services.attention_scoring import score_action_item, score_triage_batch
from app.services.signals import get_signals


class HomeService:
    def __init__(self, db: Session):
        self.db = db

    def get_home_data(self, user_id: int) -> dict[str, Any]:
        """Aggregate all data for the Home page dashboard."""
        now = datetime.now()

        # Per-user isolation: regular users see only their own cases' data;
        # admins (owner=None) see everything. Triage (pre-case-assignment) is a
        # shared workflow stage and is not owner-filtered.
        from app.models.database import User

        user = self.db.get(User, user_id) if user_id is not None else None
        from app.services import access_service

        # None = admin/dev (no restriction); otherwise owned ∪ shared case ids.
        visible = access_service.visible_case_ids(self.db, user)
        user_name = (user.display_name or user.email) if user else "there"

        # Calculate Greeting
        hour = now.hour
        if hour < 12:
            greeting = "Good morning"
        elif hour < 18:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"

        # 1. Today Panel (Action Items)
        # Due in next 30 days or overdue, status=open
        thirty_days_later = now + timedelta(days=30)
        action_items_q = (
            self.db.query(ActionItem)
            .options(
                joinedload(ActionItem.case), joinedload(ActionItem.source_document)
            )
            .filter(
                ActionItem.status == ActionItemStatus.OPEN,
                ActionItem.due_date <= thirty_days_later,
                # Home shows only the user's deadlines; opposing/third-party/court
                # items are case-scoped and visible via the case action-items toggle.
                or_(ActionItem.addressee == "user", ActionItem.addressee.is_(None)),
            )
        )
        if visible is not None:
            action_items_q = action_items_q.filter(ActionItem.case_id.in_(visible))
        action_items = action_items_q.all()

        # Sort by attention score (urgency first)
        action_items = sorted(action_items, key=score_action_item, reverse=True)

        # 2. Awaiting Triage Panel — per-user intake inbox (each user sees only
        # the batches they ingested).
        triage_batches = (
            self.db.query(IngestBatch)
            .options(joinedload(IngestBatch.documents))
            .filter(
                IngestBatch.status != IngestBatchStatus.COMPLETED,
                IngestBatch.owner_id == user_id,
            )
            .all()
        )

        # Sort by attention score
        triage_batches = sorted(triage_batches, key=score_triage_batch, reverse=True)

        # 3. Delta Feed (New docs since last home visit)
        from app.services.user_settings_service import get_last_home_visit

        last_home_visit = get_last_home_visit(self.db, user_id)

        delta_cases: list[dict[str, Any]] = []
        sig_values = {
            SignificanceTier.CRITICAL: 4,
            SignificanceTier.SIGNIFICANT: 3,
            SignificanceTier.INFORMATIONAL: 2,
            SignificanceTier.ADMINISTRATIVE: 1,
        }
        if last_home_visit:
            cases_with_new_docs_q = (
                self.db.query(Case)
                .join(Document, Case.id == Document.case_id)
                .filter(Document.ingest_date > last_home_visit)
            )
            if visible is not None:
                cases_with_new_docs_q = cases_with_new_docs_q.filter(
                    Case.id.in_(visible)
                )
            cases_with_new_docs = cases_with_new_docs_q.distinct().all()

            # Single batched query for all new docs across all affected cases —
            # avoids the N+1 that previously fired one query per case in the loop.
            case_ids = [c.id for c in cases_with_new_docs]
            new_docs_by_case: dict[str, list[Document]] = {cid: [] for cid in case_ids}
            if case_ids:
                rows = (
                    self.db.query(Document)
                    .filter(
                        Document.case_id.in_(case_ids),
                        Document.ingest_date > last_home_visit,
                    )
                    .order_by(Document.ingest_date.desc())
                    .all()
                )
                for d in rows:
                    # case_id can't be None here: the query above filters it to
                    # be in case_ids (a list of real case ids).
                    if d.case_id is not None:
                        new_docs_by_case[d.case_id].append(d)

            # Single batched query for new ActionItem counts per case.
            from sqlalchemy import func as sa_func

            action_counts: dict[str, int] = dict.fromkeys(case_ids, 0)
            if case_ids:
                action_count_rows = (
                    self.db.query(ActionItem.case_id, sa_func.count(ActionItem.id))
                    .filter(
                        ActionItem.case_id.in_(case_ids),
                        ActionItem.ingest_date > last_home_visit,
                    )
                    .group_by(ActionItem.case_id)
                    .all()
                )
                for case_id, n in action_count_rows:
                    action_counts[case_id] = n

            for case in cases_with_new_docs:
                new_docs = new_docs_by_case.get(case.id, [])

                max_sig = SignificanceTier.ADMINISTRATIVE
                for d in new_docs:
                    if d.significance_tier and sig_values.get(
                        d.significance_tier, 0
                    ) > sig_values.get(max_sig, 0):
                        max_sig = d.significance_tier

                delta_cases.append(
                    {
                        "case_id": case.id,
                        "case_title": case.title,
                        "new_doc_count": len(new_docs),
                        "max_significance": max_sig.value
                        if max_sig
                        else "administrative",
                        "doc_titles": [d.title for d in new_docs[:3]],
                        "new_actions": action_counts.get(case.id, 0),
                    }
                )

            # Sort delta cases by significance
            delta_cases.sort(
                key=lambda x: sig_values.get(
                    SignificanceTier(x["max_significance"]), 0
                ),
                reverse=True,
            )

        # 4. Signals
        signals = get_signals(self.db)

        # 5. Active Cases Strip
        from app.services.case_service import CaseService

        case_service = CaseService(self.db)
        active_cases_query = (
            self.db.query(Case)
            .options(joinedload(Case.proceedings))
            .filter(Case.status != CaseStatus.CLOSED, Case.id != "_TRIAGE")
        )
        if visible is not None:
            active_cases_query = active_cases_query.filter(Case.id.in_(visible))
        active_cases = active_cases_query.order_by(Case.ingest_date.desc()).all()

        batched = case_service._batch_card_context([c.id for c in active_cases])
        enriched_cases = [
            case_service.enrich_case_for_card(c, now, last_home_visit, _batched=batched)
            for c in active_cases
        ]
        draft_cases = [c for c in enriched_cases if c["is_draft"]]
        confirmed_cases = [c for c in enriched_cases if not c["is_draft"]]

        return {
            "user_name": user_name,
            "greeting": greeting,
            "now": now,
            "today_items": action_items,
            "triage_bundles": triage_batches,
            "delta_cases": delta_cases,
            "signals": signals,
            "active_cases": confirmed_cases,
            "draft_cases": draft_cases,
            "last_home_visit": last_home_visit,
            "caught_up": not (action_items or triage_batches or delta_cases or signals),
        }


def count_new_since(case_id: str, since: datetime | None, db) -> int:
    """Helper to count documents added to the case after `since`."""
    if since is None:
        return 0
    return (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.ingest_date > since)
        .count()
    )
