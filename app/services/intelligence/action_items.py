"""Shared helper for creating ActionItem rows from AI-extracted action payload."""

import logging
from datetime import UTC, datetime, timedelta

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
    *,
    source_doc_date: datetime | None = None,
) -> int:
    """Parse an AI-extracted actions list and insert ActionItem rows.

    Deduplicates when source_doc_id is set: deletes existing ActionItems for
    that source doc before inserting, so re-running enrichment doesn't duplicate.

    Also deduplicates across documents: skips any item whose (due_date, action_type)
    already exists for the case from another source document. This prevents the
    letter + Verfügung pattern (German Ladungen) from producing two identical
    court-date action items.

    When source_doc_date is provided, drops actions whose due_date is more than
    one day before source_doc_date — guards against the AI extracting past
    hearing dates from Sitzungsprotokolle / Terminsprotokolle as if they were
    upcoming deadlines.

    Returns the count of rows created.
    """
    if not case_id:
        return 0

    if source_doc_id is not None:
        db.query(ActionItem).filter(
            ActionItem.source_document_id == source_doc_id
        ).delete(synchronize_session=False)

    # Compare on `.date()` — `due_date` from strptime is naive, but
    # `doc.issued_date` arrives as tz-aware UTC in production; mixing the two
    # raises TypeError. We only have YYYY-MM-DD precision either way.
    cutoff_date = (
        (source_doc_date - timedelta(days=1)).date()
        if source_doc_date is not None
        else None
    )

    # Load existing (due_date, action_type) pairs for this case so we can skip
    # cross-document duplicates (e.g. cover letter + Verfügung both extracting
    # the same court date).
    existing_keys: set[tuple] = {
        (row.due_date.date(), row.action_type.value)
        for row in db.query(ActionItem)
        .filter(
            ActionItem.case_id == case_id,
            ActionItem.due_date.isnot(None),
        )
        .all()
    }

    count = 0
    for action in actions:
        raw_type = (action.get("action_type") or "deadline").lower()
        if raw_type not in VALID_ACTION_TYPES:
            raw_type = "deadline"
        due_str = action.get("due_date")
        # Truncate to YYYY-MM-DD — the batch analyzer may return full ISO
        # datetimes like "2025-09-22T10:00:00+02:00".
        try:
            due_date = datetime.strptime(due_str[:10], "%Y-%m-%d") if due_str else None
        except ValueError:
            due_date = None
        if not due_date:
            continue

        if cutoff_date is not None and due_date.date() < cutoff_date:
            continue

        # Remove any action item the AI says this date supersedes (Umladung /
        # Terminsverlegung pattern: the old date is now void).
        supersedes_str = action.get("supersedes_date")
        try:
            supersedes_date = (
                datetime.strptime(supersedes_str[:10], "%Y-%m-%d")
                if supersedes_str
                else None
            )
        except (ValueError, TypeError):
            supersedes_date = None
        if supersedes_date:
            # Match by date alone — the AI may classify the same real-world
            # event with different action_types across documents (a hearing
            # showing up as "court_date" in the rescheduling notice but
            # "deadline" in the original Ladung). The supersedes contract is
            # "the old date is void," so any action on that date for this
            # case is invalidated.
            deleted = (
                db.query(ActionItem)
                .filter(
                    ActionItem.case_id == case_id,
                    ActionItem.due_date == supersedes_date,
                )
                .delete(synchronize_session=False)
            )
            if deleted:
                logger.info(
                    "ActionItem: removed %d superseded item(s) for case=%s "
                    "date=%s (replaced by %s)",
                    deleted,
                    case_id,
                    supersedes_date.date(),
                    due_date.date(),
                )
            existing_keys = {k for k in existing_keys if k[0] != supersedes_date.date()}

        key = (due_date.date(), raw_type)
        if key in existing_keys:
            continue
        existing_keys.add(key)

        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(ActionItem.__table__)
            .values(
                case_id=case_id,
                proceeding_id=proceeding_id,
                source_document_id=source_doc_id,
                title=action.get("title", "Extracted action item")[:255],
                description=action.get("description"),
                due_date=due_date,
                action_type=ActionItemType(raw_type),
                status=ActionItemStatus.OPEN,
                ingest_date=datetime.now(UTC),
            )
            .on_conflict_do_nothing(
                index_elements=["case_id", "due_date", "action_type"]
            )
        )
        if db.execute(stmt).rowcount > 0:
            count += 1

    if count:
        logger.info(
            "Created %d ActionItem(s) for source_doc=%s case=%s",
            count,
            source_doc_id,
            case_id,
        )
    return count
