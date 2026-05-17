"""GDPR data export endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_db
from app.models.enums import AuditEventType
from app.services import audit_service
from app.services.export_service import build_export_zip

router = APIRouter()


@router.get("/api/export")
@limiter.limit("1/hour")
def export_data(request: Request, db: Session = Depends(get_db)):
    """Stream a zip archive containing all user data."""
    zip_bytes, manifest = build_export_zip(db)
    filename = f"sanctuary_export_{now_utc().date().isoformat()}.zip"
    audit_service.record(
        db,
        AuditEventType.DATA_EXPORTED,
        payload={
            "table_counts": manifest["table_counts"],
            "bytes": len(zip_bytes),
            "files_included": manifest["files_included"],
        },
    )
    db.commit()
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
