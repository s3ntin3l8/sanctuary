"""Settings → Identity & context (global: who 'we' are, and the AI user context)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.dependencies import get_current_admin, get_db
from app.schemas.settings import IdentityView
from app.services.ai_config import _get_ai_section, set_user_context
from app.services.ai_provider import chat_provider
from app.services.user_settings_service import get_party_identity, set_party_identity

router = APIRouter(
    prefix="/settings/identity",
    tags=["settings"],
    dependencies=[Depends(get_current_admin)],
)


def _view(db: Session) -> IdentityView:
    identity = get_party_identity(db)
    return IdentityView(
        own_self=identity.get("own_self", "") or "",
        own_parties=list(identity.get("own_parties") or []),
        user_context=_get_ai_section(db).get("user_context", "") or "",
    )


@router.get("", response_model=IdentityView)
def identity(db: Session = Depends(get_db)):
    return _view(db)


@router.put("", response_model=IdentityView)
def save_identity(body: IdentityView, db: Session = Depends(get_db)):
    set_party_identity(
        {
            "own_self": body.own_self.strip(),
            "own_parties": [p.strip() for p in body.own_parties if p.strip()],
        },
        db,
    )
    set_user_context(db, body.user_context)
    db.commit()
    chat_provider.reload_from_db(db)
    return _view(db)
