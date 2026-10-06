"""Re-exports for the legacy routers app.main still mounts (each cutover
removes one; other modules import their routers directly)."""

from app.api import home

home_router = home.router

__all__ = ["home", "home_router"]
