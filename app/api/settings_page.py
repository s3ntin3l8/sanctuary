"""Settings page router — /settings/* subpages."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.helpers import render_page
from app.services.ai_config import _get_ai_section, get_embed_config
from app.services.ai_provider import chat_provider, embed_provider
from app.services.timezone_service import get_timezone_choices
from app.services.user_settings_service import (
    _get_or_create,
    get_ai_debug_redact,
    get_party_identity,
)

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


@router.get("/settings/parties", response_class=HTMLResponse)
async def settings_parties(request: Request, db: Session = Depends(get_db)):
    party_identity = get_party_identity(db)
    ai = _get_ai_section(db)
    user_context = ai.get("user_context", "")
    return render_page(
        request,
        "pages/settings/parties.html",
        db=db,
        party_identity=party_identity,
        user_context=user_context,
    )


@router.get("/settings/ai", response_class=HTMLResponse)
async def settings_ai(request: Request, db: Session = Depends(get_db)):
    from app.services.ai_config import list_instances

    ai = _get_ai_section(db)
    instances = list_instances(db)
    active_chat_id = ai.get("active_chat_id", "")
    active_embed_id = ai.get("active_embed_id", "")
    embed_cfg = get_embed_config(db)

    chat_provider.reload_from_db(db)
    embed_provider.reload_from_db(db)

    import asyncio

    health_results = await asyncio.gather(
        *[chat_provider.probe_health(config=inst) for inst in instances]
    )
    instance_health = {
        inst["id"]: h for inst, h in zip(instances, health_results, strict=True)
    }

    return render_page(
        request,
        "pages/settings/ai.html",
        db=db,
        instances=instances,
        instance_health=instance_health,
        active_chat_id=active_chat_id,
        active_embed_id=active_embed_id,
        embed_cfg=embed_cfg,
        ai_debug_redact=get_ai_debug_redact(db),
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
        timezone_choices=get_timezone_choices(),
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


@router.get("/settings/export", response_class=HTMLResponse)
async def settings_export(request: Request, db: Session = Depends(get_db)):
    return render_page(
        request,
        "pages/settings/export.html",
        db=db,
    )
