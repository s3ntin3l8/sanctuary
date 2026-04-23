"""AI & Models settings endpoints."""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.ai_provider import ai_provider
from app.services.embeddings import reindex_all_docs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/ai", tags=["settings"])


@router.post("/test", response_class=HTMLResponse)
async def test_ai_connection():
    result = await ai_provider.probe_health()
    color = "var(--color-primary)" if result["ok"] else "var(--color-error)"
    symbol = "check_circle" if result["ok"] else "error"
    return HTMLResponse(
        f'<span class="inline-flex items-center gap-1.5 text-xs">'
        f'<span class="material-symbols-outlined text-[14px]" style="color:{color}">{symbol}</span>'
        f'<span style="color:{color}">{result["detail"]}</span>'
        f"</span>"
    )


@router.post("/reindex", response_class=HTMLResponse)
async def reindex_documents(db: Session = Depends(get_db)):
    result = await reindex_all_docs(db)
    fail_note = f" ({result['failed']} failed)" if result["failed"] else ""
    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-on-surface-variant)">'
        f"Reindexed {result['reindexed']}/{result['total']} documents{fail_note}"
        f"</span>"
    )
