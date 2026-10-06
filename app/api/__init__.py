"""Re-exports for the legacy routers app.main still mounts (each cutover
removes one; other modules import their routers directly)."""

from app.api import contacts, costs, home

home_router = home.router
costs_router = costs.router

__all__ = ["contacts", "costs", "home", "home_router", "costs_router"]
