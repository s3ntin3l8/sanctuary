"""Every route name must be unique: legacy Jinja templates resolve URLs by name.

During the SPA migration a /api/v1 handler that reuses a legacy function name
would silently hijack ``url_path_for`` in templates that are still served.
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


def test_legacy_template_names_resolve_to_legacy_pages():
    assert app.url_path_for("home") == "/"
    assert app.url_path_for("case_directory") == "/cases"
    assert app.url_path_for("case_detail", case_id="ADV-1") == "/cases/ADV-1"
    assert app.url_path_for("v1_confirm_close", case_id="X") == (
        "/api/v1/cases/X/confirm-close"
    )
