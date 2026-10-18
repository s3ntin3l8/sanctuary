"""Read tools: the case list and a case overview."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.mcp.db import run
from app.mcp.principal import McpPrincipal
from app.mcp.serializers import iso_date
from app.models.database import Case
from app.repositories.action_item import ActionItemRepository
from app.repositories.document import DocumentRepository
from app.services import case_overview_service


class CaseSummary(BaseModel):
    case_id: str
    title: str
    status: str
    case_type: str
    is_draft: bool
    document_count: int
    open_action_items: int
    documents_needing_review: int
    last_ingest: str | None


def list_cases(
    db: Session,
    p: McpPrincipal,
    status: str | None = None,
    query: str | None = None,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[CaseSummary]:
    readable = p.readable_case_ids(db)
    q = db.query(Case).filter(Case.id != "_TRIAGE")
    if readable is not None:
        q = q.filter(Case.id.in_(readable))
    if status:
        q = q.filter(Case.status == status)
    if query:
        like = f"%{query}%"
        q = q.filter(Case.title.ilike(like) | Case.id.ilike(like))
    q = q.order_by(Case.ingest_date.desc().nullslast(), Case.id)
    if offset:
        q = q.offset(offset)
    if limit is not None:
        q = q.limit(limit)
    cases = q.all()
    ids = [c.id for c in cases]
    docs = DocumentRepository(db).bulk_count_by_case(ids)
    review = DocumentRepository(db).bulk_count_needs_review_by_case(ids)
    actions = ActionItemRepository(db).bulk_count_open_by_case(ids)
    return [
        CaseSummary(
            case_id=c.id,
            title=c.title,
            status=c.status.value,
            case_type=c.case_type.value,
            is_draft=bool(c.is_draft),
            document_count=docs.get(c.id, 0),
            open_action_items=actions.get(c.id, 0),
            documents_needing_review=review.get(c.id, 0),
            last_ingest=iso_date(c.ingest_date),
        )
        for c in cases
    ]


# Fields the dashboard shell needs but an agent does not (documents have their own tool).
_OVERVIEW_EXCLUDE = {
    "documents",
    "new_doc_count",
    "last_visit",
    "can_edit",
    "can_manage_sharing",
    "active_proceeding_id",
}


def get_case(db: Session, p: McpPrincipal, case_id: str) -> dict[str, Any]:
    case = p.require_case(db, case_id)
    detail = case_overview_service.build_case_detail(
        db, p.user, case, active_proceeding_id=None, last_visit=None
    )
    out = detail.model_dump(mode="json", exclude=_OVERVIEW_EXCLUDE)
    out["case_id"] = out.pop("id")
    for view, item in zip(detail.action_items, out["action_items"], strict=True):
        item["due_date"] = iso_date(view.due_date)
    return out


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="list_cases")
    async def _list_cases(
        ctx: Context, status: str | None = None, query: str | None = None
    ) -> list[CaseSummary]:
        """List the cases this token can see, newest activity first.

        Optionally filter by case status or by a text match on case id/title.
        """
        return await run(ctx, lambda db, p: list_cases(db, p, status, query))

    @mcp.tool(name="get_case")
    async def _get_case(ctx: Context, case_id: str) -> dict[str, Any]:
        """Overview of one case: proceedings (with court file numbers), parties,
        the AI case brief, open deadlines, cost exposure, open claim count and
        any dormancy alert. Use list_documents for the documents themselves."""
        return await run(ctx, lambda db, p: get_case(db, p, case_id))
