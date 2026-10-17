"""Settings → Data: workspace stats, danger-zone maintenance, export."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_current_admin, get_db
from app.models.database import Case, Claim, Document, LegalCost
from app.models.enums import AuditEventType
from app.schemas.settings import DataView, MaintenanceResult
from app.services import audit_service, maintenance_service
from app.services.export_service import build_export_zip

router = APIRouter(
    prefix="/settings/data",
    tags=["settings"],
    dependencies=[Depends(get_current_admin)],
)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


@router.get("", response_model=DataView)
def data(db: Session = Depends(get_db)):
    db_size = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
    return DataView(
        doc_count=db.query(Document).count(),
        case_count=db.query(Case).filter(Case.id != "_TRIAGE").count(),
        claim_count=db.query(Claim).count(),
        cost_count=db.query(LegalCost).count(),
        db_size_mb=round((db_size or 0) / 1024 / 1024, 2),
    )


@router.post("/reset-enrichment", response_model=MaintenanceResult)
@limiter.limit("5/minute")
def reset_enrichment(request: Request, db: Session = Depends(get_db)):
    docs, vectors = maintenance_service.reset_ai_enrichment(db)
    return MaintenanceResult(
        message=f"Reset {_plural(docs, 'document')}; {_plural(vectors, 'embedding')} cleared."
    )


@router.post("/clear-all-data", response_model=MaintenanceResult)
@limiter.limit("5/minute")
def clear_all_data(request: Request, db: Session = Depends(get_db)):
    rows, files = maintenance_service.clear_all_data(db)
    return MaintenanceResult(
        message=f"Cleared {_plural(rows, 'database row')}; {_plural(files, 'disk artifact')} removed."
    )


@router.get("/export", response_class=Response)
@limiter.limit("1/hour")
def export(request: Request, db: Session = Depends(get_db)):
    """Download the whole workspace as a zip (GDPR Art. 15/20)."""
    zip_bytes, manifest = build_export_zip(db)
    filename = f"sanctuary_export_{now_utc().date().isoformat()}.zip"
    audit_service.record(
        db,
        AuditEventType.DATA_EXPORTED,
        payload={
            "scope": "workspace",
            "table_counts": manifest["table_counts"],
            "bytes": len(zip_bytes),
            "files_included": manifest["files_included"],
            "undecryptable": manifest["undecryptable"],
        },
    )
    db.commit()
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
