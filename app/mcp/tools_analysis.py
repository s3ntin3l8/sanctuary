"""Read tools: timeline, Truth Map, correspondence graph, money, deadlines, triage."""

from __future__ import annotations

import dataclasses
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.mcp.db import run
from app.mcp.principal import NOT_FOUND, McpPrincipal
from app.mcp.serializers import DocumentRef, document_ref, iso_date
from app.models.database import ActionItem, Document, Proceeding
from app.models.enums import ActionItemStatus
from app.services import api_token_service, case_overview_service, triage_bundles
from app.services.case_dashboard_service import reaction_map_for_proceeding
from app.services.case_graph_service import CaseGraphService
from app.services.case_timeline_service import CaseTimelineService
from app.services.claim_service import ClaimService

# --- Timeline ----------------------------------------------------------------


class TimelineEvent(BaseModel):
    date: str | None
    kind: str
    actor: str
    title: str
    significance: str | None
    note: str | None
    amount_eur: float | None
    direction: str | None
    status: str | None
    is_overdue: bool
    is_future: bool
    document: DocumentRef | None


def _refs(db: Session, p: McpPrincipal, ids: set[int]) -> dict[int, DocumentRef]:
    """DocumentRefs for the readable documents among ``ids``."""
    if not ids:
        return {}
    docs = db.query(Document).filter(Document.id.in_(ids)).all()
    return {
        d.id: document_ref(d)
        for d in docs
        if p.can_read_object(db, owner_id=d.owner_id, case_id=d.case_id)
    }


def get_timeline(db: Session, p: McpPrincipal, case_id: str) -> list[TimelineEvent]:
    case = p.require_case(db, case_id)
    events = CaseTimelineService(db).build_payload(case.id)["events"]
    refs = _refs(db, p, {e.source_document_id for e in events if e.source_document_id})
    return [
        TimelineEvent(
            date=iso_date(e.date),
            kind=e.kind,
            actor=e.actor,
            title=e.title,
            significance=e.sig,
            note=e.note,
            amount_eur=e.amount_eur,
            direction=e.direction,
            status=e.status,
            is_overdue=e.is_overdue,
            is_future=e.is_future,
            document=refs.get(e.source_document_id) if e.source_document_id else None,
        )
        for e in events
    ]


# --- Truth Map ---------------------------------------------------------------


class EvidenceItem(BaseModel):
    role: str
    excerpt: str | None
    provenance: str
    reactions: list[str]
    document: DocumentRef
    agent_token_name: str | None = None


class ClaimItem(BaseModel):
    claim_id: int
    text: str
    claim_type: str
    status: str
    is_precedent: bool
    evidence: list[EvidenceItem]
    agent_token_name: str | None = None


class TruthMap(BaseModel):
    case_id: str
    filter: str
    open_claim_count: int
    claims: list[ClaimItem]
    pending_merge_proposals: int
    pending_evidence_proposals: int


TruthMapStatus = Literal["open", "established", "refuted", "all"]


def get_truth_map(
    db: Session, p: McpPrincipal, case_id: str, status: TruthMapStatus = "open"
) -> TruthMap:
    case = p.require_case(db, case_id)
    view = ClaimService(db).get_truth_map(case.id, status, viewer=p.user)
    names = api_token_service.token_names(
        db,
        [
            *(r.claim.agent_token_id for g in view.groups for r in g.claims),
            *(
                e.evidence.agent_token_id
                for g in view.groups
                for r in g.claims
                for e in r.evidence
            ),
        ],
    )
    claims = []
    for group in view.groups:
        for row in group.claims:
            evidence = [
                EvidenceItem(
                    role=e.evidence.role.value,
                    excerpt=e.evidence.excerpt,
                    provenance=e.evidence.confidence.value,
                    reactions=[r.reaction.value for r in e.reactions],
                    document=document_ref(e.document),
                    agent_token_name=names.get(e.evidence.agent_token_id),
                )
                for e in row.evidence
                # Evidence may live in a case this token cannot read.
                if p.can_read_object(
                    db, owner_id=e.document.owner_id, case_id=e.document.case_id
                )
            ]
            c = row.claim
            claims.append(
                ClaimItem(
                    claim_id=c.id,
                    text=c.claim_text,
                    claim_type=c.claim_type.value,
                    status=c.status.value,
                    is_precedent=bool(c.is_precedent),
                    evidence=evidence,
                    agent_token_name=names.get(c.agent_token_id),
                )
            )
    return TruthMap(
        case_id=case.id,
        filter=status,
        open_claim_count=view.open_claim_count,
        claims=claims,
        pending_merge_proposals=len(view.pending_merges),
        pending_evidence_proposals=len(view.pending_evidence),
    )


# --- Correspondence graph ----------------------------------------------------


class GraphNodeItem(BaseModel):
    node_id: int
    lane: str
    significance: str | None
    thread_open: bool
    is_bundle: bool
    reaction: str | None
    document: DocumentRef


class GraphEdgeItem(BaseModel):
    from_node_id: int
    to_node_id: int
    kind: str


class CaseGraph(BaseModel):
    proceeding_id: int
    filter: str
    nodes: list[GraphNodeItem]
    edges: list[GraphEdgeItem]
    # document_id -> number of documents attached to it as proof
    proof_attachments: dict[str, int]
    node_counts: dict[str, int]


GraphFilter = Literal["critical", "significant+", "all"]


def get_case_graph(
    db: Session,
    p: McpPrincipal,
    proceeding_id: int,
    filter: GraphFilter = "significant+",
) -> CaseGraph:
    proc = db.get(Proceeding, proceeding_id)
    if proc is None:
        raise ToolError(NOT_FOUND)
    p.require_case(db, proc.case_id)
    payload = CaseGraphService(db).build_payload(
        proceeding_id,
        filter,
        reaction_map=reaction_map_for_proceeding(db, proceeding_id),
    )
    data = dataclasses.asdict(payload)
    refs = _refs(db, p, {n["id"] for n in data["nodes"]})
    nodes = [
        GraphNodeItem(
            node_id=n["id"],
            lane=n["lane"],
            significance=n.get("tier"),
            thread_open=bool(n.get("thread_open")),
            is_bundle=bool(n.get("is_bundle")),
            reaction=n.get("reaction"),
            document=refs[n["id"]],
        )
        for n in data["nodes"]
        if n["id"] in refs
    ]
    kept = {n.node_id for n in nodes}
    edges = [
        GraphEdgeItem(
            from_node_id=e["from_node_id"], to_node_id=e["to_node_id"], kind=e["kind"]
        )
        for e in data["edges"]
        if e["from_node_id"] in kept and e["to_node_id"] in kept
    ]
    return CaseGraph(
        proceeding_id=proceeding_id,
        filter=filter,
        nodes=nodes,
        edges=edges,
        proof_attachments={
            str(k): v for k, v in (data.get("proof_badges") or {}).items() if k in kept
        },
        node_counts={str(k): v for k, v in (data.get("node_counts") or {}).items()},
    )


# --- Financials --------------------------------------------------------------


def get_financials(db: Session, p: McpPrincipal, case_id: str) -> dict[str, Any]:
    case = p.require_case(db, case_id)
    return case_overview_service.build_financials(db, case).model_dump(mode="json")


# --- Action items ------------------------------------------------------------


class ActionItemEntry(BaseModel):
    action_item_id: int
    title: str
    description: str | None
    due_date: str | None
    action_type: str
    status: str
    location: str | None
    is_overdue: bool
    proceeding_id: int | None
    source_document: DocumentRef | None
    agent_token_name: str | None = None


def list_action_items(
    db: Session, p: McpPrincipal, case_id: str, include_done: bool = False
) -> list[ActionItemEntry]:
    from app.core.timezone import now_utc

    case = p.require_case(db, case_id)
    q = db.query(ActionItem).filter(
        ActionItem.case_id == case.id, ActionItem.superseded.is_(False)
    )
    if not include_done:
        q = q.filter(ActionItem.status == ActionItemStatus.OPEN)
    items = q.order_by(ActionItem.due_date.asc().nullslast(), ActionItem.id.asc()).all()
    refs = _refs(db, p, {i.source_document_id for i in items if i.source_document_id})
    today = now_utc()
    names = api_token_service.token_names(db, (i.agent_token_id for i in items))
    out = []
    for i in items:
        view = case_overview_service.action_item_view(i, today, names)
        out.append(
            ActionItemEntry(
                action_item_id=i.id,
                title=i.title,
                description=i.description,
                due_date=iso_date(i.due_date),
                action_type=i.action_type.value,
                status=i.status.value,
                location=i.location,
                is_overdue=view.is_overdue,
                proceeding_id=i.proceeding_id,
                source_document=refs.get(i.source_document_id)
                if i.source_document_id
                else None,
                agent_token_name=view.agent_token_name,
            )
        )
    return out


# --- Triage inbox ------------------------------------------------------------


class TriageDocument(BaseModel):
    pipeline_state: str
    needs_review: bool
    # Waiting for the user to split it or keep it whole (agents cannot decide this).
    held_for_split: bool
    significance: str | None
    document: DocumentRef


class TriageBundleItem(BaseModel):
    subject: str | None
    title_hint: str | None
    sender_email: str | None
    received_at: str | None
    status: str
    suggested_case_id: str | None
    confirmed_case_id: str | None
    case_title: str | None
    document_count: int
    documents: list[TriageDocument]


def list_triage_inbox(
    db: Session, p: McpPrincipal, limit: int = 30
) -> list[TriageBundleItem]:
    if not p.all_cases:
        raise ToolError(
            "The triage inbox needs an all-cases token; this token is limited to specific cases."
        )
    bundles = triage_bundles.get_triage_bundles(
        db, limit=max(1, min(limit, 100)), owner_id=p.user.id
    )
    return [
        TriageBundleItem(
            subject=b.subject,
            title_hint=(b.documents[0].title if b.documents else None),
            sender_email=b.sender_email,
            received_at=iso_date(b.received_at),
            status=b.mock_status,
            suggested_case_id=b.suggested_case_id,
            confirmed_case_id=b.confirmed_case_id,
            # Despite the name, enrich_bundles fills this for a confirmed case too
            # (triage_bundles.py, the confirmed non-draft branch).
            case_title=b.suggested_case_title,
            document_count=b.doc_count,
            documents=[
                TriageDocument(
                    pipeline_state=d.pipeline_state.value,
                    needs_review=bool(d.needs_review),
                    held_for_split=d.held_for_split_at is not None,
                    significance=d.significance_tier.value
                    if d.significance_tier
                    else None,
                    document=document_ref(d),
                )
                for d in b.documents
            ],
        )
        for b in bundles
    ]


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="get_timeline")
    async def _get_timeline(ctx: Context, case_id: str) -> list[TimelineEvent]:
        """Chronological events of a case across all proceedings: documents (with
        their kind and significance), deadlines and court dates, payments and
        proceeding milestones. Each event that comes from a document carries a
        document reference to cite."""
        return await run(ctx, lambda db, p: get_timeline(db, p, case_id))

    @mcp.tool(name="get_truth_map")
    async def _get_truth_map(
        ctx: Context, case_id: str, status: TruthMapStatus = "open"
    ) -> TruthMap:
        """The Truth Map of a case: atomic factual claims with their status
        (asserted, contested, needs_proof, established, refuted) and the evidence
        chain behind each (document, role, quoted excerpt, whether the link was
        AI-detected or user-confirmed). `status` filters: open (default),
        established, refuted or all."""
        return await run(ctx, lambda db, p: get_truth_map(db, p, case_id, status))

    @mcp.tool(name="get_case_graph")
    async def _get_case_graph(
        ctx: Context, proceeding_id: int, filter: GraphFilter = "significant+"
    ) -> CaseGraph:
        """The correspondence graph of one proceeding: documents as nodes (with
        swim-lane = who sent it, significance, open-thread flag) and typed edges
        between them (kind: reply, reference or supersede). Documents attached as
        proof are counted in proof_attachments rather than drawn as edges.
        `filter` limits nodes by significance: critical, significant+ (default)
        or all; node_counts says how many nodes each tier has. Get proceeding ids
        from get_case."""
        return await run(
            ctx, lambda db, p: get_case_graph(db, p, proceeding_id, filter)
        )

    @mcp.tool(name="get_financials")
    async def _get_financials(ctx: Context, case_id: str) -> dict[str, Any]:
        """Money in a case, as recorded: a summary (booked, paid, outstanding,
        reimbursable, total cost exposure in cents), per-proceeding cost
        exposure (own lawyer, court fees, opposing costs, with the source of each
        figure), case-level costs and the documents that signalled costs. No
        probabilities; amounts are facts or labelled theoretical figures."""
        return await run(ctx, lambda db, p: get_financials(db, p, case_id))

    @mcp.tool(name="list_action_items")
    async def _list_action_items(
        ctx: Context, case_id: str, include_done: bool = False
    ) -> list[ActionItemEntry]:
        """Deadlines, court dates and other action items of a case, soonest first,
        each with the document it was extracted from. Open items only unless
        `include_done` is true."""
        return await run(
            ctx, lambda db, p: list_action_items(db, p, case_id, include_done)
        )

    @mcp.tool(name="list_triage_inbox")
    async def _list_triage_inbox(
        ctx: Context, limit: int = 30
    ) -> list[TriageBundleItem]:
        """Documents that arrived (by email or scan) and still need review, grouped
        by email. A bundle carries the case it was filed under (confirmed_case_id)
        or the one the pipeline suggests (suggested_case_id); case_title is the
        title of whichever applies. Only your own inbox. Requires an all-cases
        token."""
        return await run(ctx, lambda db, p: list_triage_inbox(db, p, limit))
