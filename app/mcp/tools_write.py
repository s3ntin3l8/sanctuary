"""Write tools: annotations, Truth Map, deadlines and document links.

Every tool needs a read-write token and a case the token may edit. Rows are
written as the token's owner (user-created provenance) and tagged with the
token (``agent_token_id``) so the UI can show an agent wrote them; each write
leaves an ``MCP_WRITE`` audit row.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.async_utils import run_async
from app.mcp.db import run
from app.mcp.principal import McpPrincipal
from app.models.database import (
    ActionItem,
    Claim,
    DocumentRelationship,
    Proceeding,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    AuditEventType,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    RelationshipConfidence,
    RelationshipType,
    UserReactionType,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.document_relationship import insert_edge_if_absent, is_rejected
from app.services import (
    action_item_service,
    audit_service,
    pin_service,
    reaction_service,
)
from app.services.claim_embedding import nearest_claims, upsert_claim_embedding
from app.services.claim_service import ClaimService

logger = logging.getLogger(__name__)

MAX_TEXT = 4000
_SIMILAR_CLAIMS = 3


def _audit(
    db: Session,
    p: McpPrincipal,
    tool: str,
    target_type: str,
    target_id: int | str,
    **args,
) -> None:
    audit_service.record(
        db,
        AuditEventType.MCP_WRITE,
        target_type=target_type,
        target_id=str(target_id),
        payload={"tool": tool, "api_token_id": p.token_id, **args},
        actor_user_id=p.user.id,
        actor_label=p.audit_label,
    )


def _text(value: str, name: str, *, min_len: int = 1, max_len: int = MAX_TEXT) -> str:
    value = value.strip()
    if len(value) < min_len:
        raise ToolError(f"{name} must not be empty.")
    if len(value) > max_len:
        raise ToolError(f"{name} is too long (max {max_len} characters).")
    return value


def _optional_text(value: str | None, name: str) -> str | None:
    return _text(value, name) if value and value.strip() else None


# --- Annotations --------------------------------------------------------------


class ReactionOut(BaseModel):
    reaction: str
    notes: str | None
    by_agent: bool


def set_reaction(
    db: Session,
    p: McpPrincipal,
    document_id: int,
    reaction: UserReactionType,
    notes: str | None,
) -> list[ReactionOut]:
    doc = p.require_document(db, document_id, write=True)
    rows = reaction_service.ensure_reaction(
        db,
        p.user.id,
        doc,
        reaction,
        _optional_text(notes, "notes"),
        agent_token_id=p.token_id,
    )
    _audit(db, p, "set_reaction", "document", doc.id, reaction=reaction.value)
    return [
        ReactionOut(
            reaction=r.reaction.value,
            notes=r.notes,
            by_agent=r.agent_token_id is not None,
        )
        for r in rows
    ]


class PinOut(BaseModel):
    pin_id: int
    document_id: int
    passage_id: str
    note: str | None


def add_pin(
    db: Session, p: McpPrincipal, document_id: int, passage_id: str, note: str
) -> PinOut:
    doc = p.require_document(db, document_id, write=True)
    try:
        pin = pin_service.create_pin(
            db,
            p.user.id,
            doc,
            passage_id,
            _text(note, "note"),
            agent_token_id=p.token_id,
        )
    except pin_service.UnknownPassage:
        raise ToolError(
            "passage_id is not one of this document's key passages "
            "(see get_document → key_passages)."
        ) from None
    _audit(db, p, "add_pin", "document_pin", pin.id, document_id=doc.id)
    return PinOut(
        pin_id=pin.id, document_id=doc.id, passage_id=pin.passage_id, note=pin.note
    )


# --- Truth Map ------------------------------------------------------------------


class SimilarClaim(BaseModel):
    claim_id: int
    text: str
    status: str
    distance: float


class ClaimCreated(BaseModel):
    claim_id: int
    claim_type: str
    status: str
    document_id: int
    similar_claims: list[SimilarClaim]


def create_claim(
    db: Session,
    p: McpPrincipal,
    case_id: str,
    text: str,
    claim_type: ClaimType,
    document_id: int,
    excerpt: str | None,
) -> ClaimCreated:
    case = p.require_case(db, case_id, write=True)
    doc = p.require_document(db, document_id, write=True)
    if doc.case_id != case.id:
        raise ToolError("That document is not in this case.")
    claim_text = _text(text, "text", min_len=5)

    similar: list[SimilarClaim] = []
    try:  # best effort: the embedding provider may be down
        near = run_async(
            nearest_claims(claim_text, db, k=_SIMILAR_CLAIMS, case_id=case.id)
        )
        by_id = {
            c.id: c for c in db.query(Claim).filter(Claim.id.in_([i for i, _ in near]))
        }
        similar = [
            SimilarClaim(
                claim_id=i,
                text=by_id[i].claim_text,
                status=by_id[i].status.value,
                distance=round(float(d), 4),
            )
            for i, d in near
            if i in by_id and by_id[i].dismissed_at is None
        ]
    except Exception as exc:  # noqa: BLE001
        logger.info("create_claim: similar-claim lookup skipped: %s", exc)

    claim = ClaimService(db).create_user_claim(
        doc,
        claim_text,
        claim_type,
        _optional_text(excerpt, "excerpt"),
        agent_token_id=p.token_id,
    )
    _audit(
        db, p, "create_claim", "claim", claim.id, case_id=case.id, document_id=doc.id
    )
    out = ClaimCreated(
        claim_id=claim.id,
        claim_type=claim.claim_type.value,
        status=claim.status.value,
        document_id=doc.id,
        similar_claims=similar,
    )
    try:  # commits; a failure is recorded on the claim for the maintenance sweep
        run_async(upsert_claim_embedding(claim.id, db))
    except Exception as exc:  # noqa: BLE001
        logger.info("create_claim: embedding skipped: %s", exc)
    return out


class EvidenceLinked(BaseModel):
    claim_id: int
    document_id: int
    role: str
    changed: bool
    claim_status: str


def link_claim_evidence(
    db: Session,
    p: McpPrincipal,
    claim_id: int,
    document_id: int,
    role: ClaimEvidenceRole,
    excerpt: str | None,
) -> EvidenceLinked:
    claim = p.require_claim(db, claim_id, write=True)
    doc = p.require_document(db, document_id, write=True)
    row, changed = ClaimService(db).link_user_evidence(
        claim, doc, role, _optional_text(excerpt, "excerpt"), agent_token_id=p.token_id
    )
    if changed:
        _audit(
            db, p, "link_claim_evidence", "claim", claim.id,
            document_id=doc.id, role=role.value,
        )  # fmt: skip
    return EvidenceLinked(
        claim_id=claim.id,
        document_id=doc.id,
        role=row.role.value,
        changed=changed,
        claim_status=claim.status.value,
    )


class ClaimStatusOut(BaseModel):
    claim_id: int
    status: str


def set_claim_status(
    db: Session, p: McpPrincipal, claim_id: int, status: ClaimStatus
) -> ClaimStatusOut:
    claim = p.require_claim(db, claim_id, write=True)
    previous = claim.status
    try:
        updated = ClaimService(db).transition_status(claim.id, status)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    _audit(
        db, p, "set_claim_status", "claim", claim.id,
        from_status=previous.value, to_status=status.value,
    )  # fmt: skip
    return ClaimStatusOut(claim_id=updated.id, status=updated.status.value)


# --- Deadlines ------------------------------------------------------------------


class ActionItemOut(BaseModel):
    action_item_id: int
    case_id: str
    title: str
    due_date: str
    action_type: str
    status: str


def _action_out(item: ActionItem) -> ActionItemOut:
    return ActionItemOut(
        action_item_id=item.id,
        case_id=item.case_id,
        title=item.title,
        due_date=item.due_date.date().isoformat(),
        action_type=item.action_type.value,
        status=item.status.value,
    )


def create_action_item(
    db: Session,
    p: McpPrincipal,
    case_id: str,
    title: str,
    due_date: str,
    action_type: ActionItemType,
    description: str | None,
    location: str | None,
    proceeding_id: int | None,
    source_document_id: int | None,
) -> ActionItemOut:
    case = p.require_case(db, case_id, write=True)
    try:  # date only, like AI-extracted items
        due = datetime.strptime(due_date.strip()[:10], "%Y-%m-%d")
    except ValueError:
        raise ToolError("due_date must be an ISO date (YYYY-MM-DD).") from None
    if proceeding_id is not None:
        proceeding = db.get(Proceeding, proceeding_id)
        if proceeding is None or proceeding.case_id != case.id:
            raise ToolError("That proceeding is not part of this case.")
    if source_document_id is not None:
        source = p.require_document(db, source_document_id)
        if source.case_id != case.id:
            raise ToolError("The source document is not in this case.")

    clash = (
        db.query(ActionItem)
        .filter(
            ActionItem.case_id == case.id,
            ActionItem.action_type == action_type,
            ActionItem.due_date >= due,
            ActionItem.due_date < due + timedelta(days=1),
        )
        .first()
    )
    if clash is not None:
        reason = (
            "was superseded (rescheduled) and is void"
            if clash.superseded
            else f"already exists (action item {clash.id})"
        )
        raise ToolError(
            f"A {action_type.value} on {due.date().isoformat()} {reason}; "
            "use update_action_item instead."
        )
    try:
        with db.begin_nested():
            item = ActionItemRepository(db).create_action_item(
                case.id,
                _text(title, "title", max_len=300),
                due,
                action_type,
                _optional_text(description, "description"),
                _optional_text(location, "location"),
                proceeding_id,
                source_document_id,
                agent_token_id=p.token_id,
            )
    except IntegrityError:
        raise ToolError(
            "An action item of this type already exists on that date."
        ) from None
    _audit(db, p, "create_action_item", "action_item", item.id, case_id=case.id)
    return _action_out(item)


def update_action_item(
    db: Session, p: McpPrincipal, action_item_id: int, status: ActionItemStatus
) -> ActionItemOut:
    item = p.require_action_item(db, action_item_id, write=True)
    previous = item.status
    item = action_item_service.set_status(db, item, status)
    _audit(
        db, p, "update_action_item", "action_item", item.id,
        from_status=previous.value, to_status=status.value,
    )  # fmt: skip
    return _action_out(item)


# --- Document links -------------------------------------------------------------


class RelationshipOut(BaseModel):
    relationship_id: int
    from_document_id: int
    to_document_id: int
    relationship_type: str
    created: bool


def create_relationship(
    db: Session,
    p: McpPrincipal,
    from_document_id: int,
    to_document_id: int,
    relationship_type: RelationshipType,
    notes: str | None,
) -> RelationshipOut:
    from app.services.ingestion.service import refresh_review_reasons
    from app.services.intelligence.thread_open_scanner import recompute_thread_open

    if from_document_id == to_document_id:
        raise ToolError("A document cannot be linked to itself.")
    source = p.require_document(db, from_document_id, write=True)
    target = p.require_document(db, to_document_id, write=True)
    if source.case_id != target.case_id:
        raise ToolError("Both documents must be in the same case.")
    if is_rejected(
        db,
        from_document_id=source.id,
        to_document_id=target.id,
        relationship_type=relationship_type,
    ):
        raise ToolError(
            "The user previously rejected this link; it will not be recreated."
        )
    created = insert_edge_if_absent(
        db,
        from_document_id=source.id,
        to_document_id=target.id,
        relationship_type=relationship_type,
        confidence=RelationshipConfidence.USER_CREATED,
        notes=_optional_text(notes, "notes"),
        agent_token_id=p.token_id,
    )
    db.flush()
    edge = (
        db.query(DocumentRelationship)
        .filter(
            DocumentRelationship.from_document_id == source.id,
            DocumentRelationship.to_document_id == target.id,
            DocumentRelationship.relationship_type == relationship_type,
        )
        .one()
    )
    if created:
        _audit(
            db, p, "create_relationship", "document_relationship", edge.id,
            from_document_id=source.id, to_document_id=target.id,
            relationship_type=relationship_type.value,
        )  # fmt: skip
        db.commit()  # recompute_thread_open reads committed edges
        recompute_thread_open(target.id, db)
        refresh_review_reasons(source, db, commit=False)
    return RelationshipOut(
        relationship_id=edge.id,
        from_document_id=source.id,
        to_document_id=target.id,
        relationship_type=edge.relationship_type.value,
        created=created,
    )


# --- Registration ---------------------------------------------------------------


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="set_reaction")
    async def _set_reaction(
        ctx: Context,
        document_id: int,
        reaction: Literal["lies", "true", "needs_proof", "precedent"],
        notes: str | None = None,
    ) -> list[ReactionOut]:
        """Flag a document the way the user does in triage: `lies` (🚩 a false
        statement), `true` (✅ confirmed), `needs_proof` (🔍 asserted without
        proof) or `precedent` (⚖️ a ruling/citation to remember). Optional notes
        record why. Reactions are one per document and type and are shared with
        the user's own: setting one that is already there keeps it (notes update
        the existing one), it never removes anything. Returns all reactions on
        the document; those the agent wrote are marked `by_agent`. Needs a
        read-write token."""
        return await run(
            ctx,
            lambda db, p: set_reaction(
                db, p, document_id, UserReactionType(reaction), notes
            ),
        )

    @mcp.tool(name="add_pin")
    async def _add_pin(
        ctx: Context, document_id: int, passage_id: str, note: str
    ) -> PinOut:
        """Attach a margin note to one of a document's highlighted key passages.
        `passage_id` must be an id from get_document → key_passages. Needs a
        read-write token."""
        return await run(
            ctx, lambda db, p: add_pin(db, p, document_id, passage_id, note)
        )

    @mcp.tool(name="create_claim")
    async def _create_claim(
        ctx: Context,
        case_id: str,
        text: str,
        claim_type: Literal["factual", "legal", "procedural"],
        document_id: int,
        excerpt: str | None = None,
    ) -> ClaimCreated:
        """Add a claim (one atomic factual, legal or procedural assertion) to the
        case's Truth Map, originating in the given document of that case;
        `excerpt` is the supporting passage. The claim starts as `asserted`.
        Check get_truth_map first: `similar_claims` in the result lists the
        nearest existing claims in the case (lower distance = more similar), and
        creation is never blocked by them. Needs a read-write token."""
        return await run(
            ctx,
            lambda db, p: create_claim(
                db, p, case_id, text, ClaimType(claim_type), document_id, excerpt
            ),
        )

    @mcp.tool(name="link_claim_evidence")
    async def _link_claim_evidence(
        ctx: Context,
        claim_id: int,
        document_id: int,
        role: Literal["supports", "contests", "refutes", "cites_as_proof"],
        excerpt: str | None = None,
    ) -> EvidenceLinked:
        """Record that a document bears on an existing claim: `supports`,
        `contests`, `refutes` or `cites_as_proof`. A document holds one link per
        claim — an existing one is replaced by this decision, except the
        document that originally asserted the claim (left unchanged,
        `changed=false`). Like in the UI, `contests` moves an asserted claim to
        contested and `refutes` marks it refuted (established claims never
        move). Needs a read-write token."""
        return await run(
            ctx,
            lambda db, p: link_claim_evidence(
                db, p, claim_id, document_id, ClaimEvidenceRole(role), excerpt
            ),
        )

    @mcp.tool(name="set_claim_status")
    async def _set_claim_status(
        ctx: Context,
        claim_id: int,
        status: Literal["asserted", "contested", "needs_proof", "established"],
    ) -> ClaimStatusOut:
        """Change a claim's status to `asserted`, `contested`, `needs_proof` or
        `established` (any move between these except staying put). `refuted` is
        set only by the analysis pipeline, and a refuted claim can only be
        reopened to `asserted`. Illegal moves return an error. Needs a
        read-write token."""
        return await run(
            ctx,
            lambda db, p: set_claim_status(db, p, claim_id, ClaimStatus(status)),
        )

    @mcp.tool(name="create_action_item")
    async def _create_action_item(
        ctx: Context,
        case_id: str,
        title: str,
        due_date: str,
        action_type: Literal[
            "deadline",
            "court_date",
            "response_required",
            "filing_required",
            "payment_due",
        ],
        description: str | None = None,
        location: str | None = None,
        proceeding_id: int | None = None,
        source_document_id: int | None = None,
    ) -> ActionItemOut:
        """Add a deadline or court date to a case. `due_date` is an ISO date
        (YYYY-MM-DD; the time of day is not stored, put it in the description).
        A case can hold one item per type and date — a clash returns an error
        naming the existing item. Items added here are kept when documents are
        re-analysed. Needs a read-write token."""
        return await run(
            ctx,
            lambda db, p: create_action_item(
                db,
                p,
                case_id,
                title,
                due_date,
                ActionItemType(action_type),
                description,
                location,
                proceeding_id,
                source_document_id,
            ),
        )

    @mcp.tool(name="update_action_item")
    async def _update_action_item(
        ctx: Context,
        action_item_id: int,
        status: Literal["open", "completed", "dismissed"],
    ) -> ActionItemOut:
        """Mark a deadline or court date `completed`, `dismissed` or `open`
        again. Needs a read-write token."""
        return await run(
            ctx,
            lambda db, p: update_action_item(
                db, p, action_item_id, ActionItemStatus(status)
            ),
        )

    @mcp.tool(name="create_relationship")
    async def _create_relationship(
        ctx: Context,
        from_document_id: int,
        to_document_id: int,
        relationship_type: Literal[
            "replies_to",
            "references",
            "attaches_as_proof",
            "supersedes",
            "cited_by",
            "encloses",
            "copy_of",
        ],
        notes: str | None = None,
    ) -> RelationshipOut:
        """Link two documents of the same case. Direction is from → to:
        `replies_to` (from answers to), `references`, `attaches_as_proof`
        (from is cited as proof in to), `supersedes` (from replaces to),
        `cited_by`, `encloses` (cover letter from → enclosure to) and `copy_of`
        (from is a copy of the original to). Idempotent; a link the user once
        rejected is refused. A reply closes the other document's open thread.
        Needs a read-write token."""
        return await run(
            ctx,
            lambda db, p: create_relationship(
                db,
                p,
                from_document_id,
                to_document_id,
                RelationshipType(relationship_type),
                notes,
            ),
        )
