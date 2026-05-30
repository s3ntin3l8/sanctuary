from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.constants import CASE_STATUS_META, ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_current_user, get_db
from app.helpers import (
    format_deadline_badge,
    format_relative_time,
    format_upcoming_datetime,
    render_page,
)
from app.models.database import User
from app.services.home_service import HomeService
from app.services.user_settings_service import mark_home_visit

router = APIRouter(prefix="", tags=["pages"])


@router.get("/")
async def home(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The proactive home page dashboard."""
    home_service = HomeService(db)
    data = home_service.get_home_data(user.id)

    # Mark the visit (last_home_visit is updated)
    # Actually, as per spec: "opening Home passively doesn't "count" as reviewing — only explicit actions advance the timestamp."
    # So we don't mark_home_visit here, only when users click "review all".

    return render_page(
        request,
        "pages/home.html",
        db=db,
        **data,
        originator_colors=ORIGINATOR_COLORS,
        originator_icons=ORIGINATOR_ICONS,
        status_meta=CASE_STATUS_META,
        format_relative_time=format_relative_time,
        format_upcoming_datetime=format_upcoming_datetime,
        format_deadline_badge=format_deadline_badge,
    )


@router.post("/home/review-all")
async def review_all_delta(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Mark all delta as reviewed by updating last_home_visit timestamp."""
    mark_home_visit(db, user.id)
    return {"status": "ok"}
