"""Ingest tools: upload a file, and file a triage item into a case.

Both need a read-write token. ``upload_document`` is the programmatic twin of
``POST /api/v1/upload`` for one file; ``assign_to_case`` moves the owner's own
triage items into a case. File contents are never logged or audited.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os

from fastapi import HTTPException
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import config
from app.core.async_utils import run_async
from app.mcp.db import run
from app.mcp.principal import NOT_FOUND, READ_ONLY, McpPrincipal
from app.models.database import Document, IngestBatch, Proceeding
from app.models.enums import AuditEventType
from app.services import audit_service, triage_routing_service
from app.services.ingestion.converters import ALLOWED_EXTENSIONS

_TRIAGE = "_TRIAGE"


class UploadResult(BaseModel):
    document_id: int
    ingest_batch_id: int
    case_id: str | None
    status: str
    url: str
    hint: str


class AssignResult(BaseModel):
    case_id: str
    proceeding_id: int | None
    document_ids: list[int]


def _require_write(p: McpPrincipal) -> None:
    if not p.can_write:
        raise ToolError(READ_ONLY)


def upload_document(
    db: Session,
    p: McpPrincipal,
    filename: str,
    content_base64: str,
    case_id: str | None = None,
    proceeding_id: int | None = None,
) -> UploadResult:
    from app.services.ingestion.service import upload_bytes_to_case

    _require_write(p)
    name = os.path.basename((filename or "").strip())
    ext = os.path.splitext(name)[1].lower()
    if not name:
        raise ToolError("filename must not be empty.")
    if ext == ".eml":
        raise ToolError("Emails are imported through Gmail import, not uploaded here.")
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS - {".eml"}))
        raise ToolError(f"Unsupported file type '{ext}'. Allowed: {allowed}.")

    max_bytes = config.MCP_MAX_UPLOAD_MB * 1024 * 1024
    # base64 expands by 4/3: reject an obviously oversized payload before decoding.
    if len(content_base64) > max_bytes * 4 // 3 + 4:
        raise ToolError(f"File too large. Maximum size: {config.MCP_MAX_UPLOAD_MB} MB.")
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError("content_base64 is not valid base64.") from exc
    if not content:
        raise ToolError("The file is empty.")
    if len(content) > max_bytes:
        raise ToolError(f"File too large. Maximum size: {config.MCP_MAX_UPLOAD_MB} MB.")

    if case_id is None:
        if not p.all_cases:
            raise ToolError("case_id is required for a token limited to certain cases.")
        if proceeding_id is not None:
            raise ToolError("proceeding_id needs a case_id.")
        target: str | None = None
    else:
        target = p.require_case(db, case_id, write=True).id
        if proceeding_id is not None:
            try:
                triage_routing_service.check_proceeding(db, proceeding_id, target)
            except triage_routing_service.RoutingError as exc:
                raise ToolError(exc.detail) from exc

    try:
        doc, batch_id = run_async(
            upload_bytes_to_case(
                db,
                filename=name,
                content=content,
                owner_id=p.user.id,
                case_id=target,
                agent_token_id=p.token_id,
            )
        )
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc
    if proceeding_id is not None:
        doc.proceeding_id = proceeding_id
        # Filing into a draft proceeding makes it real, as route_document does.
        proc = db.get(Proceeding, proceeding_id)
        if proc and proc.is_draft:
            proc.is_draft = False
    audit_service.record(
        db,
        AuditEventType.MCP_UPLOAD,
        target_type="document",
        target_id=str(doc.id),
        payload={
            "tool": "upload_document",
            "api_token_id": p.token_id,
            "filename": name,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "case_id": target,
        },
        actor_user_id=p.user.id,
        actor_label=p.audit_label,
    )
    return UploadResult(
        document_id=doc.id,
        ingest_batch_id=batch_id,
        case_id=target,
        status="queued",
        url=f"/document/{doc.id}",
        hint="Processing runs in the background; poll get_document for its status.",
    )


def assign_to_case(
    db: Session,
    p: McpPrincipal,
    case_id: str,
    document_id: int | None = None,
    batch_id: int | None = None,
    proceeding_id: int | None = None,
    finalize: bool = False,
) -> AssignResult:
    _require_write(p)
    if not p.all_cases:
        raise ToolError("Only an all-cases token can file items from the triage inbox.")
    if (document_id is None) == (batch_id is None):
        raise ToolError("Give exactly one of document_id or batch_id.")
    case = p.require_case(db, case_id, write=True)

    if batch_id is not None:
        batch = db.get(IngestBatch, batch_id)
        if batch is None or batch.owner_id != p.user.id:
            raise ToolError(NOT_FOUND)
        in_triage = [
            d.id
            for d in db.query(Document).filter(
                Document.ingest_batch_id == batch_id, Document.case_id == _TRIAGE
            )
        ]
        if not in_triage:
            raise ToolError("That bundle is not in the triage inbox.")
    else:
        doc = db.get(Document, document_id) if document_id is not None else None
        if doc is None or doc.owner_id != p.user.id:
            raise ToolError(NOT_FOUND)
        if doc.case_id not in (None, _TRIAGE):
            raise ToolError("That document is already filed in a case.")
        in_triage = [doc.id]

    try:
        triage_routing_service.check_proceeding(db, proceeding_id, case.id)
        if batch_id is not None:
            triage_routing_service.route_batch(
                db, batch_id, case.id, proceeding_id, finalize
            )
        else:
            assert document_id is not None  # exactly one of the two ids is set
            triage_routing_service.route_document(
                db, document_id, case.id, proceeding_id, finalize
            )
    except triage_routing_service.RoutingError as exc:
        raise ToolError(exc.detail) from exc
    audit_service.record(
        db,
        AuditEventType.MCP_WRITE,
        target_type="batch" if batch_id is not None else "document",
        target_id=str(batch_id if batch_id is not None else document_id),
        payload={
            "tool": "assign_to_case",
            "api_token_id": p.token_id,
            "case_id": case.id,
            "proceeding_id": proceeding_id,
            "document_ids": in_triage,
        },
        actor_user_id=p.user.id,
        actor_label=p.audit_label,
    )
    return AssignResult(
        case_id=case.id, proceeding_id=proceeding_id, document_ids=in_triage
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="upload_document")
    async def _upload_document(
        ctx: Context,
        filename: str,
        content_base64: str,
        case_id: str | None = None,
        proceeding_id: int | None = None,
    ) -> UploadResult:
        """Upload one file (pdf, docx, txt, md, pptx, xlsx) into Sanctuary.

        content_base64 is the file's bytes, base64-encoded; size is capped (25 MB
        by default). A token limited to certain cases must name a case_id it can
        edit; an all-cases token may omit it and the file lands in the triage
        inbox. Processing (OCR, AI analysis) runs in the background: poll
        get_document with the returned document_id for its status. Needs a
        read-write token.
        """
        return await run(
            ctx,
            lambda db, p: upload_document(
                db, p, filename, content_base64, case_id, proceeding_id
            ),
        )

    @mcp.tool(name="assign_to_case")
    async def _assign_to_case(
        ctx: Context,
        case_id: str,
        document_id: int | None = None,
        batch_id: int | None = None,
        proceeding_id: int | None = None,
        finalize: bool = False,
    ) -> AssignResult:
        """File one of your triage-inbox items into an existing case.

        Give exactly one of document_id (a loose document) or batch_id (an
        email/scan bundle), both from list_triage_inbox. proceeding_id optionally
        picks a court level of the case; finalize confirms the item as reviewed.
        Needs a read-write, all-cases token; it cannot create a case.
        """
        return await run(
            ctx,
            lambda db, p: assign_to_case(
                db, p, case_id, document_id, batch_id, proceeding_id, finalize
            ),
        )
