"""Second-opinion tools: record whether you agree with Sanctuary's AI output.

``review_output`` writes an ``AiReviewFinding`` (needs a read-write token and
edit rights on the target); ``list_review_findings`` reads them. A
disagreement is only a proposal: the user confirms or dismisses it in the app,
there is no confirm or dismiss over MCP.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.mcp.db import run
from app.mcp.principal import NOT_FOUND, McpPrincipal
from app.mcp.serializers import untrusted
from app.models.database import AiReviewFinding
from app.models.enums import (
    AuditEventType,
    ReviewerKind,
    ReviewFindingStatus,
    ReviewTargetKind,
    ReviewVerdict,
)
from app.services import ai_review_service, api_token_service, audit_service

TargetKind = Literal[
    "document_field",
    "summary",
    "case_assignment",
    "relationship",
    "claim",
    "claim_evidence_proposal",
    "claim_merge_proposal",
    "action_item",
    "cost_signal",
    "entity",
    "ocr_text",
    "case_brief",
]


class FindingOut(BaseModel):
    finding_id: int
    target_kind: str
    target: str
    document_id: int | None
    case_id: str
    target_id: int | None
    field: str | None
    verdict: str
    status: str
    # The AI output as recorded (fenced: it can quote the document).
    ai_value: str | None
    proposed_value: Any
    rationale: str
    cited_excerpt: str | None
    reviewer: str
    reviewed_model: str | None
    reviewed_prompt_version: str | None
    created_at: str


def _fenced_json(value: Any) -> str | None:
    if value is None:
        return None
    return untrusted(json.dumps(value, ensure_ascii=False))


def _out(db: Session, findings: list[AiReviewFinding]) -> list[FindingOut]:
    names = api_token_service.token_names(db, (f.agent_token_id for f in findings))
    return [
        FindingOut(
            finding_id=f.id,
            target_kind=f.target_kind.value,
            target=ai_review_service.target_label(db, f),
            document_id=f.document_id,
            case_id=f.case_id,
            target_id=f.target_id,
            field=f.field,
            verdict=f.verdict.value,
            status=f.status.value,
            ai_value=_fenced_json(f.ai_value),
            proposed_value=f.proposed_value,
            rationale=f.rationale,
            cited_excerpt=untrusted(f.cited_excerpt) if f.cited_excerpt else None,
            reviewer=(
                "verifier"
                if f.reviewer_kind == ReviewerKind.VERIFIER
                else f"agent {names.get(f.agent_token_id) or '(deleted token)'}"
            ),
            reviewed_model=f.reviewed_model,
            reviewed_prompt_version=f.reviewed_prompt_version,
            created_at=f.created_at.isoformat(),
        )
        for f in findings
    ]


def _require_target(
    db: Session, p: McpPrincipal, target: ai_review_service.Target
) -> None:
    """Edit rights on everything confirming the finding would change."""
    kind, row = target.kind, target.row
    if kind == ReviewTargetKind.RELATIONSHIP:
        p.require_document(db, row.from_document_id, write=True)
        p.require_document(db, row.to_document_id, write=True)
    elif kind == ReviewTargetKind.CLAIM:
        p.require_claim(db, row.id, write=True)
    elif kind == ReviewTargetKind.CLAIM_EVIDENCE_PROPOSAL:
        p.require_document(db, row.source_document_id, write=True)
        p.require_claim(db, row.target_claim_id, write=True)
    elif kind == ReviewTargetKind.CLAIM_MERGE_PROPOSAL:
        p.require_claim(db, row.new_claim_id, write=True)
        p.require_claim(db, row.existing_claim_id, write=True)
    elif kind == ReviewTargetKind.ACTION_ITEM:
        p.require_action_item(db, row.id, write=True)
    elif target.document is not None:
        p.require_document(db, target.document.id, write=True)
    else:
        p.require_case(db, target.case_id, write=True)


def review_output(
    db: Session,
    p: McpPrincipal,
    target_kind: ReviewTargetKind,
    verdict: ReviewVerdict,
    rationale: str,
    document_id: int | None = None,
    case_id: str | None = None,
    target_id: int | None = None,
    field: str | None = None,
    proposed_value: Any = None,
    cited_excerpt: str | None = None,
) -> FindingOut:
    if not p.can_write:
        raise ToolError("This token is read-only.")
    try:
        target = ai_review_service.resolve_target(
            db,
            target_kind,
            document_id=document_id,
            case_id=case_id,
            target_id=target_id,
            field=field,
        )
    except ai_review_service.ReviewFindingError as exc:
        raise ToolError(NOT_FOUND if exc.status == 404 else exc.detail) from None
    _require_target(db, p, target)

    def can_edit_case(other: str) -> bool:
        try:
            p.require_case(db, other, write=True)
        except ToolError:
            return False
        return True

    try:
        finding = ai_review_service.record_finding(
            db,
            target,
            reviewer_kind=ReviewerKind.AGENT,
            agent_token_id=p.token_id,
            verdict=verdict,
            rationale=rationale,
            proposed_value=proposed_value,
            cited_excerpt=cited_excerpt,
            can_edit_case=can_edit_case,
        )
    except ai_review_service.ReviewFindingError as exc:
        raise ToolError(exc.detail) from None
    audit_service.record(
        db,
        AuditEventType.MCP_WRITE,
        target_type="ai_review_finding",
        target_id=str(finding.id),
        payload={
            "tool": "review_output",
            "api_token_id": p.token_id,
            "target_kind": target_kind.value,
            "verdict": verdict.value,
            "case_id": target.case_id,
        },
        actor_user_id=p.user.id,
        actor_label=p.audit_label,
    )
    return _out(db, [finding])[0]


def list_review_findings(
    db: Session,
    p: McpPrincipal,
    case_id: str,
    status: ReviewFindingStatus | None = None,
) -> list[FindingOut]:
    case = p.require_case(db, case_id)
    return _out(db, ai_review_service.for_case(db, case.id, status))


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="review_output")
    async def _review_output(
        ctx: Context,
        target_kind: TargetKind,
        verdict: Literal["agree", "disagree", "uncertain"],
        rationale: str,
        document_id: int | None = None,
        case_id: str | None = None,
        target_id: int | None = None,
        field: str | None = None,
        proposed_value: Any = None,
        cited_excerpt: str | None = None,
    ) -> FindingOut:
        """Record a second opinion on one piece of Sanctuary's AI output. Call it
        once per item you checked, `agree` included. `disagree` is a proposal
        the user confirms or dismisses in the app; confirming applies
        `proposed_value`. Quote the passage you relied on in `cited_excerpt`;
        no probabilities. Calling again for the same open item updates it.

        Targets (identify with document_id, target_id or case_id; `field` where
        listed; `proposed_value` only with `disagree`):
        - document_field (document_id, field: title, significance_tier,
          document_type, originator_type, sender, issued_date, received_date,
          az_court, internal_id): the corrected value (enum value or ISO date)
        - summary (document_id): {legal_significance, required_action,
          financial_impact}
        - case_assignment (document_id): {case_id, proceeding_id?}
        - relationship (target_id = relationship id): no value; confirming
          rejects the link
        - claim (target_id, field: status | text): a status, or for text an
          optional corrected wording; confirming dismisses the claim
        - claim_evidence_proposal / claim_merge_proposal (target_id): no
          value; confirming dismisses the proposal
        - action_item (target_id, field: due_date | title | status): the
          corrected value; null for due_date/title = not a real deadline
        - cost_signal (target_id, field: client_role): winner | loser | unset
        - entity (target_id): optional {type, name}; recorded only
        - ocr_text (document_id): {pages: [{page, text}]}; confirming
          re-reads the document
        - case_brief (case_id, field: posture | next_move | detected_status):
          the corrected text or case status
        Needs a read-write token."""
        return await run(
            ctx,
            lambda db, p: review_output(
                db,
                p,
                ReviewTargetKind(target_kind),
                ReviewVerdict(verdict),
                rationale,
                document_id=document_id,
                case_id=case_id,
                target_id=target_id,
                field=field,
                proposed_value=proposed_value,
                cited_excerpt=cited_excerpt,
            ),
        )

    @mcp.tool(name="list_review_findings")
    async def _list_review_findings(
        ctx: Context,
        case_id: str,
        status: Literal["pending", "confirmed", "dismissed", "stale", "noted"]
        | None = None,
    ) -> list[FindingOut]:
        """Second opinions recorded on a case's AI output (yours, other agents'
        and the verifier's), newest first; filter by `status` (`pending` = the
        user has not decided, `stale` = the AI output changed since, `noted` =
        an agreement)."""
        return await run(
            ctx,
            lambda db, p: list_review_findings(
                db,
                p,
                case_id,
                ReviewFindingStatus(status) if status else None,
            ),
        )
