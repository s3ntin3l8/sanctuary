"""Re-exports for routers consumed by app.main during the late-import block.

The `/api/v1` dual-mount was removed (see commit history / project_audit_completion).
This file now only provides convenient names for the routers app.main actually
mounts. Other modules import their routers directly.
"""

from app.api import (
    cases,
    contacts,
    costs,
    documents,
    home,
    proceedings,
    triage,
)

home_router = home.router
triage_router = triage.router
costs_router = costs.router
documents_router = documents.router
proceedings_router = proceedings.router

__all__ = [
    "cases",
    "contacts",
    "costs",
    "documents",
    "home",
    "proceedings",
    "triage",
    "home_router",
    "triage_router",
    "costs_router",
    "documents_router",
    "proceedings_router",
]
