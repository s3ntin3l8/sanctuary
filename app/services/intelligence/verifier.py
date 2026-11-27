"""Opt-in verifier: a second model checks the local AI's output.

The admin picks an endpoint and model for the verifier (Settings → AI →
Verifier), the groups of output it checks and the cases it may see. Every
call here first goes through ``ai_config.verifier_for_case``: when the switch
is off, nothing is chosen or the case is out of scope, no request is made.

The call path is separate from ``call_json_ai`` on purpose: the verifier has
its own ``AIProvider`` (the ``chat_provider`` singleton is never touched), its
own options (not the Qwen-tuned stage options), and it never takes
``model_gate`` (it never waits behind local OCR). Each call is written to the
runs index with ``external`` set when the endpoint is public, and its prompt is
dumped only while ``ai.ai_debug_capture`` is on (encrypted, like every dump).

The model's answer is a list of findings on the numbered items it was given.
Only items that were actually offered are recorded (a made-up id is dropped),
each through ``ai_review_service.record_finding`` as ``reviewer_kind=verifier``;
a finding the service refuses (a bad proposed value) is skipped, not fatal.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AI_READ_TIMEOUT, DATA_DIR
from app.core.async_utils import run_async
from app.core.safe_error import describe_exception, sanitize_error_text
from app.models.database import (
    ActionItem,
    Case,
    Claim,
    ClaimEvidence,
    CostSignal,
    Document,
    DocumentRelationship,
)
from app.models.enums import (
    ActionItemStatus,
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    DocumentType,
    OriginatorType,
    RelationshipConfidence,
    ReviewerKind,
    ReviewTargetKind,
    ReviewVerdict,
    SignificanceTier,
)
from app.services import ai_review_service
from app.services.ai_config import (
    DOCUMENT_VERIFIER_GROUPS,
    VerifierConfig,
    verifier_for_case,
)
from app.services.ai_provider import AIProvider, ProviderType, is_external_endpoint
from app.services.ai_run_index import record_run
from app.services.ai_summary import content_windows
from app.services.intelligence import prompts
from app.services.intelligence._ai_call import _scope_file, _write_block
from app.services.intelligence._json import parse_json_response
from app.services.timezone_service import get_user_tz, local_date_str

logger = logging.getLogger(__name__)

# The verifier's own options: a hosted model needs no sampler tuning, only an
# output cap. Anthropic ignores temperature (Claude 5 rejects it, so it is
# never sent there); OpenAI-compatible endpoints get a deterministic one.
VERIFIER_OPTIONS: dict[str, Any] = {
    "max_tokens": 8192,
    "temperature": 0.0,
    "_include_user_context": False,
}
# Document text sent per call: the first windows (cut at page boundaries) up
# to this many characters. The rest of a very long document is not sent.
VERIFIER_MAX_CHARS = 120_000
# Case documents listed for the brief check (newest last), and the characters
# of each one's legal summary.
VERIFIER_BRIEF_MAX_DOCS = 120
_BRIEF_SUMMARY_CHARS = 400

_GROUP_PROMPTS = {
    "document_fields": prompts.VERIFIER_DOCUMENT_FIELDS,
    "summary": prompts.VERIFIER_SUMMARY,
    "relationships": prompts.VERIFIER_RELATIONSHIPS,
    "claims": prompts.VERIFIER_CLAIMS,
    "action_items": prompts.VERIFIER_ACTION_ITEMS,
    "brief": prompts.VERIFIER_BRIEF,
}

_DOCUMENT_FIELD_ENUMS: dict[str, list[str]] = {
    "significance_tier": [e.value for e in SignificanceTier],
    "document_type": [e.value for e in DocumentType],
    "originator_type": [e.value for e in OriginatorType],
}
# Kinds whose disagreement takes no replacement (confirming rejects/dismisses).
_NO_VALUE_KINDS = (ReviewTargetKind.RELATIONSHIP,)


class VerifierItem(BaseModel):
    target_kind: Literal[
        "document_field",
        "summary",
        "relationship",
        "claim",
        "action_item",
        "cost_signal",
        "case_brief",
    ]
    target_id: int | None = None
    field: str | None = None
    verdict: Literal["agree", "disagree", "uncertain"]
    proposed_value: str | None = None
    rationale: str
    cited_excerpt: str | None = None


class VerifierFindings(BaseModel):
    findings: list[VerifierItem] = []


Key = tuple[str, int | None, str | None]


@dataclass
class _Offer:
    """What one call shows the verifier: the items (keyed as the model must
    echo them back) and where each one resolves to."""

    items: list[dict[str, Any]] = field(default_factory=list)
    keys: set[Key] = field(default_factory=set)

    def add(
        self,
        kind: ReviewTargetKind,
        target_id: int | None,
        fld: str | None,
        value: Any,
        **extra: Any,
    ) -> None:
        self.keys.add((kind.value, target_id, fld))
        self.items.append(
            {
                "target_kind": kind.value,
                "target_id": target_id,
                "field": fld,
                "value": value,
                **extra,
            }
        )


@dataclass
class VerifyResult:
    """What a run did: ``called`` is False when the gate refused (no request)."""

    called: bool
    recorded: int = 0
    dropped: int = 0


# --- Collecting the local model's output --------------------------------------


def _not_ai(doc: Document, fld: str) -> bool:
    """A field whose value a mark says was not the AI's: set by the user, read
    from the mail headers or a court manifest, or inherited from its family.

    Only those setter marks count: a high/medium/low grade is the reader's own
    confidence (the AI's, or a rule-based extractor's) and is checked.
    """
    from app.services.document_review_service import FIELD_SOURCE_MARKS

    return (doc.extraction_confidence or {}).get(fld) in FIELD_SOURCE_MARKS


def _offer_document_fields(db: Session, doc: Document, offer: _Offer) -> None:
    for fld in ai_review_service.DOCUMENT_FIELDS:
        if _not_ai(doc, fld):
            continue
        value = ai_review_service.current_value(
            db,
            ai_review_service.resolve_target(
                db, ReviewTargetKind.DOCUMENT_FIELD, document_id=doc.id, field=fld
            ),
        )
        extra = (
            {"allowed": _DOCUMENT_FIELD_ENUMS[fld]}
            if fld in _DOCUMENT_FIELD_ENUMS
            else {}
        )
        offer.add(ReviewTargetKind.DOCUMENT_FIELD, None, fld, value, **extra)


def _offer_summary(doc: Document, offer: _Offer) -> None:
    if doc.ai_summary_approved_at is not None:
        return  # approved by the user
    summary = ai_review_service.summary_value(doc.ai_summary)
    if not summary:
        return
    for key in ai_review_service.SUMMARY_KEYS:
        offer.add(ReviewTargetKind.SUMMARY, None, key, summary.get(key))


def _offer_relationships(db: Session, doc: Document, offer: _Offer) -> None:
    rows = (
        db.query(DocumentRelationship, Document)
        .join(Document, Document.id == DocumentRelationship.to_document_id)
        .filter(
            DocumentRelationship.from_document_id == doc.id,
            DocumentRelationship.confidence == RelationshipConfidence.AI_DETECTED,
            # Only the case in scope: an edge into another case would send that
            # document's title and sender out with it.
            Document.case_id == doc.case_id,
        )
        .order_by(DocumentRelationship.id)
        .all()
    )
    for rel, other in rows:
        offer.add(
            ReviewTargetKind.RELATIONSHIP,
            rel.id,
            None,
            {
                "type": rel.relationship_type.value,
                "to_document": prompts.sanitize_oneline(other.title),
                "to_document_date": (
                    local_date_str(other.issued_date) if other.issued_date else None
                ),
                "to_document_sender": prompts.sanitize_oneline(other.sender),
            },
        )


def _offer_claims(db: Session, doc: Document, offer: _Offer) -> None:
    claims = (
        db.query(Claim)
        .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
        .filter(
            ClaimEvidence.document_id == doc.id,
            ClaimEvidence.role == ClaimEvidenceRole.ASSERTS,
            Claim.dismissed_at.is_(None),
            Claim.agent_token_id.is_(None),
        )
        .order_by(Claim.id)
        .all()
    )
    statuses = [e.value for e in ClaimStatus]
    for claim in claims:
        offer.add(ReviewTargetKind.CLAIM, claim.id, "text", claim.claim_text)
        offer.add(
            ReviewTargetKind.CLAIM,
            claim.id,
            "status",
            claim.status.value,
            allowed=statuses,
        )


def _offer_action_items(db: Session, doc: Document, offer: _Offer) -> None:
    items = (
        db.query(ActionItem)
        .filter(
            ActionItem.source_document_id == doc.id,
            ActionItem.agent_token_id.is_(None),
            ActionItem.superseded.is_(False),
        )
        .order_by(ActionItem.id)
        .all()
    )
    statuses = [e.value for e in ActionItemStatus]
    for item in items:
        offer.add(
            ReviewTargetKind.ACTION_ITEM,
            item.id,
            "due_date",
            local_date_str(item.due_date),
        )
        offer.add(ReviewTargetKind.ACTION_ITEM, item.id, "title", item.title)
        offer.add(
            ReviewTargetKind.ACTION_ITEM,
            item.id,
            "status",
            item.status.value,
            allowed=statuses,
        )
    for signal in (
        db.query(CostSignal)
        .filter(CostSignal.source_document_id == doc.id)
        .order_by(CostSignal.id)
        .all()
    ):
        offer.add(
            ReviewTargetKind.COST_SIGNAL,
            signal.id,
            "client_role",
            (signal.allocation or {}).get("client_role"),
            allowed=list(ai_review_service.CLIENT_ROLES),
        )


_DOCUMENT_COLLECTORS = {
    "document_fields": lambda db, doc, offer: _offer_document_fields(db, doc, offer),
    "summary": lambda db, doc, offer: _offer_summary(doc, offer),
    "relationships": _offer_relationships,
    "claims": _offer_claims,
    "action_items": _offer_action_items,
}


def _document_text(doc: Document) -> str:
    """The document's first windows, up to ``VERIFIER_MAX_CHARS``."""
    windows = content_windows(doc)
    parts: list[str] = []
    size = 0
    for w in windows:
        if parts and size + len(w.text) > VERIFIER_MAX_CHARS:
            break
        parts.append(w.text[:VERIFIER_MAX_CHARS])
        size += len(parts[-1])
    text = "".join(parts)
    if len(parts) < len(windows):
        text += "\n\n[The rest of the document is not included.]"
    return text


# --- The call -------------------------------------------------------------------


def _system_prompt(groups: tuple[str, ...]) -> str:
    return "\n\n".join(
        [prompts.VERIFIER_BASE_SYSTEM, *(_GROUP_PROMPTS[g] for g in groups)]
    )


def _stream(
    provider: AIProvider, ptype: ProviderType, params: dict
) -> tuple[str, dict]:
    """POST the request and collect the streamed answer and token usage."""
    response_text = ""
    usage: dict = {}
    with httpx.Client(
        timeout=httpx.Timeout(connect=10.0, read=AI_READ_TIMEOUT, write=30.0, pool=10.0)
    ) as client:
        with client.stream(
            "POST", params["url"], json=params["json"], headers=params["headers"]
        ) as response:
            if not response.is_success:
                response.read()
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            for line in response.iter_lines():
                chunk = provider.parse_stream_line(line, ptype)
                if not chunk:
                    continue
                if chunk.get("usage"):
                    usage = {**usage, **chunk["usage"]}
                if chunk.get("done"):
                    break
                response_text += chunk.get("response") or ""
    return response_text, usage


def _call(
    db: Session,
    cfg: VerifierConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    kind: str,
    scope_id: str,
    doc_id: int | None,
    batch_id: int | None,
    case_id: str,
) -> VerifierFindings | None:
    """One verifier request; None when it failed or its answer is unusable."""
    from app.services.user_settings_service import get_ai_debug_capture

    capture = get_ai_debug_capture(db)
    provider = AIProvider.for_endpoint(
        "verifier", base_url=cfg.base_url, provider=cfg.provider, api_key=cfg.api_key
    )
    label = f"{kind}_{scope_id}_verifier"
    started_at = datetime.now(get_user_tz()).isoformat(timespec="seconds")
    start = time.perf_counter()
    ptype_label = cfg.provider
    params: dict = {"json": {}}
    text = ""
    usage: dict = {}
    error: str | None = None
    try:
        ptype = run_async(provider.get_type())
        ptype_label = str(ptype)
        params = run_async(
            provider.get_generate_params(
                model=cfg.model,
                prompt=user_prompt,
                system_prompt=system_prompt,
                stream=True,
                options={
                    **VERIFIER_OPTIONS,
                    "_response_schema": VerifierFindings.model_json_schema(),
                    "_schema_name": "VerifierFindings",
                },
            )
        )
        text, usage = _stream(provider, ptype, params)
    except Exception as exc:
        error = describe_exception(exc)
        logger.warning("Verifier call %s failed: %s", label, error)
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        _write_block(
            _scope_file(DATA_DIR / "ai_debug", label, batch_id),
            debug_label=label,
            stage="verifier",
            started_at=started_at,
            model=cfg.model,
            provider=ptype_label,
            kind=kind,
            scope_id=scope_id,
            ingest_batch_id=batch_id,
            duration_ms=duration_ms,
            ttfb_ms=None,
            payload=params.get("json") or {},
            thinking="",
            response=text,
            error=error,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            capture=capture,
        )
        record_run(
            started_at=started_at,
            kind=kind,
            scope_id=scope_id,
            stage="verifier",
            doc_id=doc_id,
            batch_id=batch_id,
            case_id=case_id,
            model=cfg.model,
            provider=ptype_label,
            duration_ms=duration_ms,
            response_len=len(text),
            status="error" if error else "ok",
            error=sanitize_error_text(error, 200) if error else None,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            external=is_external_endpoint(cfg.base_url),
        )
    if error is not None:
        return None
    try:
        return VerifierFindings.model_validate(parse_json_response(text))
    except (ValueError, ValidationError) as exc:
        logger.warning(
            "Verifier call %s returned an unusable answer: %s",
            label,
            describe_exception(exc),
        )
        return None


def _user_prompt(items: list[dict[str, Any]], source: str, source_kind: str) -> str:
    return (
        "Items to check (the local model's output):\n"
        + prompts.fence(json.dumps(items, ensure_ascii=False, indent=1), "ai_extracted")
        + f"\n\nSource ({source_kind}):\n"
        + prompts.fence(source, "document")
    )


# --- Recording ------------------------------------------------------------------


def _record(
    db: Session,
    target: ai_review_service.Target,
    *,
    verdict: ReviewVerdict,
    rationale: str,
    proposed: Any,
    excerpt: str | None,
) -> bool:
    try:
        with db.begin_nested():
            ai_review_service.record_finding(
                db,
                target,
                reviewer_kind=ReviewerKind.VERIFIER,
                agent_token_id=None,
                verdict=verdict,
                rationale=rationale,
                proposed_value=proposed if verdict == ReviewVerdict.DISAGREE else None,
                cited_excerpt=excerpt,
                can_edit_case=lambda _case_id: False,
            )
        return True
    except (ai_review_service.ReviewFindingError, IntegrityError) as exc:
        logger.info(
            "Verifier finding on %s dropped: %s",
            target.kind.value,
            describe_exception(exc),
        )
        return False


def _verdict(items: list[VerifierItem]) -> ReviewVerdict:
    verdicts = {i.verdict for i in items}
    for v in ("disagree", "uncertain"):
        if v in verdicts:
            return ReviewVerdict(v)
    return ReviewVerdict.AGREE


def _record_all(
    db: Session,
    offer: _Offer,
    answer: VerifierFindings,
    *,
    document_id: int | None,
    case_id: str,
) -> tuple[int, int]:
    """Record the answer's findings on offered items; return (recorded, dropped)."""
    recorded = dropped = 0
    summary_items: list[VerifierItem] = []
    for item in answer.findings:
        fld = (item.field or "").strip() or None
        if (item.target_kind, item.target_id, fld) not in offer.keys:
            dropped += 1  # not an item it was shown
            continue
        kind = ReviewTargetKind(item.target_kind)
        if kind == ReviewTargetKind.SUMMARY:
            summary_items.append(item)
            continue
        try:
            target = ai_review_service.resolve_target(
                db,
                kind,
                document_id=document_id
                if kind != ReviewTargetKind.CASE_BRIEF
                else None,
                case_id=case_id,
                target_id=item.target_id,
                field=fld,
            )
        except ai_review_service.ReviewFindingError:
            dropped += 1
            continue
        proposed: Any = None if kind in _NO_VALUE_KINDS else item.proposed_value
        ok = _record(
            db,
            target,
            verdict=ReviewVerdict(item.verdict),
            rationale=item.rationale,
            proposed=proposed,
            excerpt=item.cited_excerpt,
        )
        recorded += ok
        dropped += not ok
    if summary_items and document_id is not None:
        # One finding on the summary: the disputed bullets replaced, the rest kept.
        target = ai_review_service.resolve_target(
            db, ReviewTargetKind.SUMMARY, document_id=document_id, case_id=case_id
        )
        proposed_summary = dict(ai_review_service.current_value(db, target) or {})
        for i in summary_items:
            if i.verdict == "disagree" and i.proposed_value:
                proposed_summary[i.field or ""] = i.proposed_value
        disputed = [i for i in summary_items if i.verdict != "agree"]
        rationale = " ".join(
            f"{(i.field or '').replace('_', ' ')}: {i.rationale.strip()}"
            for i in disputed
        ) or " ".join(i.rationale.strip() for i in summary_items)
        ok = _record(
            db,
            target,
            verdict=_verdict(summary_items),
            rationale=rationale[: ai_review_service.MAX_TEXT],
            proposed=proposed_summary,
            excerpt=next(
                (i.cited_excerpt for i in summary_items if i.cited_excerpt), None
            ),
        )
        recorded += ok
        dropped += not ok
    return recorded, dropped


# --- Entry points ---------------------------------------------------------------


def verify_document(db: Session, doc_id: int) -> VerifyResult:
    """Check one document's AI output with the verifier, if it is in scope.

    Caller owns the session; this commits the findings it records.
    """
    doc = db.get(Document, doc_id)
    if doc is None:
        return VerifyResult(called=False)
    gate = verifier_for_case(db, doc.case_id, DOCUMENT_VERIFIER_GROUPS)
    if gate is None or not (doc.content or "").strip():
        return VerifyResult(called=False)
    cfg, groups = gate
    offer = _Offer()
    for group in groups:
        _DOCUMENT_COLLECTORS[group](db, doc, offer)
    if not offer.items:
        return VerifyResult(called=False)
    case_id = doc.case_id
    assert case_id is not None
    answer = _call(
        db,
        cfg,
        system_prompt=_system_prompt(groups),
        user_prompt=_user_prompt(offer.items, _document_text(doc), "the document"),
        kind="doc",
        scope_id=str(doc.id),
        doc_id=doc.id,
        batch_id=doc.ingest_batch_id,
        case_id=case_id,
    )
    if answer is None:
        return VerifyResult(called=True)
    recorded, dropped = _record_all(
        db, offer, answer, document_id=doc.id, case_id=case_id
    )
    db.commit()
    logger.info(
        "Doc #%d: verifier recorded %d finding(s), dropped %d",
        doc.id,
        recorded,
        dropped,
    )
    return VerifyResult(called=True, recorded=recorded, dropped=dropped)


def _case_documents_text(db: Session, case: Case) -> str:
    docs = (
        db.query(Document)
        .filter(Document.case_id == case.id)
        .order_by(Document.issued_date.is_(None), Document.issued_date, Document.id)
        .all()
    )[-VERIFIER_BRIEF_MAX_DOCS:]
    lines = []
    for d in docs:
        summary = ai_review_service.summary_value(d.ai_summary) or {}
        legal = (summary.get("legal_significance") or "")[:_BRIEF_SUMMARY_CHARS]
        date = local_date_str(d.issued_date) if d.issued_date else "undated"
        lines.append(
            f"- {date} · {prompts.sanitize_oneline(d.sender) or 'unknown sender'} · "
            f"{prompts.sanitize_oneline(d.title)}: {prompts.sanitize_oneline(legal, 500)}"
        )
    return "\n".join(lines)


def verify_case_brief(db: Session, case_id: str) -> VerifyResult:
    """Check the case brief with the verifier, if the case and ``brief`` are in scope."""
    gate = verifier_for_case(db, case_id, ("brief",))
    case = db.get(Case, case_id)
    if gate is None or case is None or not case.ai_brief:
        return VerifyResult(called=False)
    cfg, groups = gate
    offer = _Offer()
    statuses = [e.value for e in CaseStatus]
    for fld in ai_review_service.BRIEF_FIELDS:
        value = (case.ai_brief or {}).get(fld)
        if value is None:
            continue
        extra = {"allowed": statuses} if fld == "detected_status" else {}
        offer.add(ReviewTargetKind.CASE_BRIEF, None, fld, value, **extra)
    if not offer.items:
        return VerifyResult(called=False)
    answer = _call(
        db,
        cfg,
        system_prompt=_system_prompt(groups),
        user_prompt=_user_prompt(
            offer.items, _case_documents_text(db, case), "the case's documents"
        ),
        kind="case",
        scope_id=case.id,
        doc_id=None,
        batch_id=None,
        case_id=case.id,
    )
    if answer is None:
        return VerifyResult(called=True)
    recorded, dropped = _record_all(
        db, offer, answer, document_id=None, case_id=case.id
    )
    db.commit()
    logger.info(
        "Case %s: verifier recorded %d brief finding(s), dropped %d",
        case.id,
        recorded,
        dropped,
    )
    return VerifyResult(called=True, recorded=recorded, dropped=dropped)


def case_document_ids(db: Session, case_id: str) -> list[int]:
    """The documents a case verification checks: filed in the case, with text."""
    return [
        d_id
        for (d_id,) in db.query(Document.id)
        .filter(
            Document.case_id == case_id,
            Document.content.isnot(None),
        )
        .order_by(Document.id)
        .all()
    ]
