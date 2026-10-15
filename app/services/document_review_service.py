"""The document review/reader view model, shared by the SPA routes and the MCP layer.

``review_fields`` is the single builder of everything the review panel shows
(key passages with stable ids, reactions, pins, relationships, summary, claims
grounds, actions); ``review_view`` / ``reader_view`` wrap it in the response
schemas. Access checks stay with the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import Case, Document, Proceeding, User
from app.models.enums import (
    OriginatorType,
    RelationshipConfidence,
    StageStatus,
)
from app.repositories.case import CaseRepository
from app.schemas.case_detail import EvidenceProposalView
from app.schemas.document_review import (
    ActionView,
    CaseRef,
    CostSignalView,
    DocumentReader,
    DocumentReview,
    GroundView,
    KeyPassage,
    MetadataField,
    OcrUnverifiedPage,
    PinView,
    PipelineView,
    ProceedingRef,
    ReactionView,
    ReaderNav,
    RelationshipView,
    ResolvedReference,
    StageView,
    SummaryBullet,
    SummaryView,
)
from app.services import access_service, api_token_service, family_context
from app.services.hud_context import build_hud_context
from app.services.markdown_render import render_highlighted
from app.services.pipeline_status import STAGE_REGISTRY, stages_dict

logger = logging.getLogger(__name__)

METADATA_FIELDS = (
    ("originator_type", "Originator"),
    ("document_type", "Type"),
    ("sender", "Sender"),
    ("internal_id", "Reference"),
    ("issued_date", "Issued"),
    ("received_date", "Received"),
    ("significance_tier", "Tier"),
    ("az_court", "AZ"),
)


def case_ref(case: Case) -> CaseRef:
    return CaseRef(id=case.id, title=case.title, is_draft=case.is_draft)


def proceeding_ref(p: Proceeding) -> ProceedingRef:
    return ProceedingRef(
        id=p.id,
        case_id=p.case_id,
        court_name=p.court_name,
        az_court=p.az_court,
        court_level=p.court_level.value if p.court_level else "other",
        is_draft=bool(p.is_draft),
    )


def pipeline_view(doc: Document) -> PipelineView:
    stages = stages_dict(doc)
    out = []
    for spec in sorted(STAGE_REGISTRY.values(), key=lambda s: s.order):
        rec = stages.get(spec.stage.value) or {}
        status = rec.get("status")
        out.append(
            StageView(
                key=spec.stage,
                label=spec.label,
                icon=spec.icon,
                status=StageStatus(status) if status else None,
                error=rec.get("error"),
                attempt=rec.get("attempt"),
                max_attempts=rec.get("max_attempts"),
                next_at=rec.get("next_at"),
                completed_at=rec.get("completed_at"),
            )
        )
    meta = doc.meta or {}
    failures = meta.get("page_failures") or []
    # The extractor only lists a page in ocr_unverified_pages after storing its
    # crosscheck words on the chunk, so every page listed here has them.
    unverified = {int(p) for p in meta.get("ocr_unverified_pages") or []}
    return PipelineView(
        state=doc.pipeline_state,
        stages=out,
        ocr_page_failures=sorted(int(p) for p in failures),
        ocr_crosscheck_unavailable=bool(meta.get("ocr_crosscheck_unavailable")),
        ocr_unverified=[
            OcrUnverifiedPage(
                page=chunk["meta"]["page"],
                words=chunk["meta"].get("crosscheck", {}).get("unsupported", []),
            )
            for chunk in sorted(
                (
                    c
                    for c in meta.get("chunks") or []
                    # Docling chunks (the Chandra fallback) carry no page.
                    if c["meta"].get("page") in unverified
                ),
                key=lambda c: c["meta"]["page"],
            )
        ],
    )


def field_value(doc: Document, field: str) -> str | None:
    value = getattr(doc, field)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return getattr(value, "value", value)


def review_view(db: Session, user: User, doc: Document) -> DocumentReview:
    return DocumentReview(**review_fields(db, user, doc)[0])


def pin_view(pin, names: dict[int | None, str] | None = None) -> PinView:
    return PinView(
        id=pin.id,
        passage_id=pin.passage_id,
        note=pin.note,
        user_id=pin.user_id,
        updated_at=pin.updated_at,
        agent_token_name=(names or {}).get(pin.agent_token_id),
    )


def reader_view(db: Session, user: User, doc: Document) -> DocumentReader:
    """The review view plus everything only the full-screen HUD shows."""
    fields, ctx = review_fields(db, user, doc)
    names = api_token_service.token_names(db, (p.agent_token_id for p in ctx["pins"]))
    body_html = (
        str(
            render_highlighted(
                doc.content,
                ctx["key_passages"],
                ctx["passage_claim_map"],
                ctx["claim_excerpt_map"],
            )
        )
        if doc.content
        else None
    )
    return DocumentReader(
        **fields,
        body_html=body_html,
        pins=[pin_view(p, names) for p in ctx["pins"]],
        nav=ReaderNav(
            prev_doc_id=ctx["prev_doc_id"],
            next_doc_id=ctx["next_doc_id"],
            position=ctx["doc_position"],
            total=ctx["proceeding_total"],
            parent_id=doc.parent_id,
            first_child_id=ctx["first_child_id"],
            bundle_prev_id=ctx["bundle_prev_id"],
            bundle_next_id=ctx["bundle_next_id"],
        ),
        thread_open=bool(doc.thread_open),
        context_strategy=(doc.meta or {}).get("ai_context_strategy"),
        context_windows=(doc.meta or {}).get("ai_context_windows"),
        has_original=bool(doc.file_path),
    )


def resolved_reference(db: Session, doc: Document) -> ResolvedReference:
    fam = family_context.resolve(db, doc)
    return ResolvedReference(
        case_id=fam.case_id,
        case_source=fam.case_source,
        az_court=fam.proceeding.az_court if fam.proceeding else None,
        az_source=fam.az_source,
        inherited_from_doc_id=fam.inherited_from.id if fam.inherited_from else None,
        inherited_from_title=fam.inherited_from.title if fam.inherited_from else None,
        exhibit_to_title=fam.exhibit_to.title if fam.exhibit_to else None,
    )


def review_fields(db: Session, user: User, doc: Document) -> tuple[dict, dict]:
    cases = list(CaseRepository(db).list_for_picker(owner_id=user.id))
    ctx = build_hud_context(
        db, doc, viewer=user, mode="review", context="embedded", cases=cases
    )
    names = api_token_service.token_names(
        db,
        [
            *(r.agent_token_id for r in ctx["reactions"]),
            *(a.agent_token_id for a in ctx["actions"]),
            *(
                r["rel_obj"].agent_token_id
                for rels in (ctx["relationships_out"], ctx["relationships_in"])
                for r in rels
            ),
        ],
    )
    conf = doc.extraction_confidence or {}
    editable = access_service.editable_case_ids(db, user)
    proc_q = db.query(Proceeding).order_by(Proceeding.court_name.asc())
    if editable is not None:
        proc_q = proc_q.filter(Proceeding.case_id.in_(editable))
    enrich = (stages_dict(doc).get("enrich") or {}).get("status")
    fields: dict = {
        "id": doc.id,
        "title": doc.title,
        "original_filename": doc.original_filename,
        "page_count": doc.page_count or 0,
        "packet_starts": [int(p) for p in (doc.meta or {}).get("packet_starts") or []],
        "case_id": doc.case_id,
        "case": case_ref(ctx["current_case"]) if ctx.get("current_case") else None,
        "proceeding": proceeding_ref(doc.proceeding) if doc.proceeding else None,
        "reference": resolved_reference(db, doc),
        "originator_type": doc.originator_type,
        "attributed_originator": doc.attributed_originator,
        "court_relay": bool(doc.court_relay),
        "sender": doc.sender,
        "internal_id": doc.internal_id,
        "az_court": doc.az_court,
        "issued_date": doc.issued_date,
        "received_date": doc.received_date,
        "ingest_date": doc.ingest_date,
        "document_type": doc.document_type,
        "significance_tier": doc.significance_tier,
        "needs_review": bool(doc.needs_review),
        "review_reasons": list(doc.review_reasons or []),
        "content_hash": doc.content_hash,
        "metadata": [
            MetadataField(
                field=field,
                label=label,
                value=field_value(doc, field),
                confidence=conf.get(field)
                if conf.get(field) in ("high", "medium", "low")
                and field_value(doc, field) is not None
                else None,
            )
            for field, label in METADATA_FIELDS
        ],
        "pipeline": pipeline_view(doc),
        "summary": SummaryView(
            bullets=[
                SummaryBullet(**b)
                for b in ctx["summary_bullets"]
                if b.get("kind") in ("legal", "action", "finance")
            ],
            approved_at=doc.ai_summary_approved_at,
            created_at=doc.ai_summary_created_at,
            enrich_status=StageStatus(enrich) if enrich else None,
        ),
        "key_passages": [
            KeyPassage(
                id=p["id"],
                text=p["text"],
                kind=p.get("kind"),
                page=p.get("page"),
                rationale=p.get("rationale"),
                start_offset=p.get("start_offset"),
                end_offset=p.get("end_offset"),
                claim_id=ctx["passage_claim_map"].get(p["id"]),
                pin_count=ctx["passage_pin_counts"].get(p["id"], 0),
            )
            for p in ctx["key_passages"]
        ],
        "relationships": [
            RelationshipView(
                id=r["rel_obj"].id,
                doc_id=r["id"],
                title=r["title"],
                originator_type=r["originator_type"] or OriginatorType.UNKNOWN,
                rel_type=r["rel_obj"].relationship_type,
                confidence=r["rel_obj"].confidence
                or RelationshipConfidence.AI_DETECTED,
                direction=side,  # type: ignore[arg-type]
                agent_token_name=names.get(r["rel_obj"].agent_token_id),
            )
            for side, rels in (
                ("out", ctx["relationships_out"]),
                ("in", ctx["relationships_in"]),
            )
            for r in rels
        ],
        "grounds": [
            GroundView(
                id=c.id,
                claim_text=c.claim_text,
                claim_type=c.claim_type,
                status=c.status,
                is_precedent=c.is_precedent,
                first_made_at=c.first_made_at,
            )
            for c in ctx["grounds"]
        ],
        "evidence_proposals": [
            EvidenceProposalView(
                proposal_id=p.id,
                proposed_role=p.proposed_role,
                excerpt=p.excerpt,
                target_claim_id=p.target_claim_id,
                target_claim_text=ctx["proposal_targets"][p.target_claim_id].claim_text,
                target_claim_status=ctx["proposal_targets"][p.target_claim_id].status,
                source_document_id=doc.id,
                source_document_title=doc.title,
            )
            for p in ctx["evidence_proposals_for_doc"]
            if target_known(p, ctx["proposal_targets"])
        ],
        "contradiction_notes": (
            [str(n) for n in (doc.meta or {}).get("contradiction_notes") or []]
            if (doc.meta or {}).get("ai_contradiction")
            else []
        ),
        "claims_status": ctx["claims_status"],
        "actions": [
            ActionView(
                id=a.id,
                title=a.title,
                description=a.description,
                due_date=a.due_date,
                action_type=a.action_type,
                status=a.status,
                location=a.location,
                addressee=a.addressee,
                agent_token_name=names.get(a.agent_token_id),
            )
            for a in ctx["actions"]
        ],
        "cost_signals": [
            CostSignalView(
                id=s.id,
                signal_type=s.signal_type,
                amount=s.amount,
                description=s.description,
                issued_at=s.issued_at,
            )
            for s in doc.cost_signals
        ],
        "reactions": [
            ReactionView(
                reaction=r.reaction,
                notes=r.notes,
                created_at=r.ingest_date,
                agent_token_name=names.get(r.agent_token_id),
            )
            for r in ctx["reactions"]
        ],
        "bundle_prev_id": ctx["bundle_prev_id"],
        "bundle_next_id": ctx["bundle_next_id"],
        "cases": [case_ref(c) for c in ctx["cases"]],
        "proceedings": [proceeding_ref(p) for p in proc_q.all()],
    }
    return fields, ctx


def target_known(proposal, targets: dict) -> bool:
    known = proposal.target_claim_id in targets
    if not known:
        logger.debug(
            "skipping evidence proposal %s: target claim %s missing",
            proposal.id,
            proposal.target_claim_id,
        )
    return known
