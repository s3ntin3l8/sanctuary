from fastapi import APIRouter, Depends, Form, HTTPException, Response
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.models.enums import ProceedingCourtLevel, ProceedingStatus
from app.repositories.proceeding import ProceedingRepository

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
