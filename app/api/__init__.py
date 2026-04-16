from fastapi import APIRouter

from app.api import cases, contacts, costs, dashboard, documents, timeline_api, triage

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(cases.router)
api_router.include_router(documents.router)
api_router.include_router(contacts.router)
api_router.include_router(timeline_api.router)

__all__ = [
    "api_router",
    "dashboard_router",
    "triage_router",
    "costs_router",
    "documents_router",
]


dashboard_router = dashboard.router
triage_router = triage.router
costs_router = costs.router
documents_router = documents.router
