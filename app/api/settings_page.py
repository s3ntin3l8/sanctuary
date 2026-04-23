"""Settings page router — /settings/* subpages."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.helpers import render_page
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.user_settings_service import _get_or_create

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


def _stats(db):
    from app.models.database import Case, Claim, Document, LegalCost

    db_path = Path("data/sanctuary.db")
    db_size = db_path.stat().st_size if db_path.exists() else 0
    return {
        "db_size_mb": round(db_size / 1024 / 1024, 2),
        "doc_count": db.query(Document).count(),
        "case_count": db.query(Case).count(),
        "claim_count": db.query(Claim).count(),
        "cost_count": db.query(LegalCost).count(),
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_root():
    return RedirectResponse(url="/settings/gmail", status_code=303)


@router.get("/settings/gmail", response_class=HTMLResponse)
async def settings_gmail(request: Request, db: Session = Depends(get_db)):
    user_settings = _get_or_create(db)
    settings_json = user_settings.settings_json or {}
    return render_page(
        request,
        "pages/settings/gmail.html",
        db=db,
        settings=settings_json,
    )


@router.get("/settings/ai", response_class=HTMLResponse)
async def settings_ai(request: Request, db: Session = Depends(get_db)):
    cfg = get_effective_config(db)
    ai_health = await ai_provider.probe_health()
    ai_type = await ai_provider.get_type()
    return render_page(
        request,
        "pages/settings/ai.html",
        db=db,
        cfg=cfg,
        ai_type=str(ai_type),
        ai_health=ai_health,
    )


@router.get("/settings/appearance", response_class=HTMLResponse)
async def settings_appearance(request: Request, db: Session = Depends(get_db)):
    user_settings = _get_or_create(db)
    settings_json = user_settings.settings_json or {}
    return render_page(
        request,
        "pages/settings/appearance.html",
        db=db,
        settings=settings_json,
    )


@router.get("/settings/data", response_class=HTMLResponse)
async def settings_data(request: Request, db: Session = Depends(get_db)):
    user_settings = _get_or_create(db)
    settings_json = user_settings.settings_json or {}
    return render_page(
        request,
        "pages/settings/data.html",
        db=db,
        settings=settings_json,
        stats=_stats(db),
    )
