"""Command-palette search."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.schemas.search import SearchCase, SearchContact, SearchDocument, SearchResults
from app.services import access_service
from app.services.search_service import SearchService

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResults)
async def search(
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(30, ge=6, le=120, description="Across all result kinds"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Palette and results page; ``limit`` is split across documents, cases and contacts."""
    result = await run_in_threadpool(SearchService(db).search_all, q, limit=limit)
    documents, cases = result.documents, result.cases
    visible = access_service.visible_case_ids(db, user)
    if visible is not None:
        documents = [d for d in documents if d.case_id in visible]
        cases = [c for c in cases if c.id in visible]
    senders = list(dict.fromkeys(d.sender for d in documents if d.sender))[
        : max(5, limit // 6)
    ]
    return SearchResults(
        documents=[
            SearchDocument(id=d.id, title=d.title, case_id=d.case_id) for d in documents
        ],
        cases=[SearchCase(id=c.id, title=c.title, status=c.status) for c in cases],
        contacts=[SearchContact(name=s) for s in senders],
        total=len(documents) + len(cases),
    )
