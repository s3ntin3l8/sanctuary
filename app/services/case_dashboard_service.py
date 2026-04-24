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
    CostStatus,
    Document,
    Proceeding,
    UserReaction,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    UserReactionType,
)
from app.services.case_graph_service import CaseGraphService
from app.services.case_service import CaseService, _compute_dormancy_alert
from app.services.claim_service import ClaimService


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
    ) -> dict | None:
        """Return the full template context dict for the case dashboard.

        Returns ``None`` when the case does not exist (caller should render 404).
        """
        case_service = CaseService(self.db)
        data = case_service.get_case_with_summary(case_id)
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
                significance_filter,
                new_doc_ids=new_doc_ids,
                reaction_map=reaction_map,
            )
            graph_dict = dataclasses.asdict(payload)

        # --- Action items (deadlines + court dates combined, chronological)
        action_items = (
            self.db.query(ActionItem)
            .filter(ActionItem.case_id == case_id)
            .order_by(ActionItem.due_date.asc().nullslast(), ActionItem.id.asc())
            .all()
        )

        # --- Strategic layer: truth map, cost summary, dormancy ---------
        claim_svc = ClaimService(self.db)
        truth_map = claim_svc.get_truth_map(case.id, "open")
        cost_summary = build_cost_summary(data["costs"], CostStatus)
        dormancy_alert = _compute_dormancy_alert(case, self.db)

        # --- Financials (factual: total exposure in cents) --------------
        financials = {
            "total_cost_exposure": case.total_cost_exposure or 0,
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
            key=lambda d: d.issued_date or d.ingest_date,
            reverse=True,
        )

        return {
            # Phase 8 additions
            "proceedings": proceedings,
            "active_proceeding": active_proceeding,
            "graph": graph_dict,
            "action_items": action_items,
            "new_docs": new_docs_for_template,
            "parties": case.parties or [],
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
        rows = (
            self.db.query(UserReaction, Document.id)
            .join(Document, UserReaction.document_id == Document.id)
            .filter(Document.proceeding_id == proceeding_id)
            .order_by(UserReaction.ingest_date.desc())
            .all()
        )
        out: dict[int, str] = {}
        for reaction, doc_id in rows:
            if doc_id in out:
                continue
            # Store the raw string value (e.g. 'lies') for template emoji mapping
            val = reaction.reaction
            if hasattr(val, "value"):
                out[doc_id] = val.value
            else:
                out[doc_id] = str(val)
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
            }
        )
    return out


def originator_color_for_doc(doc) -> str:
    """Return the originator-color key (own/court/opposing/third) for a doc."""
    from app.services.case_graph_service import _lane_for

    return _lane_for(doc)


def neighbor_doc_ids(db: Session, doc) -> tuple[int | None, int | None]:
    """Return (prev_doc_id, next_doc_id) within the same proceeding."""
    if doc.proceeding_id is None:
        return None, None
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
        return None, None
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if idx < len(ids) - 1 else None
    return prev_id, next_id


# Re-export so callers can reach these without extra imports
__all__ = [
    "CaseDashboardService",
    "summary_bullets_from_ai_summary",
    "key_passages_for_template",
    "originator_color_for_doc",
    "neighbor_doc_ids",
]
