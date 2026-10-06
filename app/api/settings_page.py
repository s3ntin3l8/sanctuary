"""Settings and admin screens are SPA routes (data comes from /api/v1/settings/*)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse, Response

from app.dependencies import get_current_admin
from app.spa import spa_index

router = APIRouter(tags=["pages"], include_in_schema=False)

SETTINGS_TABS = ("account", "gmail", "identity", "ai", "appearance", "data", "export")


@router.get("/settings")
def settings_root() -> Response:
    return RedirectResponse(url="/settings/account", status_code=303)


@router.get("/settings/{tab}")
def settings_tab(tab: str) -> Response:
    if tab not in SETTINGS_TABS:
        return RedirectResponse(url="/settings/account", status_code=303)
    return spa_index()


@router.get("/admin/users", dependencies=[Depends(get_current_admin)])
def admin_users_page() -> Response:
    return spa_index()
