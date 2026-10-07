"""Screens owned by the SPA; data comes from /api/v1."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.dependencies import get_current_user
from app.spa import spa_index

router = APIRouter(
    tags=["pages"], include_in_schema=False, dependencies=[Depends(get_current_user)]
)


@router.get("/triage")
def triage_page() -> Response:
    return spa_index()


@router.get("/ingest/slice/{batch_id}")
def slicing_review(batch_id: int) -> Response:
    return spa_index()


@router.get("/document/{doc_id}")
def document_page(doc_id: int) -> Response:
    return spa_index()


@router.get("/cases")
def case_directory() -> Response:
    return spa_index()


# Legacy templates still build links with url_path_for("case_detail").
@router.get("/cases/{case_id}", name="case_detail")
def case_detail(case_id: str) -> Response:
    return spa_index()


@router.get("/costs")
def costs_page() -> Response:
    return spa_index()


@router.get("/contacts")
def contacts_page() -> Response:
    return spa_index()


@router.get("/search")
def search_page() -> Response:
    return spa_index()
