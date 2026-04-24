"""Assemble prompt context strings for document-chat and case-chat."""

from sqlalchemy.orm import Session

from app.models.database import Case, ConversationMessage, Document
from app.services.intelligence.reaction_context import (
    format_reactions_for_case,
    format_reactions_for_document,
)

MAX_HISTORY_TURNS = 20


def build_document_chat_prompt(
    doc: Document,
    db: Session,
    history: list[ConversationMessage],
    user_message: str,
) -> str:
    passages = doc.key_passages or []
    passages_block = ""
    if passages:
        lines = [
            f"  [{i + 1}] {p.get('text', '')}" for i, p in enumerate(passages[:10])
        ]
        passages_block = "Key passages:\n" + "\n".join(lines)

    content_preview = (doc.content or "")[:6000]
    reactions_block = format_reactions_for_document(db, doc.id)

    context = f"""Document: [{doc.id}] {doc.title}
Case: {doc.case_id or "unassigned"}
Issued: {doc.issued_date.strftime("%d.%m.%Y") if doc.issued_date else "unknown"}
Significance: {doc.significance_tier.value if doc.significance_tier else "unset"}

{passages_block}

Document content (first 6000 chars):
{content_preview}
"""
    if reactions_block:
        context += f"\n{reactions_block}\n"

    return _format_with_history(context, history, user_message)


def build_case_chat_prompt(
    case: Case,
    db: Session,
    history: list[ConversationMessage],
    user_message: str,
    retrieved_hits: list,
) -> str:
    brief = case.ai_brief or {}
    brief_block = ""
    if isinstance(brief, dict) and brief.get("status") != "processing":
        parts = []
        if brief.get("posture"):
            parts.append(f"Posture: {brief['posture']}")
        if brief.get("pressure_points"):
            parts.append(
                "Pressure points:\n"
                + "\n".join(f"  - {p}" for p in brief["pressure_points"])
            )
        if brief.get("next_move"):
            parts.append(f"Next move: {brief['next_move']}")
        brief_block = "\n".join(parts)

    retrieved_block = ""
    if retrieved_hits:
        sections = []
        for hit in retrieved_hits:
            passages = "\n".join(
                f"    [{i + 1}] {p.get('text', '')}"
                for i, p in enumerate(hit.key_passages[:5])
            )
            sections.append(
                f"[DOC:{hit.doc_id}] {hit.title} ({hit.significance_tier or 'unknown tier'})\n"
                + (
                    f"  Key passages:\n{passages}"
                    if passages
                    else "  (no key passages)"
                )
            )
        retrieved_block = "Retrieved documents:\n" + "\n\n".join(sections)

    reactions_block = format_reactions_for_case(db, case.id)

    context = f"""Case: {case.title} ({case.id}) — Status: {case.status.value}
Cost exposure: {case.total_cost_exposure or 0} cents

Case AI Brief:
{brief_block or "(not yet generated)"}

{retrieved_block}
"""
    if reactions_block:
        context += f"\n{reactions_block}\n"

    return _format_with_history(context, history, user_message)


def _format_with_history(
    context: str, history: list[ConversationMessage], user_message: str
) -> str:
    """Build the full prompt: context block + last N turns + new user message."""
    recent = history[-MAX_HISTORY_TURNS * 2 :]
    history_lines = []
    for msg in recent:
        role = "User" if msg.role == "user" else "Assistant"
        history_lines.append(f"[{role}]: {msg.content}")

    history_block = "\n".join(history_lines)

    parts = [f"=== CONTEXT ===\n{context}"]
    if history_block:
        parts.append(f"=== CONVERSATION HISTORY ===\n{history_block}")
    parts.append(f"=== CURRENT QUESTION ===\n{user_message}")

    return "\n\n".join(parts)
