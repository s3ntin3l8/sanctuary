"""Settings page router — /settings/* subpages."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import config as cfg
from app.dependencies import get_current_user, get_db
from app.helpers import render_page
from app.models.database import User
from app.services.ai_config import _get_ai_section, get_embed_config
from app.services.ai_provider import chat_provider, embed_provider, ocr_provider
from app.services.timezone_service import get_timezone_choices
from app.services.user_settings_service import (
    get_ai_debug_redact,
    get_extraction_engine,
    get_party_identity,
    get_worker_concurrency,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


def _display_settings(db, user_id: int) -> dict:
    """Merged settings for template display: global AppSettings keys overlaid
    with the user's per-user keys (theme, dashboard_cards, timezone preference)."""
    from app.models.database import UserSettings
    from app.services.app_settings_service import get_json as _app_json

    merged = dict(_app_json(db))
    row = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if row and isinstance(row.settings_json, dict):
        merged.update(row.settings_json)
    return merged


def _stats(db):
    from app.models.database import Case, Claim, Document, LegalCost

    db_path = cfg.DATA_DIR / "sanctuary.db"
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
    return RedirectResponse(url="/settings/account", status_code=303)


@router.get("/settings/account", response_class=HTMLResponse)
async def settings_account(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return render_page(
        request,
        "pages/settings/account.html",
        db=db,
        account=user,
    )


@router.get("/settings/gmail", response_class=HTMLResponse)
async def settings_gmail(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return render_page(
        request,
        "pages/settings/gmail.html",
        db=db,
        settings=_display_settings(db, user.id),
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
    active_ocr_id = ai.get("active_ocr_id", "")
    embed_cfg = get_embed_config(db)

    chat_provider.reload_from_db(db)
    embed_provider.reload_from_db(db)
    ocr_provider.reload_from_db(db)

    import asyncio

    health_results = await asyncio.gather(
        *[chat_provider.probe_health(config=inst) for inst in instances]
    )
    instance_health = {
        inst["id"]: h for inst, h in zip(instances, health_results, strict=True)
    }

    # Role-first cards: each role resolves to its active instance + stored
    # model. Shared with create_instance so a newly added endpoint's cards
    # are constructed identically whether from a full GET or an OOB refresh.
    from app.api.settings_ai_config import build_role_cards

    role_cards = await build_role_cards(db)

    return render_page(
        request,
        "pages/settings/ai.html",
        db=db,
        instances=instances,
        instance_health=instance_health,
        role_cards=role_cards,
        active_chat_id=active_chat_id,
        active_embed_id=active_embed_id,
        active_ocr_id=active_ocr_id,
        embed_cfg=embed_cfg,
        extraction_engine=get_extraction_engine(db),
        worker_concurrency=get_worker_concurrency(db),
    )


@router.get("/settings/appearance", response_class=HTMLResponse)
async def settings_appearance(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return render_page(
        request,
        "pages/settings/appearance.html",
        db=db,
        settings=_display_settings(db, user.id),
        timezone_choices=get_timezone_choices(),
    )


@router.get("/settings/data", response_class=HTMLResponse)
async def settings_data(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return render_page(
        request,
        "pages/settings/data.html",
        db=db,
        settings=_display_settings(db, user.id),
        stats=_stats(db),
        ai_debug_redact=get_ai_debug_redact(db),
    )


@router.get("/settings/export", response_class=HTMLResponse)
async def settings_export(request: Request, db: Session = Depends(get_db)):
    return render_page(
        request,
        "pages/settings/export.html",
        db=db,
    )
