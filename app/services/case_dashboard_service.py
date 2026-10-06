"""Shared helpers for the case dashboard API, the document HUD and chat:
per-proceeding reaction glyphs, summary bullets, normalised key passages,
originator colours and proceeding-neighbour navigation."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.database import (
    Document,
    UserReaction,
)

# Display order for party roles in the sidebar: own first, unknown last.
_PARTY_ROLE_ORDER: dict[str, int] = {
    "own": 0,
    "court": 1,
    "opposing": 2,
    "third_party": 3,
    "unknown": 4,
}


def reaction_map_for_proceeding(db: Session, proceeding_id: int) -> dict[int, str]:
    """Return {doc_id: reaction_emoji} for docs in a proceeding.

    Pairs each document with its most-recent user reaction (if any). The
    graph only needs a single glyph per node, so we collapse multiple
    reactions down to the latest.
    """
    from sqlalchemy import and_

    # Subquery: latest reaction ingest_date per doc, scoped to the
    # proceeding's documents. SQLite has no DISTINCT ON, so we GROUP BY
    # doc_id and pick the row whose ingest_date matches the max — cheaper
    # than fetching every reaction and discarding 80% in Python.
    latest = (
        db.query(
            UserReaction.document_id.label("doc_id"),
            func.max(UserReaction.ingest_date).label("ts"),
        )
        .join(Document, UserReaction.document_id == Document.id)
        .filter(Document.proceeding_id == proceeding_id)
        .group_by(UserReaction.document_id)
        .subquery()
    )

    rows = (
        db.query(UserReaction)
        .join(
            latest,
            and_(
                UserReaction.document_id == latest.c.doc_id,
                UserReaction.ingest_date == latest.c.ts,
            ),
        )
        .all()
    )

    out: dict[int, str] = {}
    for r in rows:
        val = r.reaction
        out[r.document_id] = val.value if hasattr(val, "value") else str(val)
    return out


# ---------------------------------------------------------------------------
# Helpers shared by the HUD, chat and case dashboard API
# ---------------------------------------------------------------------------


def summary_bullets_from_ai_summary(ai_summary) -> list[dict]:
    """Coerce the various `Document.ai_summary` shapes into the HUD template's
    `[{kind, text}]` format.

    Accepted shapes:
    * dict with keys ``legal_significance`` / ``required_action`` /
      ``financial_impact`` (the canonical Management Summary structure).
    * already-formatted list of ``{kind, text}`` dicts — passed through.
    * plain string — wrapped as a single ``{kind: "legal", text}`` bullet.
    """
    if not ai_summary:
        return []
    if isinstance(ai_summary, list):
        return [b for b in ai_summary if isinstance(b, dict) and b.get("text")]
    if isinstance(ai_summary, str):
        return [{"kind": "legal", "text": ai_summary}]
    if isinstance(ai_summary, dict):
        mapping = [
            ("legal", ai_summary.get("legal_significance")),
            ("action", ai_summary.get("required_action")),
            ("finance", ai_summary.get("financial_impact")),
        ]
        return [{"kind": kind, "text": text} for kind, text in mapping if text]
    return []


def key_passages_for_template(key_passages) -> list[dict]:
    """Normalise `Document.key_passages` to `[{text, kind, page, rationale, id}]`."""
    import hashlib

    if not key_passages:
        return []
    out: list[dict] = []
    for raw in key_passages:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text") or ""
        if not text:
            continue
        kind = (raw.get("kind") or "neutral").lower()
        page = raw.get("page")
        if page is None:
            span = raw.get("span") or {}
            if isinstance(span, dict):
                page = span.get("page")
        pid = raw.get("id") or hashlib.sha1(f"{text}|{kind}".encode()).hexdigest()[:12]
        out.append(
            {
                "text": text,
                "kind": kind,
                "page": page,
                "rationale": raw.get("rationale") or "",
                "id": pid,
                "start_offset": raw.get("start_offset"),
                "end_offset": raw.get("end_offset"),
            }
        )
    return out


def originator_color_for_doc(doc) -> str:
    """Return the originator-color key (own/court/opposing/third) for a doc."""
    from app.services.case_graph_service import _lane_for

    return _lane_for(doc)


def neighbor_doc_ids(db: Session, doc) -> tuple[int | None, int | None, int, int]:
    """Return (prev_doc_id, next_doc_id, position, total) within the same proceeding.

    position is 1-indexed; both position and total are 0 when there is no proceeding.
    """
    if doc.proceeding_id is None:
        return None, None, 0, 0
    siblings = (
        db.query(Document.id)
        .filter(Document.proceeding_id == doc.proceeding_id)
        .order_by(
            Document.issued_date.asc().nullslast(),
            Document.id.asc(),
        )
        .all()
    )
    ids = [row[0] for row in siblings]
    try:
        idx = ids.index(doc.id)
    except ValueError:
        return None, None, 0, 0
    prev_id = ids[idx - 1] if idx > 0 else None
    next_id = ids[idx + 1] if idx < len(ids) - 1 else None
    return prev_id, next_id, idx + 1, len(ids)


# Re-export so callers can reach these without extra imports
__all__ = [
    "reaction_map_for_proceeding",
    "summary_bullets_from_ai_summary",
    "key_passages_for_template",
    "originator_color_for_doc",
    "neighbor_doc_ids",
]
