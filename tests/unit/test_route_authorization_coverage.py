"""Route-coverage meta-test for PR5's API-wide authorization sweep.

Walks every registered route and, for each one whose path carries an
`{..._id}` parameter, asserts its resolved dependency tree includes a
recognized access guard. New ID-bearing routes must either wire one of the
`app.api.access_guards` dependencies (or `require_triage_object_owner` for
triage/slicing) or be added to `_ALLOWLIST` below with a reason.

This can't see inline `access_service.can_view_case(...)` checks or guard
logic embedded directly in a route body — those are intentionally allowlisted
here and covered instead by dedicated route tests.
"""

import re

import pytest
from fastapi.routing import APIRoute, _IncludedRouter

from app.dependencies import get_current_admin
from app.main import app

_ID_PARAM_RE = re.compile(r"\{([a-zA-Z_]*_id)\}")

# path -> reason it's exempt from the Depends-based guard requirement.
_ALLOWLIST: dict[str, str] = {
    # Inline access_service.can_view_case checks (custom queries/responses
    # that don't fit the Depends-based guard shape) — see the route bodies.
    "GET /document/{doc_id}": "inline check_owned_or_case_access (custom joinedload query)",
    "GET /upload/status/{doc_id}": "inline check_owned_or_case_access (non-raising empty-HTML denial)",
    "GET /cases/{case_id}/brief": "inline access_service.can_view_case",
    "GET /cases/{case_id}": "inline access_service.can_view_case",
    "GET /cases/{case_id}/document/{doc_id}/hud": "inline access_service.can_view_case",
    "GET /cases/{case_id}/document/{doc_id}": "inline access_service.can_view_case",
    # case_sharing.py's own inline owner-or-admin guard (stricter than the
    # generic edit guard: only the owner or an admin may manage shares).
    "GET /cases/{case_id}/sharing": "inline _require_owner_or_admin",
    "POST /cases/{case_id}/shares": "inline _require_owner_or_admin",
    "POST /cases/{case_id}/shares/{user_id}/remove": "inline _require_owner_or_admin",
    # Admin-only user management — global admin capability, not scoped to a
    # case/document; get_current_admin (403 for non-admins) is the guard.
    "POST /admin/users/{user_id}/toggle-active": "admin-only via get_current_admin",
    "POST /admin/users/{user_id}/role": "admin-only via get_current_admin",
    "POST /admin/users/{user_id}/reset-password": "admin-only via get_current_admin",
    "POST /admin/users/{user_id}/delete": "admin-only via get_current_admin",
    "POST /admin/users/{user_id}/reassign-cases": "admin-only via get_current_admin",
    # Global AI-provider config, not a per-user/per-case resource — any
    # authenticated user may configure it, consistent with the rest of
    # /api/settings/*.
    "POST /api/settings/ai/instances/{instance_id}": "global app config, not per-object",
    "DELETE /api/settings/ai/instances/{instance_id}": "global app config, not per-object",
    "POST /api/settings/ai/instances/{instance_id}/test": "global app config, not per-object",
}


def _flatten_id_routes():
    """Yield (label, dependant) for every APIRoute whose path has an id param.

    FastAPI's router inclusion is lazy (`_IncludedRouter` wraps a router
    until its "effective" routes are resolved), so top-level routes and
    included-router routes are walked separately.
    """
    results = []

    def walk(wrapper):
        for candidate in wrapper.effective_candidates():
            if isinstance(candidate, _IncludedRouter):
                walk(candidate)
            elif isinstance(getattr(candidate, "original_route", None), APIRoute):
                results.append(candidate)

    for route in app.router.routes:
        if isinstance(route, _IncludedRouter):
            walk(route)
        elif isinstance(route, APIRoute):
            results.append(route)

    for ctx in results:
        path = getattr(ctx, "path", None)
        methods = getattr(ctx, "methods", None) or set()
        dependant = getattr(ctx, "dependant", None)
        if not path or dependant is None or not _ID_PARAM_RE.search(path):
            continue
        for method in sorted(methods):
            yield f"{method} {path}", dependant


def _all_dependency_calls(dependant) -> list:
    calls = []
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        calls.append(dep.call)
        stack.extend(dep.dependencies)
    return calls


def _has_recognized_guard(dependant) -> bool:
    from app.api.triage.ownership import require_triage_object_owner

    calls = _all_dependency_calls(dependant)
    if any(getattr(c, "_is_access_guard", False) for c in calls):
        return True
    if require_triage_object_owner in calls:
        return True
    return get_current_admin in calls


@pytest.mark.unit
def test_every_id_bearing_route_has_an_access_guard():
    unguarded = []
    for label, dependant in _flatten_id_routes():
        if label in _ALLOWLIST:
            continue
        if not _has_recognized_guard(dependant):
            unguarded.append(label)

    assert not unguarded, (
        "ID-bearing routes with no recognized access guard (wire "
        "app.api.access_guards or add to _ALLOWLIST with a reason): "
        f"{sorted(unguarded)}"
    )


@pytest.mark.unit
def test_allowlist_entries_still_exist_as_routes():
    """Catch stale allowlist entries (route renamed/removed)."""
    known = {label for label, _ in _flatten_id_routes()}
    stale = set(_ALLOWLIST) - known
    assert not stale, f"Allowlist entries for routes that no longer exist: {stale}"
