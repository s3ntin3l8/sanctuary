"""Party identity settings endpoint."""

import logging

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.user_settings_service import set_party_identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.post("/parties")
async def save_parties(
    own_self: str = Form(""),
    own_parties: str = Form(""),
    db: Session = Depends(get_db),
):
    own_list = [p.strip() for p in own_parties.split(",") if p.strip()]
    set_party_identity(
        {
            "own_self": own_self.strip(),
            "own_parties": own_list,
        },
        db,
    )
    db.commit()
    return Response(status_code=204)
