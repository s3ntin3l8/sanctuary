"""Settings → Appearance: per-user theme and dashboard panels; global timezone."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.dependencies import get_current_admin, get_current_user, get_db
from app.models.database import User
from app.models.enums import AuditEventType
from app.schemas.settings import (
    AppearanceView,
    DashboardCards,
    ThemeUpdate,
    TimezoneUpdate,
)
from app.services import audit_service, timezone_service
from app.services.app_settings_service import get_json as app_settings_json
from app.services.user_settings_service import (
    _user_settings,
    set_dashboard_cards,
    set_theme,
)

router = APIRouter(prefix="/settings/appearance", tags=["settings"])


def _view(db: Session, user: User) -> AppearanceView:
    row = _user_settings(db, user.id)
    mine = (row.settings_json or {}) if row else {}
    theme: Literal["dark", "light"] = (
        "light" if mine.get("theme") == "light" else "dark"
    )
    return AppearanceView(
        theme=theme,
        dashboard_cards=DashboardCards(**(mine.get("dashboard_cards") or {})),
        timezone=app_settings_json(db).get("timezone") or "Europe/Berlin",
        timezone_choices=timezone_service.get_timezone_choices(),
    )


@router.get("", response_model=AppearanceView)
def appearance(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _view(db, user)


@router.put("/theme", response_model=AppearanceView)
def save_theme(
    body: ThemeUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    set_theme(body.theme, db, user.id)
    db.commit()
    return _view(db, user)


@router.put("/dashboard-cards", response_model=AppearanceView)
def save_dashboard_cards(
    body: DashboardCards,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    set_dashboard_cards(body.model_dump(), db, user.id)
    db.commit()
    return _view(db, user)


@router.put(
    "/timezone",
    response_model=AppearanceView,
    dependencies=[Depends(get_current_admin)],
)
def save_timezone(
    body: TimezoneUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The display timezone is global (one workspace), so admins only."""
    try:
        timezone_service.set_timezone(body.tz, db)
    except ValueError as exc:
        raise ApiError(422, "invalid_timezone", f"Unknown timezone: {body.tz}") from exc
    audit_service.record(
        db, AuditEventType.SETTINGS_TIMEZONE_CHANGED, payload={"timezone": body.tz}
    )
    db.commit()
    return _view(db, user)
