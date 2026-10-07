"""Page routers mounted by app.main; the JSON API lives in app.api.v1."""

from app.api import home

home_router = home.router

__all__ = ["home", "home_router"]
