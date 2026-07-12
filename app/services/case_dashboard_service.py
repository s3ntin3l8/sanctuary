"""CaseDashboardService — orchestrates all data for the case dashboard page.

Builds the full template context as a superset: the legacy keys the current
case_dashboard template consumes (documents/deadlines/hearings/cost_summary/
truth_map/…) plus the new Phase 8 keys (proceedings/active_proceeding/graph/
action_items/new_docs/initial).

Passing a superset lets the template be refactored in a follow-up step (task #8)
without breaking the live page in the meantime.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Literal, cast

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.constants import (
    CASE_STATUS_META,
    COST_CATEGORY_META,
    COST_STATUS_META,
    ORIGINATOR_COLORS,
)
from app.helpers import build_cost_summary
from app.models.database import (
    ActionItem,
    CostSignal,
    CostStatus,
    Document,
    IngestBatch,
    LegalCost,
    Proceeding,
    UserReaction,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    UserReactionType,
)
from app.services.case_graph_service import CaseGraphService
from app.services.case_service import (
    CaseService,
    _compute_dormancy_alert,
    build_case_level_costs,
    build_proceeding_exposure,
)
from app.services.case_timeline_service import CaseTimelineService
from app.services.claim_service import ClaimService

# Display order for party roles in the sidebar: own first, unknown last.
_PARTY_ROLE_ORDER: dict[str, int] = {
    "own": 0,
    "court": 1,
    "opposing": 2,
    "third_party": 3,
    "unknown": 4,
}


class CaseDashboardService:
    """Gather every piece of data the case dashboard page needs."""

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def build_context(
        self,
        case_id: str,
        active_proceeding_id: int | None,
        active_view: str,
        significance_filter: str = "significant+",
        user_id: int | None = None,
    ) -> dict | None:
        """Return the full template context dict for the case dashboard.

        Returns ``None`` when the case does not exist (caller should render 404).
        """
        case_service = CaseService(self.db)
        data = case_service.get_case_with_summary(case_id, user_id)
        if not data:
            return None

        case = data["case"]

        # --- Proceedings ------------------------------------------------
        proceedings_with_counts = (
            self.db.query(Proceeding, func.count(Document.id).label("doc_count"))
            .outerjoin(Document, Document.proceeding_id == Proceeding.id)
            .filter(Proceeding.case_id == case_id)
            .group_by(Proceeding.id)
            .order_by(Proceeding.ingest_date.asc().nullslast(), Proceeding.id.asc())
            .all()
        )

        proceedings = []
        for p, count in proceedings_with_counts:
            p.doc_count = count
            proceedings.append(p)

        # Compute is_deletable: empty AND not the sole remaining proceeding.
        if len(proceedings) > 1:
            proc_ids = [p.id for p in proceedings]
            batch_counts: dict[int, int] = {
                row[0]: row[1]
                for row in self.db.query(
                    IngestBatch.proceeding_id, func.count(IngestBatch.id)
                )
                .filter(IngestBatch.proceeding_id.in_(proc_ids))
                .group_by(IngestBatch.proceeding_id)
                .all()
                if row[0] is not None
            }
            action_counts: dict[int, int] = {
                row[0]: row[1]
                for row in self.db.query(
                    ActionItem.proceeding_id, func.count(ActionItem.id)
                )
                .filter(ActionItem.proceeding_id.in_(proc_ids))
                .group_by(ActionItem.proceeding_id)
                .all()
                if row[0] is not None
            }
            cost_counts: dict[int, int] = {
                row[0]: row[1]
                for row in self.db.query(
                    LegalCost.proceeding_id, func.count(LegalCost.id)
                )
                .filter(LegalCost.proceeding_id.in_(proc_ids))
                .group_by(LegalCost.proceeding_id)
                .all()
                if row[0] is not None
            }
            for p in proceedings:
                p.is_deletable = (
                    p.doc_count == 0
                    and batch_counts.get(p.id, 0) == 0
                    and action_counts.get(p.id, 0) == 0
                    and cost_counts.get(p.id, 0) == 0
                )
        else:
            for p in proceedings:
                p.is_deletable = False

        active_proceeding: Proceeding | None = None
        if active_proceeding_id is not None:
            active_proceeding = next(
                (p for p in proceedings if p.id == active_proceeding_id), None
            )
        if active_proceeding is None and proceedings:
            active_proceeding = proceedings[0]

        # --- New docs (since last visit, scoped to active proceeding) ---
        last_visit = data["last_visit"]
        new_docs: list[Document] = []
        if active_proceeding is not None and last_visit is not None:
            new_docs = (
                self.db.query(Document)
                .options(joinedload(Document.proceeding))
                .filter(
                    Document.proceeding_id == active_proceeding.id,
                    Document.ingest_date > last_visit,
                )
                .order_by(Document.ingest_date.desc())
                .all()
            )
        new_doc_ids = {d.id for d in new_docs}
        new_docs_for_template = [
            {
                "id": d.id,
                "title": d.title,
                "originator_color": originator_color_for_doc(d),
            }
            for d in new_docs
        ]

        # --- Graph payload (only when an active proceeding exists) ------
        graph_dict: dict | None = None
        if active_proceeding is not None:
            reaction_map = self._reaction_map_for_proceeding(active_proceeding.id)
            payload = CaseGraphService(self.db).build_payload(
                active_proceeding.id,
                # significance_filter is route-validated (FilterQuery regex) to be
                # one of these three literals before it reaches this call.
                cast(Literal["critical", "significant+", "all"], significance_filter),
                new_doc_ids=new_doc_ids,
                reaction_map=reaction_map,
            )
            graph_dict = dataclasses.asdict(payload)

        # --- Action items (deadlines + court dates combined, chronological)
        action_items = (
            self.db.query(ActionItem)
            .filter(
                ActionItem.case_id == case_id,
                ActionItem.superseded.is_(False),  # tombstones are display-invisible
            )
            .order_by(ActionItem.due_date.asc().nullslast(), ActionItem.id.asc())
            .all()
        )

        # --- Strategic layer: truth map, cost summary, dormancy ---------
        claim_svc = ClaimService(self.db)
        truth_map = claim_svc.get_truth_map(case.id, "open")

        costs = data["costs"]
        cost_summary = build_cost_summary(costs, CostStatus)
        dormancy_alert = _compute_dormancy_alert(case, self.db)

        # --- Financials (factual: total exposure in cents) --------------
        # Documents that carry a *meta* cost signal — streitwert, cost ruling,
        # or PKH decision. Invoice / Vorschuss-source documents are no longer
        # included here because the actual LegalCost rows surface inside the
        # per-proceeding consolidated table; listing them in two places was
        # duplication.
        signal_doc_ids = {
            row[0]
            for row in self.db.query(CostSignal.source_document_id)
            .filter(CostSignal.case_id == case_id)
            .all()
        }
        cost_signal_docs = (
            self.db.query(Document)
            .filter(
                Document.case_id == case_id,
                Document.id.in_(signal_doc_ids),
            )
            .order_by(Document.issued_date.desc().nullslast(), Document.id.desc())
            .all()
            if signal_doc_ids
            else []
        )
        financials = {
            "total_cost_exposure": case.total_cost_exposure or 0,
            "cost_signal_docs": cost_signal_docs,
            "proceeding_exposure": build_proceeding_exposure(case_id, self.db),
            "case_level_costs": build_case_level_costs(case_id, self.db),
        }

        # --- Alpine bootstrap payload ----------------------------------
        initial = {
            "caseId": case.id,
            "view": active_view,
            "filter": significance_filter,
            "activeProceedingId": active_proceeding.id if active_proceeding else None,
            "nodeCounts": graph_dict.get("node_counts", {}) if graph_dict else {},
        }

        # --- Legacy keys preserved so the existing template keeps rendering
        documents_sorted = sorted(
            data["documents"],
            key=lambda d: (d.issued_date or d.ingest_date or datetime.min).replace(
                tzinfo=None
            ),
            reverse=True,
        )

        # --- Timeline payload (always built; cheap aggregation) ---------
        timeline = CaseTimelineService(self.db).build_payload(case_id)

        from app.services import user_settings_service

        return {
            # Phase 8 additions
            "proceedings": proceedings,
            "active_proceeding": active_proceeding,
            "dedup_job": user_settings_service.get_dedup_job(case_id, self.db),
            "graph": graph_dict,
            "timeline": timeline,
            "action_items": action_items,
            "new_docs": new_docs_for_template,
            "parties": sorted(
                case.parties or [],
                key=lambda p: _PARTY_ROLE_ORDER.get(p.get("role", "unknown"), 99),
            ),
            "brief": case.ai_brief,
            "financials": financials,
            "initial": initial,
            "significance_filter": significance_filter,
            "active_view": active_view,
            # Legacy keys (kept so pages/case_dashboard.html still renders)
            "case": case,
            "documents": documents_sorted,
            "deadlines": data["deadlines"],
            "hearings": data["hearings"],
            "costs": data["costs"],
            "ai_brief_updated_at": case.ai_brief_updated_at,
            "total_cost_exposure": case.total_cost_exposure or 0,
            "cost_summary": cost_summary,
            "cost_category_meta": COST_CATEGORY_META,
            "cost_status_meta": COST_STATUS_META,
            "count": data["new_docs_since_last_visit"],
            "since": last_visit,
            "dormancy_alert": dormancy_alert,
            "originator_colors": ORIGINATOR_COLORS,
            "status_meta": CASE_STATUS_META,
            "truth_map": truth_map,
            "ClaimStatus": ClaimStatus,
            "ClaimEvidenceRole": ClaimEvidenceRole,
            "UserReactionType": UserReactionType,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _reaction_map_for_proceeding(self, proceeding_id: int) -> dict[int, str]:
        """Return {doc_id: reaction_emoji} for docs in a proceeding.

        Pairs each document with its most-recent user reaction (if any). The
        graph only needs a single glyph per node, so we collapse multiple
        reactions down to the latest.
        """
        from sqlalchemy import and_, func

        # Subquery: latest reaction ingest_date per doc, scoped to the
        # proceeding's documents. SQLite has no DISTINCT ON, so we GROUP BY
        # doc_id and pick the row whose ingest_date matches the max — cheaper
        # than fetching every reaction and discarding 80% in Python.
        latest = (
            self.db.query(
                UserReaction.document_id.label("doc_id"),
                func.max(UserReaction.ingest_date).label("ts"),
            )
            .join(Document, UserReaction.document_id == Document.id)
            .filter(Document.proceeding_id == proceeding_id)
            .group_by(UserReaction.document_id)
            .subquery()
        )

        rows = (
            self.db.query(UserReaction)
            .join(
                latest,
                and_(
                    UserReaction.document_id == latest.c.doc_id,
                    UserReaction.ingest_date == latest.c.ts,
                ),
            )
            .all()
        )

        out: dict[int, str] = {}
        for r in rows:
            val = r.reaction
            out[r.document_id] = val.value if hasattr(val, "value") else str(val)
        return out


# ---------------------------------------------------------------------------
# Module-level helpers exposed for the HUD route
# ---------------------------------------------------------------------------


def summary_bullets_from_ai_summary(ai_summary) -> list[dict]:
    """Coerce the various `Document.ai_summary` shapes into the HUD template's
    `[{kind, text}]` format.

    Accepted shapes:
    * dict with keys ``legal_significance`` / ``required_action`` /
      ``financial_impact`` (the canonical Management Summary structure).
    * already-formatted list of ``{kind, text}`` dicts — passed through.
    * plain string — wrapped as a single ``{kind: "legal", text}`` bullet.
    """
    if not ai_summary:
        return []
    if isinstance(ai_summary, list):
        return [b for b in ai_summary if isinstance(b, dict) and b.get("text")]
    if isinstance(ai_summary, str):
        return [{"kind": "legal", "text": ai_summary}]
    if isinstance(ai_summary, dict):
        mapping = [
            ("legal", ai_summary.get("legal_significance")),
            ("action", ai_summary.get("required_action")),
            ("finance", ai_summary.get("financial_impact")),
        ]
        return [{"kind": kind, "text": text} for kind, text in mapping if text]
    return []


def key_passages_for_template(key_passages) -> list[dict]:
    """Normalise `Document.key_passages` to `[{text, kind, page, rationale, id}]`."""
    import hashlib

    if not key_passages:
        return []
    out: list[dict] = []
    for raw in key_passages:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text") or ""
        if not text:
            continue
        kind = (raw.get("kind") or "neutral").lower()
        page = raw.get("page")
        if page is None:
            span = raw.get("span") or {}
            if isinstance(span, dict):
                page = span.get("page")
        pid = raw.get("id") or hashlib.sha1(f"{text}|{kind}".encode()).hexdigest()[:12]
        out.append(
            {
                "text": text,
                "kind": kind,
                "page": page,
                "rationale": raw.get("rationale") or "",
                "id": pid,
                "start_offset": raw.get("start_offset"),
                "end_offset": raw.get("end_offset"),
            }
        )
    return out


def originator_color_for_doc(doc) -> str:
    """Return the originator-color key (own/court/opposing/third) for a doc."""
    from app.services.case_graph_service import _lane_for

    return _lane_for(doc)


def neighbor_doc_ids(db: Session, doc) -> tuple[int | None, int | None, int, int]:
    """Return (prev_doc_id, next_doc_id, position, total) within the same proceeding.

    position is 1-indexed; both position and total are 0 when there is no proceeding.
    """
    if doc.proceeding_id is None:
        return None, None, 0, 0
    siblings = (
        db.query(Document.id)
        .filter(Document.proceeding_id == doc.proceeding_id)
        .order_by(
            Document.issued_date.asc().nullslast(),
            Document.id.asc(),
        )
        .all()
    )
    ids = [row[0] for row in siblings]
    try:
        idx = ids.index(doc.id)
    except ValueError:
        return None, None, 0, 0
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if idx < len(ids) - 1 else None
    return prev_id, next_id, idx + 1, len(ids)


# Re-export so callers can reach these without extra imports
__all__ = [
    "CaseDashboardService",
    "summary_bullets_from_ai_summary",
    "key_passages_for_template",
    "originator_color_for_doc",
    "neighbor_doc_ids",
]
