"""Route-coverage meta-test for PR5's API-wide authorization sweep.

Walks every registered route and, for each one whose path carries an
`{..._id}` parameter, asserts its resolved dependency tree includes a
recognized access guard. New ID-bearing routes must either wire one of the
`app.api.access_guards` dependencies (or the v1 triage/slicing owner
resolvers) or be added to `_ALLOWLIST` below with a reason.

This can't see inline `access_service.can_view_case(...)` checks or guard
logic embedded directly in a route body — those are intentionally allowlisted
here and covered instead by dedicated route tests.

A second test covers ids that do NOT arrive in the path — query parameters,
form fields and request-body fields named `*_id`/`*_ids`. A path guard cannot
see these, and a client-supplied id that is never checked is exactly how a user
attaches their data to someone else's (upload `parent_id`, a triage
`lead_doc_id` from another batch). Every such route must be listed in
`_BODY_ID_REVIEWED` with the reason the id cannot cross an access boundary.
"""

import re

import pydantic
import pytest
from fastapi.routing import APIRoute, _IncludedRouter

from app.dependencies import get_current_admin
from app.main import app

_ID_PARAM_RE = re.compile(r"\{([a-zA-Z_]*_id)\}")

# path -> reason it's exempt from the Depends-based guard requirement.
_ALLOWLIST: dict[str, str] = {
    # SPA shell only — the page is static HTML; the data call under /api/v1
    # carries the owner check.
    "GET /ingest/slice/{batch_id}": "SPA index; guarded by the v1 data route",
    "GET /document/{doc_id}": "SPA index; guarded by the v1 data route",
    "GET /cases/{case_id}": "SPA index; guarded by the v1 data route",
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
    from app.api.v1 import case_detail as v1_case_detail
    from app.api.v1 import documents as v1_documents
    from app.api.v1 import slicing as v1_slicing
    from app.api.v1 import triage as v1_triage

    # Per-object owner resolvers of the v1 API (404 unless the caller owns
    # the batch/document/case) and the relationship edit guard.
    owner_guards = {
        v1_triage.owned_batch,
        v1_triage.owned_document,
        v1_slicing.owned_batch,
        v1_documents._owned_relationship,
        v1_case_detail._owned_case,
    }
    calls = _all_dependency_calls(dependant)
    if any(getattr(c, "_is_access_guard", False) for c in calls):
        return True
    if owner_guards & set(calls):
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


# --- ids outside the path -----------------------------------------------------

_ANY_ID_RE = re.compile(r"(^|_)ids?$|_id$|_ids$")

# route -> why the non-path id(s) it takes cannot cross an access boundary.
# Add a route here only after reading the handler; "it has a path guard" is not
# a reason, because the path guard does not look at these fields.
_BODY_ID_REVIEWED: dict[str, str] = {
    "GET /api/v1/chat/conversations": "scope_id checked by check_scope_access",
    "POST /api/v1/chat/conversations": "scope_id checked by check_scope_access",
    "POST /api/v1/chat/conversations/{conversation_id}/messages": (
        "proceeding_id only narrows retrieval inside the already-guarded case"
    ),
    "GET /api/v1/triage": "case/proceeding filters apply to the caller's own bundles",
    "GET /api/v1/upload/target": "case_id resolved via _editable_case",
    "POST /api/v1/upload": "case_id via _editable_case, parent_id via _editable_parent",
    "POST /api/v1/admin/users/{user_id}/reassign-cases": "admin-only route",
    "PUT /api/v1/settings/ai/roles/{role}": "admin-only route",
    "POST /api/v1/cases": "case_id is the id of the new case, not a reference",
    "POST /api/v1/cases/{case_id}/costs": (
        "proceeding_id verified to belong to the guarded case"
    ),
    "POST /api/v1/documents/{doc_id}/pins": (
        "passage_id is an anchor inside the guarded document"
    ),
    "PUT /api/v1/documents/{doc_id}/metadata": (
        "internal_id is a free-text reference, not an object id"
    ),
    "POST /api/v1/gmail/import": "gmail_ids are resolved within the caller's own index",
    "POST /api/v1/triage/confirm": (
        "batch/doc ownership via _key_owned, case via _require_editable_target"
    ),
    "POST /api/v1/triage/batch/assign": (
        "keys owner-checked per bundle, case via _require_editable_target"
    ),
    "PUT /api/v1/triage/bundles/{batch_id}/cover": (
        "set_cover_letter requires the doc to be in the guarded batch"
    ),
    "PUT /api/v1/triage/bundles/{batch_id}/groups/{sub_group_id}": (
        "rename_sub_group matches the sub-group to this batch"
    ),
    "PUT /api/v1/triage/bundles/{batch_id}/groups/{sub_group_id}/order": (
        "reorder_documents only updates docs in this batch and only trusts a "
        "lead_doc_id sub-group that belongs to it"
    ),
}


def _model_id_fields(annotation) -> list[str]:
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return [n for n in annotation.model_fields if _ANY_ID_RE.search(n)]
    return []


def _routes_taking_non_path_ids() -> dict[str, list[str]]:
    contexts = []

    def walk(wrapper):
        for candidate in wrapper.effective_candidates():
            if isinstance(candidate, _IncludedRouter):
                walk(candidate)
            elif isinstance(getattr(candidate, "original_route", None), APIRoute):
                contexts.append(candidate)

    for route in app.router.routes:
        if isinstance(route, _IncludedRouter):
            walk(route)
        elif isinstance(route, APIRoute):
            contexts.append(route)

    found: dict[str, list[str]] = {}
    for ctx in contexts:
        dependant = getattr(ctx, "dependant", None)
        path = getattr(ctx, "path", None)
        if dependant is None or not path:
            continue
        names = [
            f"query:{p.name}"
            for p in dependant.query_params
            if _ANY_ID_RE.search(p.name)
        ]
        for p in dependant.body_params:
            if _ANY_ID_RE.search(p.name):
                names.append(f"body:{p.name}")
            annotation = getattr(getattr(p, "field_info", None), "annotation", None)
            names += [f"body.{n}" for n in _model_id_fields(annotation)]
        if names:
            for method in sorted(getattr(ctx, "methods", None) or ()):
                found[f"{method} {path}"] = names
    return found


@pytest.mark.unit
def test_every_route_taking_ids_outside_the_path_was_reviewed():
    unreviewed = {
        label: names
        for label, names in _routes_taking_non_path_ids().items()
        if label not in _BODY_ID_REVIEWED
    }
    assert not unreviewed, (
        "Routes taking *_id fields in the query/form/body that nobody reviewed "
        "(read the handler, make sure each id is checked against the caller's "
        f"access, then add it to _BODY_ID_REVIEWED with the reason): {unreviewed}"
    )


@pytest.mark.unit
def test_reviewed_body_id_entries_still_exist():
    stale = set(_BODY_ID_REVIEWED) - set(_routes_taking_non_path_ids())
    assert not stale, f"_BODY_ID_REVIEWED entries for routes that changed: {stale}"
