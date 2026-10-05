"""Sign-in screens and logout.

``/login`` and ``/signup`` are SPA routes (the forms post to ``/api/v1/auth``);
the server only decides which of the two a visitor may see. All three paths
are on the public allowlist (see app.main._is_public_path).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services import auth_service
from app.spa import spa_index

router = APIRouter(tags=["auth"], include_in_schema=False)


@router.get("/login")
def login_page(db: Session = Depends(get_db)) -> Response:
    # Fresh install with no accounts → send to first-run admin creation.
    if auth_service.count_users(db) == 0:
        return RedirectResponse("/signup", status_code=303)
    return spa_index()


@router.get("/signup")
def signup_page(db: Session = Depends(get_db)) -> Response:
    first_run = auth_service.count_users(db) == 0
    if not first_run and not auth_service.signup_enabled(db):
        return RedirectResponse("/login", status_code=303)
    return spa_index()


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
