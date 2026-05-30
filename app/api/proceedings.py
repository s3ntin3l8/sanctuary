from fastapi import APIRouter, Depends, Form, HTTPException, Response
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.models.enums import ProceedingCourtLevel, ProceedingStatus
from app.repositories.proceeding import ProceedingRepository
from app.services.case_service import CaseService
from app.services.user_settings_service import set_active_proceeding

router = APIRouter(prefix="/proceedings", tags=["proceedings"])


@router.patch("/{proceeding_id}")
async def update_proceeding(
    proceeding_id: int,
    court_name: str = Form(None),
    court_level: ProceedingCourtLevel = Form(None),
    az_court: str = Form(None),
    subject_matter: str = Form(None),
    status: ProceedingStatus = Form(None),
    db: Session = Depends(get_db),
):
    """Update a proceeding and return HX-Refresh header."""
    repo = ProceedingRepository(db)
    proceeding = repo.get(proceeding_id)
    if not proceeding:
        raise HTTPException(status_code=404, detail="Proceeding not found")

    update_data = {}
    if court_name is not None:
        update_data["court_name"] = court_name
    if court_level is not None:
        update_data["court_level"] = court_level
    if az_court is not None:
        update_data["az_court"] = az_court
    if subject_matter is not None:
        update_data["subject_matter"] = subject_matter
    if status is not None:
        update_data["status"] = status

    if update_data:
        repo.update(proceeding_id, **update_data)
        db.commit()

    return Response(headers={"HX-Refresh": "true"})


@router.delete("/{proceeding_id}")
async def delete_proceeding(
    proceeding_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete an empty proceeding (no documents, batches, action items, or costs).
    Refuses to delete the last proceeding of a case."""
    try:
        result = CaseService(db).delete_empty_proceeding(proceeding_id, user.id)
    except ValueError as e:
        msg = str(e)
        code = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=code, detail=msg) from e

    if result["was_active"]:
        set_active_proceeding(result["case_id"], None, db, user.id)
        db.commit()

    return Response(headers={"HX-Refresh": "true"})
