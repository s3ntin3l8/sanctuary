"""Data & Maintenance settings endpoints."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.dependencies import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/maintenance", tags=["settings"])


@router.post("/reset-enrichment", response_class=HTMLResponse)
def reset_ai_enrichment(db: Session = Depends(get_db)):
    vectors_cleared = db.execute(text("DELETE FROM document_vectors")).rowcount

    result = db.execute(
        text(
            "UPDATE documents SET "
            "ai_summary = NULL, ai_summary_created_at = NULL, "
            "significance_tier = NULL, key_passages = NULL "
            "WHERE 1=1"
        )
    )
    docs_reset = result.rowcount
    db.commit()

    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-primary)">'
        f"Reset {docs_reset} document{'' if docs_reset == 1 else 's'}; {vectors_cleared} embedding{'' if vectors_cleared == 1 else 's'} cleared."
        f"</span>"
    )
