"""Typed JSON API consumed by the SPA in ``frontend/``.

Every route here declares a Pydantic ``response_model`` and raises
:class:`app.api.v1.errors.ApiError` for failures, so the generated OpenAPI
schema (``frontend/src/api/openapi.json``) is the client's source of truth.
"""

from fastapi import APIRouter

from app.api.v1 import (
    admin_users,
    auth,
    case_detail,
    cases,
    chat,
    claims,
    contacts,
    costs,
    documents,
    gmail_import,
    home,
    notifications,
    search,
    settings_account,
    settings_ai,
    settings_appearance,
    settings_data,
    settings_gmail,
    settings_identity,
    shell,
    slicing,
    triage,
    upload,
    worker_queue,
)
from app.api.v1.errors import ERROR_RESPONSES

API_V1_PREFIX = "/api/v1"

router = APIRouter(prefix=API_V1_PREFIX, responses=ERROR_RESPONSES)
for _module in (
    auth,
    shell,
    home,
    notifications,
    cases,
    search,
    worker_queue,
    settings_account,
    settings_gmail,
    gmail_import,
    settings_identity,
    settings_ai,
    settings_appearance,
    settings_data,
    admin_users,
    triage,
    documents,
    upload,
    slicing,
    chat,
    case_detail,
    claims,
    costs,
    contacts,
):
    # Route names double as OpenAPI operation ids and must stay unique app-wide;
    # the prefix keeps a v1 handler from colliding with the page route of the
    # same name (see tests/unit/test_route_names_unique.py).
    for _route in _module.router.routes:
        _route.name = f"v1_{_route.name}"
    router.include_router(_module.router)
