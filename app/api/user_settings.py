from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.user_settings_service import set_active_proceeding

router = APIRouter(prefix="/api/user-settings", tags=["user-settings"])


class ActiveProceedingBody(BaseModel):
    proceeding_id: int


@router.post("/active-proceeding/{case_id}", status_code=204)
def post_active_proceeding(
    case_id: str,
    body: ActiveProceedingBody,
    db: Session = Depends(get_db),
):
    set_active_proceeding(case_id, body.proceeding_id, db)
    db.commit()
    return Response(status_code=204)
