from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models.database import ActionItem, Case, Document, IngestBatch, UserSettings
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

    def get_home_data(self) -> dict[str, Any]:
        """Aggregate all data for the Home page dashboard."""
        now = datetime.now()

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
        action_items = (
            self.db.query(ActionItem)
            .options(
                joinedload(ActionItem.case), joinedload(ActionItem.source_document)
            )
            .filter(
                ActionItem.status == ActionItemStatus.OPEN,
                ActionItem.due_date <= thirty_days_later,
            )
            .all()
        )

        # Sort by attention score (urgency first)
        action_items = sorted(action_items, key=score_action_item, reverse=True)

        # 2. Awaiting Triage Panel (Ingest Batches)
        triage_batches = (
            self.db.query(IngestBatch)
            .options(joinedload(IngestBatch.documents))
            .filter(IngestBatch.status != IngestBatchStatus.COMPLETED)
            .all()
        )

        # Sort by attention score
        triage_batches = sorted(triage_batches, key=score_triage_batch, reverse=True)

        # 3. Delta Feed (New docs since last home visit)
        # Fetch last_home_visit from user settings
        settings = self.db.query(UserSettings).first()
        last_home_visit_iso = (
            settings.settings_json.get("last_home_visit")
            if settings and settings.settings_json
            else None
        )
        last_home_visit = (
            datetime.fromisoformat(last_home_visit_iso) if last_home_visit_iso else None
        )

        delta_cases = []
        if last_home_visit:
            # Find cases with new docs since last visit
            cases_with_new_docs = (
                self.db.query(Case)
                .join(Document, Case.id == Document.case_id)
                .filter(Document.created_at > last_home_visit)
                .distinct()
                .all()
            )

            for case in cases_with_new_docs:
                new_docs = (
                    self.db.query(Document)
                    .filter(
                        Document.case_id == case.id,
                        Document.created_at > last_home_visit,
                    )
                    .order_by(Document.created_at.desc())
                    .all()
                )

                # Determine max significance tier among new documents
                max_sig = SignificanceTier.ADMINISTRATIVE
                sig_values = {
                    SignificanceTier.CRITICAL: 4,
                    SignificanceTier.SIGNIFICANT: 3,
                    SignificanceTier.INFORMATIONAL: 2,
                    SignificanceTier.ADMINISTRATIVE: 1,
                }

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
                        "new_actions": self.db.query(ActionItem)
                        .filter(
                            ActionItem.case_id == case.id,
                            ActionItem.created_at > last_home_visit,
                        )
                        .count(),
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
        active_cases_query = (
            self.db.query(Case)
            .options(joinedload(Case.proceedings))
            .filter(Case.status != CaseStatus.CLOSED)
        )
        active_cases = active_cases_query.order_by(Case.created_at.desc()).all()

        # Enrich active cases with some metadata for the card
        enriched_cases = []
        for c in active_cases:
            # Get closest action item
            next_action = (
                self.db.query(ActionItem)
                .filter(
                    ActionItem.case_id == c.id,
                    ActionItem.status == ActionItemStatus.OPEN,
                )
                .order_by(ActionItem.due_date.asc())
                .first()
            )

            new_docs_count = (
                count_new_since(c.id, last_home_visit, self.db)
                if last_home_visit
                else 0
            )

            # Days since last activity
            last_doc = (
                self.db.query(Document)
                .filter(Document.case_id == c.id)
                .order_by(Document.created_at.desc())
                .first()
            )
            days_since = (
                (now - last_doc.created_at).days
                if last_doc
                else (now - c.created_at).days
            )

            # Get active proceeding name
            active_proc = next((p for p in c.proceedings if p.status == "active"), None)
            if not active_proc and c.proceedings:
                active_proc = c.proceedings[0]

            proceeding_name = active_proc.court_name if active_proc else "General"

            enriched_cases.append(
                {
                    "id": c.id,
                    "title": c.title,
                    "status": c.status,
                    "status_line": c.ai_brief.get("status_line", "Active")
                    if c.ai_brief
                    else "Active",
                    "next_action": next_action,
                    "exposure_eur": c.total_cost_exposure / 100.0
                    if c.total_cost_exposure
                    else 0.0,
                    "new_docs": new_docs_count,
                    "days_since_activity": days_since,
                    "tier": "delta" if new_docs_count > 0 else "normal",
                    "proceeding_name": proceeding_name,
                }
            )

        return {
            "user_name": "Björn",  # Placeholder or fetch from settings
            "greeting": greeting,
            "now": now,
            "today_items": action_items,
            "triage_bundles": triage_batches,
            "delta_cases": delta_cases,
            "signals": signals,
            "active_cases": enriched_cases,
            "last_home_visit": last_home_visit,
            "caught_up": not (action_items or triage_batches or delta_cases or signals),
        }


def count_new_since(case_id: str, since: datetime | None, db) -> int:
    """Helper to count documents added to the case after `since`."""
    if since is None:
        return 0
    return (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.created_at > since)
        .count()
    )
