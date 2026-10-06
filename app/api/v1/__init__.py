"""Typed JSON API consumed by the SPA in ``frontend/``.

Every route here declares a Pydantic ``response_model`` and raises
:class:`app.api.v1.errors.ApiError` for failures, so the generated OpenAPI
schema (``frontend/src/api/openapi.json``) is the client's source of truth.
"""

from fastapi import APIRouter

from app.api.v1 import auth, cases, home, search, shell, worker_queue
from app.api.v1.errors import ERROR_RESPONSES

API_V1_PREFIX = "/api/v1"

router = APIRouter(prefix=API_V1_PREFIX, responses=ERROR_RESPONSES)
router.include_router(auth.router)
router.include_router(shell.router)
router.include_router(home.router)
router.include_router(cases.router)
router.include_router(search.router)
router.include_router(worker_queue.router)
