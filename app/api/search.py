from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.dependencies import get_current_user, get_db
from app.helpers import render_page
from app.models.database import User
from app.services import access_service
from app.services.search_service import SearchService

router = APIRouter(tags=["search"])


def _filter_visible(db, user, documents, cases):
    """Drop results the user may not see (per-user isolation). Admins see all."""
    vis = access_service.visible_case_ids(db, user)
    if vis is None:
        return documents, cases
    documents = [d for d in documents if d.case_id in vis]
    cases = [c for c in cases if c.id in vis]
    return documents, cases


@router.get("/api/search")
async def api_search(
    q: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """API endpoint for live search autocomplete."""
    if len(q) < 2:
        return {"documents": [], "cases": [], "contacts": [], "total": 0}

    search_service = SearchService(db)
    result = await run_in_threadpool(search_service.search_all, q, limit=30)
    documents, cases = _filter_visible(db, user, result.documents, result.cases)

    # Simple JSON serialization
    return {
        "documents": [
            {"id": d.id, "title": d.title, "case_id": d.case_id} for d in documents
        ],
        "cases": [
            {"id": c.id, "title": c.title, "status": c.status.value} for c in cases
        ],
        "contacts": [{"name": d.sender} for d in documents if d.sender][
            :5
        ],  # Simplified contact search from doc senders
        "total": len(documents) + len(cases),
    }


@router.get("/search")
async def search_page(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Full search results page."""
    search_service = SearchService(db)

    documents = []
    cases = []
    contacts = []
    total = 0

    if q:
        result = await run_in_threadpool(search_service.search_all, q, limit=100)
        documents, cases = _filter_visible(db, user, result.documents, result.cases)

        # Extract unique contacts from documents
        unique_contacts = set()
        for doc in documents:
            if doc.sender:
                unique_contacts.add(doc.sender)
        contacts = sorted(unique_contacts)
        total = len(documents) + len(cases) + len(contacts)

    return render_page(
        request,
        "pages/search.html",
        db=db,
        q=q,
        documents=documents,
        cases=cases,
        contacts=contacts,
        total=total,
    )
