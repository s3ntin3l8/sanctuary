from fastapi import APIRouter

from app.spa import spa_index

router = APIRouter(prefix="", tags=["pages"], include_in_schema=False)


@router.get("/")
def home():
    """Home is an SPA route (data comes from /api/v1/home)."""
    return spa_index()
