"""Second-opinion findings on AI output (``AiReviewFinding``).

A reviewer (an MCP agent, or the opt-in verifier model,
``app/services/intelligence/verifier.py``) records whether it
agrees with one piece of AI output: a document field, the summary, a
relationship, a claim, ... (``ReviewTargetKind``). ``agree`` is kept as
``noted``; ``disagree`` and ``uncertain`` wait as ``pending`` for the user.
Confirming a ``disagree`` applies ``proposed_value`` through the service that
owns the target (never a direct column write); dismissing leaves the output
alone. When a pipeline stage (or the brief) rewrites the output, a pending
finding whose ``ai_value`` no longer matches becomes ``stale`` (``mark_stale``).

Access is the caller's job: the REST guard and the MCP principal decide who may
record, read and resolve. This module knows nothing about HTTP; failures raise
:class:`ReviewFindingError`.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.timezone import now_utc
from app.models.database import (
    ActionItem,
    AiReviewFinding,
    Case,
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    CostSignal,
    Document,
    DocumentPipelineStage,
    DocumentRelationship,
    Entity,
    Proceeding,
    User,
)
from app.models.enums import (
    ActionItemStatus,
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    DocumentType,
    OriginatorType,
    PipelineStage,
    ProposalStatus,
    ReviewerKind,
    ReviewFindingStatus,
    ReviewTargetKind,
    ReviewVerdict,
    SignificanceTier,
)
from app.schemas.review_findings import ReviewFindingView
from app.services import api_token_service
from app.services.timezone_service import local_date_str

logger = logging.getLogger(__name__)

_TRIAGE = "_TRIAGE"
MAX_TEXT = 4000

DOCUMENT_FIELDS = (
    "title",
    "significance_tier",
    "document_type",
    "originator_type",
    "sender",
    "issued_date",
    "received_date",
    "az_court",
    "internal_id",
)
_DATE_FIELDS = ("issued_date", "received_date")
_ENUM_FIELDS: dict[str, type] = {
    "significance_tier": SignificanceTier,
    "document_type": DocumentType,
    "originator_type": OriginatorType,
}
SUMMARY_KEYS = ("legal_significance", "required_action", "financial_impact")
CLAIM_FIELDS = ("status", "text")
ACTION_ITEM_FIELDS = ("due_date", "title", "status")
COST_SIGNAL_FIELDS = ("client_role",)
CLIENT_ROLES = ("winner", "loser", "unset")
BRIEF_FIELDS = ("posture", "next_move", "detected_status")

# The pipeline stage whose output a finding reviews (its model and prompt
# version are copied onto the finding). Document fields split by who writes
# them: ENRICH decides title/type/tier, METADATA the rest.
_STAGE_BY_KIND: dict[ReviewTargetKind, PipelineStage] = {
    ReviewTargetKind.SUMMARY: PipelineStage.ENRICH,
    ReviewTargetKind.CASE_ASSIGNMENT: PipelineStage.METADATA,
    ReviewTargetKind.RELATIONSHIP: PipelineStage.RELATIONSHIPS,
    ReviewTargetKind.CLAIM: PipelineStage.CLAIMS,
    ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL: PipelineStage.CLAIMS,
    ReviewTargetKind.CLAIM_MERGE_PROPOSAL: PipelineStage.CLAIMS,
    ReviewTargetKind.ACTION_ITEM: PipelineStage.ENRICH,
    ReviewTargetKind.COST_SIGNAL: PipelineStage.ENRICH,
    ReviewTargetKind.ENTITY: PipelineStage.ENTITIES,
    ReviewTargetKind.OCR_TEXT: PipelineStage.EXTRACT,
}
_ENRICH_FIELDS = ("title", "significance_tier", "document_type")

# Targets that live on one document; the rest name a row by ``target_id``
# (and ``case_brief`` the case itself).
_DOCUMENT_KINDS = (
    ReviewTargetKind.DOCUMENT_FIELD,
    ReviewTargetKind.SUMMARY,
    ReviewTargetKind.CASE_ASSIGNMENT,
    ReviewTargetKind.OCR_TEXT,
)


class ReviewFindingError(Exception):
    """A refused finding or resolution, with its HTTP-equivalent status and code."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _not_found() -> ReviewFindingError:
    return ReviewFindingError(404, "not_found", "Review target not found.")


def _invalid(detail: str) -> ReviewFindingError:
    return ReviewFindingError(422, "validation_error", detail)


@dataclass
class Target:
    """The AI output a finding is about, resolved to its rows."""

    kind: ReviewTargetKind
    case_id: str
    document: Document | None
    row: Any  # relationship, claim, proposal, action item, cost signal, entity
    target_id: int | None
    field: str | None


# --- Resolving targets ----------------------------------------------------------


def _doc_case(doc: Document) -> str:
    return doc.case_id or _TRIAGE


def _claim_document(db: Session, claim_id: int) -> Document | None:
    """The claim's originating document (its ``asserts`` evidence), else any."""
    evidence = (
        db.query(ClaimEvidence)
        .filter(ClaimEvidence.claim_id == claim_id)
        .order_by(
            (ClaimEvidence.role != ClaimEvidenceRole.ASSERTS),
            ClaimEvidence.id,
        )
        .first()
    )
    return db.get(Document, evidence.document_id) if evidence else None


def resolve_target(
    db: Session,
    kind: ReviewTargetKind,
    *,
    document_id: int | None = None,
    case_id: str | None = None,
    target_id: int | None = None,
    field: str | None = None,
) -> Target:
    """Load the rows behind a target and check the identifiers fit together.

    The case is always derived from the document or row; a ``case_id`` or
    ``document_id`` that does not match it is "not found".
    """
    field = (field or "").strip() or None
    doc: Document | None = None
    row: Any = None
    derived_case: str | None = None

    if kind in _DOCUMENT_KINDS:
        if document_id is None:
            raise _invalid(f"{kind.value} needs document_id.")
        if target_id is not None:
            raise _invalid(f"{kind.value} takes no target_id.")
        doc = db.get(Document, document_id)
        if doc is None:
            raise _not_found()
        derived_case = _doc_case(doc)
        if kind == ReviewTargetKind.DOCUMENT_FIELD:
            if field not in DOCUMENT_FIELDS:
                raise _invalid(f"field must be one of {', '.join(DOCUMENT_FIELDS)}.")
        elif field is not None:
            raise _invalid(f"{kind.value} takes no field.")
    elif kind == ReviewTargetKind.CASE_BRIEF:
        if not case_id:
            raise _invalid("case_brief needs case_id.")
        if document_id is not None or target_id is not None:
            raise _invalid("case_brief takes neither document_id nor target_id.")
        if field not in BRIEF_FIELDS:
            raise _invalid(f"field must be one of {', '.join(BRIEF_FIELDS)}.")
        case = db.get(Case, case_id)
        if case is None or case.id == _TRIAGE:
            raise _not_found()
        derived_case = case.id
    else:
        if target_id is None:
            raise _invalid(f"{kind.value} needs target_id.")
        row, doc, derived_case = _resolve_row(db, kind, target_id, field)
        if document_id is not None and (doc is None or doc.id != document_id):
            if not _row_touches_document(db, kind, row, document_id):
                raise _not_found()
            doc = db.get(Document, document_id)
            if doc is None:
                raise _not_found()
            derived_case = _doc_case(doc)

    assert derived_case is not None
    if case_id and case_id != derived_case:
        raise _not_found()
    return Target(
        kind=kind,
        case_id=derived_case,
        document=doc,
        row=row,
        target_id=target_id,
        field=field,
    )


def _resolve_row(
    db: Session, kind: ReviewTargetKind, target_id: int, field: str | None
) -> tuple[Any, Document | None, str]:
    def _no_field() -> None:
        if field is not None:
            raise _invalid(f"{kind.value} takes no field.")

    if kind == ReviewTargetKind.RELATIONSHIP:
        _no_field()
        rel = db.get(DocumentRelationship, target_id)
        doc = db.get(Document, rel.from_document_id) if rel else None
        if rel is None or doc is None:
            raise _not_found()
        return rel, doc, _doc_case(doc)
    if kind == ReviewTargetKind.CLAIM:
        if field not in CLAIM_FIELDS:
            raise _invalid(f"field must be one of {', '.join(CLAIM_FIELDS)}.")
        claim = db.get(Claim, target_id)
        if claim is None or claim.dismissed_at is not None:
            raise _not_found()
        doc = _claim_document(db, claim.id)
        if doc is None:
            raise _not_found()
        return claim, doc, _doc_case(doc)
    if kind == ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL:
        _no_field()
        ev = db.get(ClaimEvidenceProposal, target_id)
        doc = db.get(Document, ev.source_document_id) if ev else None
        if ev is None or doc is None:
            raise _not_found()
        return ev, doc, _doc_case(doc)
    if kind == ReviewTargetKind.CLAIM_MERGE_PROPOSAL:
        _no_field()
        merge = db.get(ClaimMergeProposal, target_id)
        doc = _claim_document(db, merge.new_claim_id) if merge else None
        if merge is None or doc is None:
            raise _not_found()
        return merge, doc, _doc_case(doc)
    if kind == ReviewTargetKind.ACTION_ITEM:
        if field not in ACTION_ITEM_FIELDS:
            raise _invalid(f"field must be one of {', '.join(ACTION_ITEM_FIELDS)}.")
        item = db.get(ActionItem, target_id)
        if item is None:
            raise _not_found()
        src = (
            db.get(Document, item.source_document_id)
            if item.source_document_id
            else None
        )
        return item, src, item.case_id
    if kind == ReviewTargetKind.COST_SIGNAL:
        if field not in COST_SIGNAL_FIELDS:
            raise _invalid("field must be client_role.")
        signal = db.get(CostSignal, target_id)
        if signal is None:
            raise _not_found()
        return signal, db.get(Document, signal.source_document_id), signal.case_id
    if kind == ReviewTargetKind.ENTITY:
        _no_field()
        entity = db.get(Entity, target_id)
        if entity is None:
            raise _not_found()
        src = (
            db.get(Document, entity.source_document_id)
            if entity.source_document_id
            else None
        )
        return entity, src, entity.case_id
    raise _invalid(f"Unknown target kind {kind.value}.")


def _row_touches_document(
    db: Session, kind: ReviewTargetKind, row: Any, document_id: int
) -> bool:
    """Whether an explicit ``document_id`` is a legitimate anchor for ``row``."""
    if kind == ReviewTargetKind.RELATIONSHIP:
        return document_id in (row.from_document_id, row.to_document_id)
    if kind == ReviewTargetKind.CLAIM:
        return (
            db.query(ClaimEvidence.id)
            .filter(
                ClaimEvidence.claim_id == row.id,
                ClaimEvidence.document_id == document_id,
            )
            .first()
            is not None
        )
    return False


def _target_of(db: Session, finding: AiReviewFinding) -> Target | None:
    """The finding's target as it is now; None once it no longer exists."""
    try:
        if finding.target_kind == ReviewTargetKind.CASE_BRIEF:
            return resolve_target(
                db, finding.target_kind, case_id=finding.case_id, field=finding.field
            )
        return resolve_target(
            db,
            finding.target_kind,
            document_id=finding.document_id,
            target_id=finding.target_id,
            field=finding.field,
        )
    except ReviewFindingError:
        return None


# --- Current values -------------------------------------------------------------


def _json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return local_date_str(value)
    if hasattr(value, "value"):  # enums
        return value.value
    return value


def summary_value(ai_summary: Any) -> dict[str, Any] | None:
    from app.services.case_dashboard_service import summary_bullets_from_ai_summary

    if not ai_summary:
        return None
    if isinstance(ai_summary, dict):
        return {k: ai_summary.get(k) for k in SUMMARY_KEYS}
    by_kind = {
        b["kind"]: b["text"] for b in summary_bullets_from_ai_summary(ai_summary)
    }
    return {
        "legal_significance": by_kind.get("legal"),
        "required_action": by_kind.get("action"),
        "financial_impact": by_kind.get("finance"),
    }


def _content_hash(doc: Document) -> str:
    return hashlib.sha256((doc.content or "").encode()).hexdigest()


def current_value(db: Session, target: Target) -> Any:
    """The AI output as it is now, as JSON (enum values, ISO dates)."""
    kind, doc, row, field = target.kind, target.document, target.row, target.field
    if kind == ReviewTargetKind.DOCUMENT_FIELD:
        assert doc is not None and field is not None
        return _json(getattr(doc, field))
    if kind == ReviewTargetKind.SUMMARY:
        assert doc is not None
        return summary_value(doc.ai_summary)
    if kind == ReviewTargetKind.CASE_ASSIGNMENT:
        assert doc is not None
        return {"case_id": doc.case_id, "proceeding_id": doc.proceeding_id}
    if kind == ReviewTargetKind.OCR_TEXT:
        assert doc is not None
        return {"content_sha256": _content_hash(doc)}
    if kind == ReviewTargetKind.RELATIONSHIP:
        return {
            "from_document_id": row.from_document_id,
            "to_document_id": row.to_document_id,
            "relationship_type": _json(row.relationship_type),
            "confidence": _json(row.confidence),
        }
    if kind == ReviewTargetKind.CLAIM:
        return _json(row.status) if field == "status" else row.claim_text
    if kind == ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL:
        return {
            "target_claim_id": row.target_claim_id,
            "proposed_role": _json(row.proposed_role),
            "excerpt": row.excerpt,
            "status": _json(row.status),
        }
    if kind == ReviewTargetKind.CLAIM_MERGE_PROPOSAL:
        return {
            "new_claim_id": row.new_claim_id,
            "existing_claim_id": row.existing_claim_id,
            "status": _json(row.status),
        }
    if kind == ReviewTargetKind.ACTION_ITEM:
        assert field is not None
        return _json(getattr(row, field))
    if kind == ReviewTargetKind.COST_SIGNAL:
        return (row.allocation or {}).get("client_role")
    if kind == ReviewTargetKind.ENTITY:
        return {"type": _json(row.type), "name": row.name}
    if kind == ReviewTargetKind.CASE_BRIEF:
        case = db.get(Case, target.case_id)
        assert case is not None and field is not None
        return (case.ai_brief or {}).get(field)
    raise AssertionError(kind)


def _snapshot(db: Session, target: Target, proposed: Any) -> Any:
    """``current_value`` plus, for OCR, the page text the reviewer disputes."""
    value = current_value(db, target)
    if target.kind == ReviewTargetKind.OCR_TEXT and isinstance(proposed, dict):
        from app.services.ingestion.page_text import page_texts

        assert target.document is not None
        texts = page_texts(target.document)
        value["pages"] = [
            {"page": p["page"], "text": texts.get(p["page"])}
            for p in proposed.get("pages") or []
        ]
    return value


def _same_output(kind: ReviewTargetKind, recorded: Any, now: Any) -> bool:
    if kind == ReviewTargetKind.OCR_TEXT:
        return (recorded or {}).get("content_sha256") == (now or {}).get(
            "content_sha256"
        )
    return recorded == now


# --- Validation -----------------------------------------------------------------


def _text(value: Any, name: str, *, max_len: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{name} must be a non-empty string.")
    value = value.strip()
    if len(value) > max_len:
        raise _invalid(f"{name} is too long (max {max_len} characters).")
    return value


def _iso_date(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{name} must be an ISO date (YYYY-MM-DD).")
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise _invalid(f"{name} must be an ISO date (YYYY-MM-DD).") from None


def _enum_value(enum_cls: type, value: Any, name: str) -> str:
    allowed = [e.value for e in enum_cls]  # type: ignore[attr-defined]
    if value not in allowed:
        raise _invalid(f"{name} must be one of {', '.join(allowed)}.")
    return str(value)


def _validate_proposed(
    db: Session,
    target: Target,
    proposed: Any,
    can_edit_case: Callable[[str], bool],
) -> Any:
    """Check a ``disagree``'s replacement against the target; return it normalised.

    ``None`` means "no replacement": allowed where confirming without one has a
    meaning (reject a relationship, dismiss a proposal or an action item,
    dismiss a misstated claim) or nothing is applied (entity).
    """
    kind, field = target.kind, target.field
    if kind == ReviewTargetKind.DOCUMENT_FIELD:
        assert field is not None
        if proposed is None:
            raise _invalid("A disagreement on a document field needs proposed_value.")
        if field in _ENUM_FIELDS:
            return _enum_value(_ENUM_FIELDS[field], proposed, field)
        if field in _DATE_FIELDS:
            return _iso_date(proposed, field)
        return _text(proposed, field, max_len=500)
    if kind == ReviewTargetKind.SUMMARY:
        if not isinstance(proposed, dict) or set(proposed) - set(SUMMARY_KEYS):
            raise _invalid(
                "proposed_value must be an object with "
                "legal_significance, required_action and financial_impact."
            )
        out = {
            k: (_text(proposed[k], k) if proposed.get(k) is not None else None)
            for k in SUMMARY_KEYS
        }
        if not any(out.values()):
            raise _invalid("The proposed summary is empty.")
        return out
    if kind == ReviewTargetKind.CASE_ASSIGNMENT:
        if not isinstance(proposed, dict) or not isinstance(
            proposed.get("case_id"), str
        ):
            raise _invalid("proposed_value must be {case_id, proceeding_id?}.")
        case_id = proposed["case_id"].strip()
        case = db.get(Case, case_id)
        if case is None or case.id == _TRIAGE or not can_edit_case(case.id):
            raise _invalid(f"Case {case_id} does not exist or cannot be edited.")
        proceeding_id = proposed.get("proceeding_id")
        if proceeding_id is not None:
            proc = (
                db.get(Proceeding, proceeding_id)
                if isinstance(proceeding_id, int)
                else None
            )
            if proc is None or proc.case_id != case.id:
                raise _invalid("proceeding_id does not belong to that case.")
        return {"case_id": case.id, "proceeding_id": proceeding_id}
    if kind == ReviewTargetKind.OCR_TEXT:
        pages = proposed.get("pages") if isinstance(proposed, dict) else None
        if not isinstance(pages, list) or not pages:
            raise _invalid("proposed_value must be {pages: [{page, text}, ...]}.")
        out_pages = []
        for p in pages:
            if not isinstance(p, dict) or not isinstance(p.get("page"), int):
                raise _invalid("Each proposed page needs an integer page and a text.")
            out_pages.append({"page": p["page"], "text": _text(p.get("text"), "text")})
        return {"pages": out_pages}
    if kind in (
        ReviewTargetKind.RELATIONSHIP,
        ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL,
        ReviewTargetKind.CLAIM_MERGE_PROPOSAL,
    ):
        if proposed is not None:
            raise _invalid(
                f"A disagreement on a {kind.value} takes no proposed_value: "
                "confirming it rejects the AI's suggestion."
            )
        return None
    if kind == ReviewTargetKind.CLAIM:
        if field == "status":
            return _enum_value(ClaimStatus, proposed, "status")
        # Claim text has no edit path: confirming a disagreement dismisses the
        # claim, so there is no wording to propose (as for relationships).
        if proposed is not None:
            raise _invalid(
                "Claim text cannot be rewritten: a disagreement dismisses the "
                "claim, so leave proposed_value empty and say why in rationale."
            )
        return None
    if kind == ReviewTargetKind.ACTION_ITEM:
        if field == "status":
            return _enum_value(ActionItemStatus, proposed, "status")
        if proposed is None:  # no replacement = dismiss the item
            return None
        return (
            _iso_date(proposed, "due_date")
            if field == "due_date"
            else _text(proposed, "title", max_len=300)
        )
    if kind == ReviewTargetKind.COST_SIGNAL:
        if proposed not in CLIENT_ROLES:
            raise _invalid(f"client_role must be one of {', '.join(CLIENT_ROLES)}.")
        return proposed
    if kind == ReviewTargetKind.ENTITY:
        return proposed
    if kind == ReviewTargetKind.CASE_BRIEF:
        if field == "detected_status":
            return _enum_value(CaseStatus, proposed, "detected_status")
        return _text(proposed, field or "value")
    raise AssertionError(kind)


# --- Provenance -----------------------------------------------------------------


def _provenance(db: Session, target: Target) -> tuple[str | None, str | None]:
    if target.kind == ReviewTargetKind.CASE_BRIEF:
        case = db.get(Case, target.case_id)
        assert case is not None
        return case.ai_brief_model, case.ai_brief_prompt_version
    if target.document is None:
        return None, None
    if target.kind == ReviewTargetKind.DOCUMENT_FIELD:
        stage = (
            PipelineStage.ENRICH
            if target.field in _ENRICH_FIELDS
            else PipelineStage.METADATA
        )
    else:
        stage = _STAGE_BY_KIND[target.kind]
    row = db.get(DocumentPipelineStage, (target.document.id, stage.value))
    if row is None:
        return None, None
    return row.model, row.prompt_version


# --- Recording ------------------------------------------------------------------


def _key_filter(
    db: Session,
    kind,
    token_id,
    target: Target,
):
    return db.query(AiReviewFinding).filter(
        AiReviewFinding.reviewer_kind == kind,
        AiReviewFinding.agent_token_id.is_(None)
        if token_id is None
        else AiReviewFinding.agent_token_id == token_id,
        AiReviewFinding.target_kind == target.kind,
        AiReviewFinding.case_id == target.case_id,
        AiReviewFinding.document_id.is_(None)
        if target.document is None
        else AiReviewFinding.document_id == target.document.id,
        AiReviewFinding.target_id.is_(None)
        if target.target_id is None
        else AiReviewFinding.target_id == target.target_id,
        AiReviewFinding.field.is_(None)
        if target.field is None
        else AiReviewFinding.field == target.field,
    )


# A re-review replaces the reviewer's open finding, or the "agree" it left
# before, so repeated audits do not pile up rows; resolved and stale findings
# stay as the record of what was decided.
_REPLACEABLE = (ReviewFindingStatus.PENDING, ReviewFindingStatus.NOTED)


def _existing(db: Session, kind, token_id, target: Target) -> AiReviewFinding | None:
    q = _key_filter(db, kind, token_id, target)
    return (
        q.filter(AiReviewFinding.status == ReviewFindingStatus.PENDING).first()
        or q.filter(AiReviewFinding.status == ReviewFindingStatus.NOTED)
        .order_by(AiReviewFinding.id.desc())
        .first()
    )


def record_finding(
    db: Session,
    target: Target,
    *,
    reviewer_kind: ReviewerKind,
    agent_token_id: int | None,
    verdict: ReviewVerdict,
    rationale: str,
    proposed_value: Any = None,
    cited_excerpt: str | None = None,
    can_edit_case: Callable[[str], bool],
    _retried: bool = False,
) -> AiReviewFinding:
    """Record (or update the reviewer's open) finding on ``target``. Caller commits.

    ``proposed_value`` is validated against the target for ``disagree`` and
    must be absent otherwise. A reviewer's pending finding on the same target
    is updated in place rather than duplicated, and so is its earlier agreement
    with the same output when it agrees again.
    """
    rationale = _text(rationale, "rationale")
    excerpt = (
        _text(cited_excerpt, "cited_excerpt")
        if cited_excerpt and cited_excerpt.strip()
        else None
    )
    if verdict == ReviewVerdict.DISAGREE:
        proposed = _validate_proposed(db, target, proposed_value, can_edit_case)
    elif proposed_value is not None:
        raise _invalid("proposed_value is only given with verdict disagree.")
    else:
        proposed = None
    model, prompt_version = _provenance(db, target)
    status = (
        ReviewFindingStatus.NOTED
        if verdict == ReviewVerdict.AGREE
        else ReviewFindingStatus.PENDING
    )

    finding = _existing(db, reviewer_kind, agent_token_id, target)
    if finding is None:
        finding = AiReviewFinding(
            reviewer_kind=reviewer_kind,
            agent_token_id=agent_token_id,
            target_kind=target.kind,
            document_id=target.document.id if target.document else None,
            case_id=target.case_id,
            target_id=target.target_id,
            field=target.field,
        )
        db.add(finding)
    finding.verdict = verdict
    finding.ai_value = _snapshot(db, target, proposed)
    finding.proposed_value = proposed
    finding.rationale = rationale
    finding.cited_excerpt = excerpt
    finding.reviewed_model = model
    finding.reviewed_prompt_version = prompt_version
    finding.status = status
    finding.created_at = now_utc()
    finding.resolved_at = None
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # Another reviewer call opened the same pending finding between the
        # lookup above and this write: update that one instead (once; any
        # other integrity error is a real one).
        if _retried:
            raise
        db.expire_all()
        return record_finding(
            db,
            target,
            reviewer_kind=reviewer_kind,
            agent_token_id=agent_token_id,
            verdict=verdict,
            rationale=rationale,
            proposed_value=proposed_value,
            cited_excerpt=cited_excerpt,
            can_edit_case=can_edit_case,
            _retried=True,
        )
    _refresh_document_reasons(db, target.document)
    return finding


def _refresh_document_reasons(db: Session, doc: Document | None) -> None:
    from app.services.ingestion.service import refresh_review_reasons

    if doc is not None:
        db.flush()
        refresh_review_reasons(doc, db, commit=False)


# --- Reading --------------------------------------------------------------------


def for_document(db: Session, document_id: int) -> list[AiReviewFinding]:
    return (
        db.query(AiReviewFinding)
        .filter(AiReviewFinding.document_id == document_id)
        .order_by(AiReviewFinding.created_at.desc(), AiReviewFinding.id.desc())
        .all()
    )


def for_case(
    db: Session, case_id: str, status: ReviewFindingStatus | None = None
) -> list[AiReviewFinding]:
    """The case's findings: on the case itself (the brief) and on the documents
    filed in it now."""
    q = (
        db.query(AiReviewFinding)
        .outerjoin(Document, Document.id == AiReviewFinding.document_id)
        .filter(
            or_(
                Document.case_id == case_id,
                (AiReviewFinding.document_id.is_(None))
                & (AiReviewFinding.case_id == case_id),
            )
        )
    )
    if status is not None:
        q = q.filter(AiReviewFinding.status == status)
    return q.order_by(
        AiReviewFinding.created_at.desc(), AiReviewFinding.id.desc()
    ).all()


def has_pending_disagreement(db: Session, document_id: int) -> bool:
    return (
        db.query(AiReviewFinding.id)
        .filter(
            AiReviewFinding.document_id == document_id,
            AiReviewFinding.status == ReviewFindingStatus.PENDING,
            AiReviewFinding.verdict == ReviewVerdict.DISAGREE,
        )
        .first()
        is not None
    )


# --- Staleness ------------------------------------------------------------------


def mark_stale(
    db: Session, *, document_id: int | None = None, case_id: str | None = None
) -> int:
    """Mark pending findings whose AI output changed since they were recorded.

    ``document_id``: the findings on that document (called when a stage on it
    completes); ``case_id``: the findings on the case itself (the brief). A
    target that no longer exists counts as changed. Caller commits. Returns
    how many became stale.
    """
    q = db.query(AiReviewFinding).filter(
        AiReviewFinding.status == ReviewFindingStatus.PENDING
    )
    if document_id is not None:
        q = q.filter(AiReviewFinding.document_id == document_id)
    elif case_id is not None:
        q = q.filter(
            AiReviewFinding.case_id == case_id, AiReviewFinding.document_id.is_(None)
        )
    else:
        return 0
    if q.first() is None:
        return 0
    # Compare against the rows as committed by whoever just wrote them (a stage
    # often writes through another session than the one completing it): write
    # out this session's own changes, then drop what it has cached.
    db.flush()
    db.expire_all()
    changed = 0
    for finding in q.all():
        target = _target_of(db, finding)
        if target is not None and _same_output(
            finding.target_kind, finding.ai_value, current_value(db, target)
        ):
            continue
        finding.status = ReviewFindingStatus.STALE
        changed += 1
    if changed and document_id is not None:
        _refresh_document_reasons(db, db.get(Document, document_id))
    return changed


# --- Case moves -----------------------------------------------------------------


def follow_documents(db: Session, case_ids: list[str]) -> None:
    """Re-file the findings on documents that left ``case_ids`` under the
    documents' current case, so deleting one of those cases (an orphaned
    draft) does not take them along. Caller commits."""
    if not case_ids:
        return
    moved = (
        db.query(AiReviewFinding, Document.case_id)
        .join(Document, Document.id == AiReviewFinding.document_id)
        .filter(AiReviewFinding.case_id.in_(case_ids))
        .all()
    )
    for finding, doc_case in moved:
        if (doc_case or _TRIAGE) != finding.case_id:
            finding.case_id = doc_case or _TRIAGE
    db.flush()


def move_case(db: Session, source_id: str, target_id: str) -> None:
    """A case merge: findings on documents move along; open ones on the
    source's own brief go stale (that brief is gone). Caller commits."""
    db.query(AiReviewFinding).filter(
        AiReviewFinding.case_id == source_id,
        AiReviewFinding.document_id.is_(None),
        AiReviewFinding.status == ReviewFindingStatus.PENDING,
    ).update(
        {AiReviewFinding.status: ReviewFindingStatus.STALE}, synchronize_session=False
    )
    db.query(AiReviewFinding).filter(AiReviewFinding.case_id == source_id).update(
        {AiReviewFinding.case_id: target_id}, synchronize_session=False
    )


# --- Resolving ------------------------------------------------------------------


def _pending(finding: AiReviewFinding) -> None:
    if finding.status != ReviewFindingStatus.PENDING:
        raise ReviewFindingError(
            409, "not_pending", f"This finding is {finding.status.value}."
        )


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def confirm(db: Session, user: User, finding: AiReviewFinding) -> AiReviewFinding:
    """Accept a pending finding (commits).

    A ``disagree`` applies its ``proposed_value`` through the target's own
    service; an ``uncertain`` is only acknowledged. A finding whose AI output
    has changed since it was recorded is marked stale instead (409).
    """
    _pending(finding)
    target = _target_of(db, finding)
    if target is None or not _same_output(
        finding.target_kind, finding.ai_value, current_value(db, target)
    ):
        finding.status = ReviewFindingStatus.STALE
        _refresh_document_reasons(
            db, db.get(Document, finding.document_id) if finding.document_id else None
        )
        db.commit()
        raise ReviewFindingError(
            409, "stale", "The AI output changed since this finding was recorded."
        )
    if finding.verdict == ReviewVerdict.DISAGREE:
        _apply(db, user, target, finding.proposed_value)
    db.refresh(finding)
    finding.status = ReviewFindingStatus.CONFIRMED
    finding.resolved_at = now_utc()
    db.flush()
    _after_resolution(db, finding)
    db.commit()
    db.refresh(finding)
    return finding


def dismiss(db: Session, finding: AiReviewFinding) -> AiReviewFinding:
    """Reject a pending finding; the AI output stays as it is (commits)."""
    _pending(finding)
    finding.status = ReviewFindingStatus.DISMISSED
    finding.resolved_at = now_utc()
    db.flush()
    _refresh_document_reasons(
        db, db.get(Document, finding.document_id) if finding.document_id else None
    )
    db.commit()
    db.refresh(finding)
    return finding


def _after_resolution(db: Session, finding: AiReviewFinding) -> None:
    """Other open findings on what was just changed may no longer apply."""
    if finding.document_id is not None:
        doc = db.get(Document, finding.document_id)
        if doc is not None:
            mark_stale(db, document_id=doc.id)
            _refresh_document_reasons(db, doc)
    else:
        mark_stale(db, case_id=finding.case_id)


def _apply(db: Session, user: User, target: Target, proposed: Any) -> None:
    """Apply a confirmed disagreement through the service that owns the target."""
    kind, doc, row, field = target.kind, target.document, target.row, target.field
    if kind == ReviewTargetKind.DOCUMENT_FIELD:
        from app.services.triage_confirmation import confirm_document

        assert doc is not None and field is not None
        value: Any = proposed
        if field in _DATE_FIELDS:
            value = _date(proposed)
        elif field in _ENUM_FIELDS:
            value = _ENUM_FIELDS[field](proposed)
        confirm_document(db, doc.id, finalize=False, **{field: value})
    elif kind == ReviewTargetKind.SUMMARY:
        from app.services import summary_service

        assert doc is not None
        summary_service.approve(doc, replacement=proposed)
        db.commit()
    elif kind == ReviewTargetKind.CASE_ASSIGNMENT:
        from app.services.triage_routing_service import (
            RoutingError,
            check_proceeding,
            require_editable_target,
            route_document,
        )

        assert doc is not None
        try:
            require_editable_target(db, user, proposed["case_id"])
            check_proceeding(db, proposed.get("proceeding_id"), proposed["case_id"])
            route_document(
                db, doc.id, proposed["case_id"], proposed.get("proceeding_id"), False
            )
        except RoutingError as exc:
            raise ReviewFindingError(exc.status, exc.code, exc.detail) from exc
    elif kind == ReviewTargetKind.OCR_TEXT:
        from app.services.stage_retry import RetryRefused, retry_document_stage

        assert doc is not None
        try:
            retry_document_stage(db, doc, PipelineStage.EXTRACT)
        except RetryRefused as exc:
            raise ReviewFindingError(409, exc.code, exc.detail) from exc
    elif kind == ReviewTargetKind.RELATIONSHIP:
        from app.services import relationship_review_service

        relationship_review_service.reject(db, row)
    elif kind == ReviewTargetKind.CLAIM:
        from app.services.claim_service import ClaimService

        try:
            if field == "status":
                ClaimService(db).transition_status(row.id, ClaimStatus(proposed))
            else:
                ClaimService(db).dismiss_claim(row.id)
        except ValueError as exc:
            raise ReviewFindingError(422, "bad_transition", str(exc)) from exc
        db.commit()
    elif kind == ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL:
        from app.services import claim_proposal_service

        if row.status == ProposalStatus.PENDING:
            claim_proposal_service.dismiss_evidence(row.id, db)
            _refresh_document_reasons(db, doc)
            db.commit()
    elif kind == ReviewTargetKind.CLAIM_MERGE_PROPOSAL:
        from app.services import claim_proposal_service

        if row.status == ProposalStatus.PENDING:
            claim_proposal_service.dismiss_merge(row.id, db)
            db.commit()
    elif kind == ReviewTargetKind.ACTION_ITEM:
        from app.services import action_item_service

        if field == "status":
            action_item_service.set_status(db, row, ActionItemStatus(proposed))
        elif proposed is None:
            action_item_service.set_status(db, row, ActionItemStatus.DISMISSED)
        else:
            try:
                action_item_service.update(
                    db,
                    row,
                    title=proposed if field == "title" else None,
                    due_date=_date(proposed) if field == "due_date" else None,
                )
            except action_item_service.ActionItemClash:
                raise ReviewFindingError(
                    409,
                    "clash",
                    "Another item of this type already falls on that date.",
                ) from None
    elif kind == ReviewTargetKind.COST_SIGNAL:
        from app.services.cost_service import set_client_role

        set_client_role(db, row, proposed)
    elif kind == ReviewTargetKind.ENTITY:
        return  # entities have no edit path yet: confirming only records it
    elif kind == ReviewTargetKind.CASE_BRIEF:
        from app.services.case_overview_service import request_brief_refresh
        from app.services.case_service import close_case

        case = db.get(Case, target.case_id)
        assert case is not None
        if field == "detected_status" and proposed == CaseStatus.CLOSED.value:
            close_case(db, case)
        else:
            request_brief_refresh(db, case)
    else:
        raise AssertionError(kind)


# --- Views ----------------------------------------------------------------------

_FIELD_LABELS = {
    "title": "Title",
    "significance_tier": "Significance tier",
    "document_type": "Document type",
    "originator_type": "Originator",
    "sender": "Sender",
    "issued_date": "Issued date",
    "received_date": "Received date",
    "az_court": "Aktenzeichen",
    "internal_id": "Reference",
    "posture": "Brief: posture",
    "next_move": "Brief: next move",
    "detected_status": "Brief: case status",
}


def _shorten(text: str | None, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def target_label(db: Session, finding: AiReviewFinding) -> str:
    """What a finding is about, in a few words."""
    kind, field = finding.target_kind, finding.field
    if kind in (ReviewTargetKind.DOCUMENT_FIELD, ReviewTargetKind.CASE_BRIEF):
        return _FIELD_LABELS.get(field or "", field or kind.value)
    if kind == ReviewTargetKind.SUMMARY:
        return "Summary"
    if kind == ReviewTargetKind.CASE_ASSIGNMENT:
        return "Case assignment"
    if kind == ReviewTargetKind.OCR_TEXT:
        return "OCR text"
    tid = finding.target_id
    row: Any = None
    if kind == ReviewTargetKind.RELATIONSHIP:
        row = db.get(DocumentRelationship, tid)
        if row is not None:
            other = db.get(Document, row.to_document_id)
            return f"Relationship: {row.relationship_type.value.replace('_', ' ')} → {_shorten(other.title if other else '?', 60)}"
        return "Relationship (removed)"
    if kind == ReviewTargetKind.CLAIM:
        row = db.get(Claim, tid)
        what = "status" if field == "status" else "text"
        return f"Claim {what}: {_shorten(row.claim_text) if row else '(removed)'}"
    if kind == ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL:
        return "Proposed claim link"
    if kind == ReviewTargetKind.CLAIM_MERGE_PROPOSAL:
        return "Proposed claim merge"
    if kind == ReviewTargetKind.ACTION_ITEM:
        row = db.get(ActionItem, tid)
        return f"Action item {field}: {_shorten(row.title, 60) if row else '(removed)'}"
    if kind == ReviewTargetKind.COST_SIGNAL:
        return "Cost ruling: client role"
    if kind == ReviewTargetKind.ENTITY:
        row = db.get(Entity, tid)
        return f"Entity: {row.name if row else '(removed)'}"
    return kind.value


def views(db: Session, findings: list[AiReviewFinding]) -> list[ReviewFindingView]:
    """``ReviewFindingView`` rows for ``findings`` (token names in one query)."""
    names = api_token_service.token_names(db, (f.agent_token_id for f in findings))
    titles = {
        d.id: d.title
        for d in db.query(Document.id, Document.title).filter(
            Document.id.in_({f.document_id for f in findings if f.document_id})
        )
    }
    return [
        ReviewFindingView(
            id=f.id,
            reviewer_kind=f.reviewer_kind,
            agent_token_name=names.get(f.agent_token_id),
            target_kind=f.target_kind,
            target_label=target_label(db, f),
            document_id=f.document_id,
            document_title=titles.get(f.document_id) if f.document_id else None,
            case_id=f.case_id,
            target_id=f.target_id,
            field=f.field,
            verdict=f.verdict,
            ai_value=f.ai_value,
            proposed_value=f.proposed_value,
            rationale=f.rationale,
            cited_excerpt=f.cited_excerpt,
            reviewed_model=f.reviewed_model,
            reviewed_prompt_version=f.reviewed_prompt_version,
            status=f.status,
            created_at=f.created_at,
            resolved_at=f.resolved_at,
        )
        for f in findings
    ]
