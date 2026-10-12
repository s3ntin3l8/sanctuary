"""Read tool: the stored original file of a document."""

from __future__ import annotations

import base64
import hashlib

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    TextContent,
)
from sqlalchemy.orm import Session

from app import config
from app.mcp.db import run
from app.mcp.principal import McpPrincipal
from app.mcp.serializers import document_ref
from app.models.enums import AuditEventType
from app.services import audit_service, document_files

_MB = 1024 * 1024


def get_original_file(db: Session, p: McpPrincipal, document_id: int) -> CallToolResult:
    doc = p.require_document(db, document_id)
    ref = document_ref(doc)
    try:
        original = document_files.resolve_original(doc)
    except document_files.OriginalFileError as exc:
        raise ToolError(exc.detail) from exc
    size = original.path.stat().st_size
    limit_mb = config.MCP_MAX_DOWNLOAD_MB
    if size > limit_mb * _MB:
        raise ToolError(
            f"The original is {size / _MB:.1f} MB, above the {limit_mb} MB limit. "
            f"Use read_document_text, or open it in Sanctuary at {ref.url}."
        )
    data = original.path.read_bytes()
    audit_service.record(
        db,
        AuditEventType.DATA_EXPORTED,
        target_type="document",
        target_id=str(doc.id),
        payload={
            "tool": "get_original_file",
            "api_token_id": p.token_id,
            "size_bytes": len(data),
            "filename": original.filename,
        },
        actor_user_id=p.user.id,
        actor_label=p.audit_label,
    )
    sha = hashlib.sha256(data).hexdigest()
    return CallToolResult(
        content=[
            TextContent(
                text=(
                    f"{original.filename} ({original.media_type}, {len(data)} bytes, "
                    f"sha256 {sha}) — document {ref.document_id}: {ref.title}"
                )
            ),
            EmbeddedResource(
                resource=BlobResourceContents(
                    uri=f"sanctuary://document/{doc.id}/original",
                    mime_type=original.media_type,
                    blob=base64.b64encode(data).decode(),
                )
            ),
        ]
    )


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="get_original_file")
    async def _get_original_file(ctx: Context, document_id: int) -> CallToolResult:
        """The document's stored original file (PDF, scan, image …) as an embedded
        resource. Prefer read_document_text; fetch the original only when layout,
        handwriting, stamps or signatures matter. Files above the size limit are
        refused. Every download is audited because the bytes leave this instance."""
        return await run(ctx, lambda db, p: get_original_file(db, p, document_id))
