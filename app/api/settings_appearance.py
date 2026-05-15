"""Appearance & Layout settings endpoints."""

import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services import timezone_service
from app.services.user_settings_service import set_dashboard_cards, set_theme

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.post("/theme")
async def save_theme(theme: str = Form(...), db: Session = Depends(get_db)):
    if theme not in ("dark", "light"):
        theme = "dark"
    set_theme(theme, db)
    db.commit()
    return Response(status_code=204)


@router.post("/dashboard-cards")
async def save_dashboard_cards(
    action_items: str = Form("off"),
    costs: str = Form("off"),
    documents: str = Form("off"),
    db: Session = Depends(get_db),
):
    cards = {
        "action_items": action_items == "on",
        "costs": costs == "on",
        "documents": documents == "on",
    }
    set_dashboard_cards(cards, db)
    db.commit()
    return Response(status_code=204)


@router.post("/timezone")
async def save_timezone(tz: str = Form(...), db: Session = Depends(get_db)):
    try:
        timezone_service.set_timezone(tz, db)
        db.commit()
    except ValueError:
        return Response(status_code=422)
    return Response(status_code=204)
