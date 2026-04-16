from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.helpers import render_page

router = APIRouter(prefix="/entities", tags=["pages"])


@router.get("")
async def entities_page(request: Request, db: Session = Depends(get_db)):
    from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
    from app.models.database import Entity

    entities = db.query(Entity).order_by(Entity.created_at.desc()).all()

    grouped = {}
    for entity in entities:
        if entity.type not in grouped:
            grouped[entity.type] = {}
        name = entity.name
        if name in grouped[entity.type]:
            grouped[entity.type][name] += 1
        else:
            grouped[entity.type][name] = 1

    return render_page(
        request,
        "pages/entities.html",
        db=db,
        entities=entities,
        grouped_entities=grouped,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
    )
