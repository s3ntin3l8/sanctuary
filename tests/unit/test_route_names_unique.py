"""Every route name must be unique and the JSON API keeps its ``v1_`` prefix.

Route names are what ``url_path_for`` and the OpenAPI operation ids key on;
a duplicate silently shadows the earlier route.
"""

import pytest
from fastapi.routing import APIRoute, _IncludedRouter

from app.main import app

pytestmark = pytest.mark.unit


def _api_routes():
    found = []

    def walk(wrapper):
        for candidate in wrapper.effective_candidates():
            if isinstance(candidate, _IncludedRouter):
                walk(candidate)
            elif isinstance(getattr(candidate, "original_route", None), APIRoute):
                found.append(candidate.original_route)

    for route in app.router.routes:
        if isinstance(route, _IncludedRouter):
            walk(route)
        elif isinstance(route, APIRoute):
            found.append(route)
    return found


def test_route_names_are_unique():
    names = [r.name for r in _api_routes()]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert duplicates == [], f"duplicate route names: {duplicates}"


def test_v1_routes_are_name_prefixed():
    for route in _api_routes():
        if route.path.startswith("/api/v1/"):
            assert route.name.startswith("v1_"), route.path


def test_page_and_api_names_resolve():
    assert app.url_path_for("home") == "/"
    assert app.url_path_for("case_directory") == "/cases"
    assert app.url_path_for("case_detail", case_id="ADV-1") == "/cases/ADV-1"
    assert app.url_path_for("v1_confirm_close", case_id="X") == (
        "/api/v1/cases/X/confirm-close"
    )
