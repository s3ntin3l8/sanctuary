"""Build the context dict for all three Document HUD surfaces.

One entry point: ``build_hud_context(db, doc, *, mode="read")``. Returns a flat
dict ready for ``TemplateResponse(request, template, ctx)``.
"""

import hashlib

from sqlalchemy.orm import Session

from app.models.database import (
    ActionItem,
    Case,
    Claim,
    ClaimEvidence,
    Document,
    DocumentPin,
    DocumentRelationship,
    UserReaction,
)
from app.models.enums import OriginatorType as _OriginatorType
from app.services.case_dashboard_service import (
    key_passages_for_template,
    neighbor_doc_ids,
    originator_color_for_doc,
    summary_bullets_from_ai_summary,
)


def _passage_id(text: str, kind: str) -> str:
    return hashlib.sha1(f"{text}|{kind}".encode()).hexdigest()[:12]


def _build_passage_claim_map(
    db: Session, doc: Document, key_passages: list[dict]
) -> dict[str, int]:
    """Map passage_id → claim_id for passages cited as evidence from this doc."""
    if not key_passages:
        return {}

    evidence_rows = (
        db.query(ClaimEvidence)
        .filter(
            ClaimEvidence.document_id == doc.id,
            ClaimEvidence.excerpt.isnot(None),
        )
        .all()
    )
    if not evidence_rows:
        return {}

    passage_claim_map: dict[str, int] = {}
    for ev in evidence_rows:
        if not ev.excerpt:
            continue
        excerpt_lower = ev.excerpt.lower()
        for passage in key_passages:
            pid = passage.get("id")
            if not pid or pid in passage_claim_map:
                continue
            if excerpt_lower in passage["text"].lower():
                passage_claim_map[pid] = ev.claim_id
                break

    return passage_claim_map


def _build_relationships(db: Session, doc: Document) -> tuple[list[dict], list[dict]]:
    """Return (rels_out, rels_in) as flat dicts for template use."""
    rels_out = (
        db.query(DocumentRelationship)
        .filter(DocumentRelationship.from_document_id == doc.id)
        .all()
    )
    rels_in = (
        db.query(DocumentRelationship)
        .filter(DocumentRelationship.to_document_id == doc.id)
        .all()
    )

    related_ids = {r.to_document_id for r in rels_out} | {
        r.from_document_id for r in rels_in
    }
    titles: dict[int, str] = {}
    if related_ids:
        for row in (
            db.query(Document.id, Document.title)
            .filter(Document.id.in_(related_ids))
            .all()
        ):
            titles[row[0]] = row[1] or "Untitled"

    def _flatten(rels, *, side: str) -> list[dict]:
        out = []
        for rel in rels:
            other_id = rel.to_document_id if side == "out" else rel.from_document_id
            rel_type = (
                rel.relationship_type.value if rel.relationship_type else "related"
            )
            confidence = rel.confidence.value if rel.confidence else None
            out.append(
                {
                    "id": other_id,
                    "title": titles.get(other_id, "Untitled"),
                    "rel_type": rel_type,
                    "confidence": confidence,
                    "rel_obj": rel,
                }
            )
        return out

    return _flatten(rels_out, side="out"), _flatten(rels_in, side="in")


def build_hud_context(
    db: Session,
    doc: Document,
    *,
    mode: str = "read",
    context: str = "overlay",
    cases: list | None = None,
) -> dict:
    """Aggregate all data needed to render any HUD context.

    ``mode``: ``"read"`` (default) or ``"review"`` (triage right pane, shows
    metadata form for case assignment and originator editing).
    ``context``: ``"overlay"`` | ``"standalone"`` | ``"embedded"``.
    ``cases``: when ``context="embedded"`` (triage right pane), pass the list
    of active cases for the case-assignment select; also triggers addition of
    ``OriginatorType`` and ``is_draft_case`` keys.
    """
    reactions = (
        db.query(UserReaction)
        .filter(UserReaction.document_id == doc.id)
        .order_by(UserReaction.ingest_date.asc())
        .all()
    )

    grounds = (
        db.query(Claim)
        .filter(Claim.source_document_id == doc.id)
        .order_by(Claim.first_made_at.desc())
        .all()
    )

    _claims_stage = (doc.pipeline_stages or {}).get("claims", {})
    _claims_stage_status = _claims_stage.get("status", "pending")
    if _claims_stage_status == "skipped":
        claims_status = "skipped"
    elif _claims_stage_status == "completed":
        claims_status = "ran"
    elif not doc.case_id or doc.case_id == "_TRIAGE":
        claims_status = "pending_triage"
    else:
        claims_status = "pending"

    actions = (
        db.query(ActionItem)
        .filter(ActionItem.source_document_id == doc.id)
        .order_by(ActionItem.due_date.asc())
        .all()
    )

    summary_bullets = summary_bullets_from_ai_summary(doc.ai_summary)
    key_passages = key_passages_for_template(doc.key_passages)
    prev_doc_id, next_doc_id = neighbor_doc_ids(db, doc)
    originator_color = originator_color_for_doc(doc)
    relationships_out, relationships_in = _build_relationships(db, doc)
    passage_claim_map = _build_passage_claim_map(db, doc, key_passages)

    pins = (
        db.query(DocumentPin)
        .filter(DocumentPin.document_id == doc.id)
        .order_by(DocumentPin.ingest_date.asc())
        .all()
    )
    passage_pin_counts: dict[str, int] = {}
    for pin in pins:
        passage_pin_counts[pin.passage_id] = (
            passage_pin_counts.get(pin.passage_id, 0) + 1
        )

    first_child_id: int | None = None
    if doc.children:
        first_child_id = doc.children[0].id

    bundle_prev_id: int | None = None
    bundle_next_id: int | None = None
    if doc.ingest_batch_id:
        siblings = (
            db.query(Document.id)
            .filter(Document.ingest_batch_id == doc.ingest_batch_id)
            .order_by(Document.id)
            .all()
        )
        sibling_ids = [s.id for s in siblings]
        if doc.id in sibling_ids:
            current_idx = sibling_ids.index(doc.id)
            if current_idx > 0:
                bundle_prev_id = sibling_ids[current_idx - 1]
            if current_idx < len(sibling_ids) - 1:
                bundle_next_id = sibling_ids[current_idx + 1]

    ctx = {
        "doc": doc,
        "mode": mode,
        "context": context,
        "case_id": doc.case_id,
        "summary_bullets": summary_bullets,
        "key_passages": key_passages,
        "reactions": reactions,
        "grounds": grounds,
        "claims_status": claims_status,
        "actions": actions,
        "relationships_out": relationships_out,
        "relationships_in": relationships_in,
        "prev_doc_id": prev_doc_id,
        "next_doc_id": next_doc_id,
        "originator_color": originator_color,
        "passage_claim_map": passage_claim_map,
        "pins": pins,
        "passage_pin_counts": passage_pin_counts,
        "first_child_id": first_child_id,
        "bundle_prev_id": bundle_prev_id,
        "bundle_next_id": bundle_next_id,
    }

    if cases is not None:
        is_draft_case = False
        if doc.case_id and doc.case_id != "_TRIAGE":
            _case = db.query(Case).filter(Case.id == doc.case_id).first()
            if _case:
                is_draft_case = _case.is_draft
        ctx["cases"] = cases
        ctx["OriginatorType"] = _OriginatorType
        ctx["is_draft_case"] = is_draft_case

    return ctx
