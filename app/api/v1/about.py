"""Settings → About: which build is running."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.dependencies import get_current_user, get_db
from app.models.database import User
from app.schemas.about import AboutView
from app.version import get_build_info

router = APIRouter(tags=["about"])


@router.get("/about", response_model=AboutView)
def about(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Build identity plus the applied database migration."""
    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        db.rollback()
        revision = None
    info = get_build_info()
    return AboutView(
        version=info.version,
        git_sha=info.git_sha,
        build_date=info.build_date,
        db_revision=revision,
    )
