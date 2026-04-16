from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.helpers import render_page
from app.services.search_service import SearchService

router = APIRouter(tags=["search"])


@router.get("/api/search")
async def api_search(q: str, db: Session = Depends(get_db)):
    """API endpoint for live search autocomplete."""
    if len(q) < 2:
        return {"documents": [], "cases": [], "contacts": [], "total": 0}

    search_service = SearchService(db)
    result = search_service.search_all(q, limit=30)

    # Simple JSON serialization
    return {
        "documents": [
            {"id": d.id, "title": d.title, "case_id": d.case_id}
            for d in result.documents
        ],
        "cases": [
            {"id": c.id, "title": c.title, "status": c.status.value}
            for c in result.cases
        ],
        "contacts": [{"name": d.sender} for d in result.documents if d.sender][
            :5
        ],  # Simplified contact search from doc senders
        "total": result.total,
    }


@router.get("/search")
async def search_page(request: Request, q: str = "", db: Session = Depends(get_db)):
    """Full search results page."""
    search_service = SearchService(db)

    documents = []
    cases = []
    contacts = []
    total = 0

    if q:
        result = search_service.search_all(q, limit=100)
        documents = result.documents
        cases = result.cases

        # Extract unique contacts from documents
        unique_contacts = set()
        for doc in documents:
            if doc.sender:
                unique_contacts.add(doc.sender)
        contacts = sorted(unique_contacts)
        total = result.total + len(contacts)

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
