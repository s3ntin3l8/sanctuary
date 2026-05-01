"""Shared helper for creating ActionItem rows from AI-extracted action payload."""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import ActionItem
from app.models.enums import ActionItemStatus, ActionItemType

logger = logging.getLogger(__name__)

VALID_ACTION_TYPES = {e.value for e in ActionItemType}


def create_from_payload(
    case_id: str,
    source_doc_id: int | None,
    proceeding_id: int | None,
    actions: list[dict],
    db: Session,
) -> int:
    """Parse an AI-extracted actions list and insert ActionItem rows.

    Deduplicates when source_doc_id is set: deletes existing ActionItems for
    that source doc before inserting, so re-running enrichment doesn't duplicate.

    Returns the count of rows created.
    """
    if not case_id:
        return 0

    if source_doc_id is not None:
        db.query(ActionItem).filter(
            ActionItem.source_document_id == source_doc_id
        ).delete(synchronize_session=False)

    count = 0
    for action in actions:
        raw_type = (action.get("action_type") or "deadline").lower()
        if raw_type not in VALID_ACTION_TYPES:
            raw_type = "deadline"
        due_str = action.get("due_date")
        try:
            due_date = datetime.strptime(due_str, "%Y-%m-%d") if due_str else None
        except ValueError:
            due_date = None
        if not due_date:
            continue

        db.add(
            ActionItem(
                case_id=case_id,
                proceeding_id=proceeding_id,
                source_document_id=source_doc_id,
                title=action.get("title", "Extracted action item")[:255],
                description=action.get("description"),
                due_date=due_date,
                action_type=ActionItemType(raw_type),
                status=ActionItemStatus.OPEN,
                ingest_date=datetime.now(),
            )
        )
        count += 1

    if count:
        logger.info(
            "Created %d ActionItem(s) for source_doc=%s case=%s",
            count,
            source_doc_id,
            case_id,
        )
    return count
