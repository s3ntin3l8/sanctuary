from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.dependencies import get_db
from app.models.database import Case

router = APIRouter(prefix="/timeline", tags=["api"])


@router.get("")
async def timeline_api(
    request: Request,
    cursor: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
):
    """API endpoint for timeline pagination (returns HTML partial)."""
    from app.services.document_service import DocumentService

    doc_service = DocumentService(db)
    docs, has_more = doc_service.get_documents_paginated(cursor=cursor, limit=limit + 1)

    case_titles = {c.id: c.title for c in db.query(Case.id, Case.title).all()}

    next_cursor = docs[-1].id if docs and has_more else None

    html_parts = []
    for doc in docs:
        stripe_color = ORIGINATOR_COLORS.get(doc.originator_type, "#64748b")
        stripe_icon = ORIGINATOR_ICONS.get(doc.originator_type, "help_outline")

        html_parts.append(f'<div class="timeline-doc-item" data-doc-id="{doc.id}">')
        html_parts.append('<div class="relative mb-6 group">')
        html_parts.append(
            f'<div class="absolute left-[-25px] top-5 z-10"><div class="w-[14px] h-[14px] rounded-full border-2 border-white shadow-sm" style="background-color: {stripe_color};"></div></div>'
        )
        html_parts.append(
            f'<div class="rounded-xl border shadow-sm overflow-hidden bg-surface-container-lowest border-outline-variant/10" style="border-left: 4px solid {stripe_color};">'
        )
        html_parts.append('<div class="p-5">')
        html_parts.append('<div class="flex items-center justify-between mb-2">')
        html_parts.append(
            f'<div class="flex items-center gap-2"><span class="material-symbols-outlined text-sm" style="color: {stripe_color};">{stripe_icon}</span><span class="font-mono text-[10px] text-on-surface-variant">{doc.created_at.strftime("%Y-%m-%d %H:%M")}</span></div>'
        )
        if doc.case_id:
            html_parts.append(
                f'<a href="/cases/{doc.case_id}" class="flex items-center gap-1 bg-surface-container-high text-on-surface-variant text-[9px] font-bold px-2.5 py-1 rounded-full uppercase tracking-wider">{case_titles.get(doc.case_id, doc.case_id)}</a>'
            )
        else:
            html_parts.append(
                '<span class="bg-amber-100 text-amber-700 text-[9px] font-bold px-2.5 py-1 rounded-full uppercase tracking-wider">Unlinked</span>'
            )
        html_parts.append("</div>")
        html_parts.append(
            f'<h3 class="text-sm font-bold text-on-surface mb-1 line-clamp-1 group-hover:text-primary">{doc.title}</h3>'
        )
        content_preview = (doc.content or "No content available")[:200]
        html_parts.append(
            f'<p class="text-xs text-on-surface-variant leading-relaxed line-clamp-2 mb-3">{content_preview}</p>'
        )
        html_parts.append(
            '<div class="flex items-center justify-between pt-3 border-t border-outline-variant/10">'
        )
        if doc.sender:
            html_parts.append(
                f'<p class="text-[9px] text-on-surface-variant font-medium truncate max-w-[70%]">Via: Email from {doc.sender}</p>'
            )
        else:
            html_parts.append(
                f'<p class="text-[9px] text-on-surface-variant font-medium truncate max-w-[70%]">Document ID: {doc.id}</p>'
            )
        if doc.needs_review:
            html_parts.append(
                '<span class="text-[9px] bg-amber-100 text-amber-700 px-2 py-0.5 rounded-full font-bold uppercase tracking-tighter">Needs Review</span>'
            )
        else:
            html_parts.append(
                '<span class="text-[9px] bg-primary-container/20 text-primary px-2 py-0.5 rounded-full font-bold uppercase tracking-tighter">Reviewed</span>'
            )
        html_parts.append("</div></div></div></div></div>")

    return {
        "html": "".join(html_parts),
        "next_cursor": next_cursor,
        "has_more": has_more,
    }
