"""Shared helper for creating ActionItem rows from AI-extracted action payload."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.database import ActionItem
from app.models.enums import ActionItemStatus, ActionItemType

logger = logging.getLogger(__name__)

VALID_ACTION_TYPES = {e.value for e in ActionItemType}


def purge_action_items_from_doc(source_doc_id: int, db: Session) -> int:
    """Delete non-superseded ActionItems sourced from this document.

    Mirrors the Streitwert-erase pattern: when the action-item court gate
    rejects a re-enrichment (originator flipped court→party, or the doc was
    never a court source to begin with), prior auto-extracted items are
    stale and must vanish. Tombstones (superseded=True) are preserved —
    they're permanent guards against later re-insertion of stale dates.
    """
    return (
        db.query(ActionItem)
        .filter(
            ActionItem.source_document_id == source_doc_id,
            ActionItem.superseded.is_(False),
        )
        .delete(synchronize_session=False)
    )


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

    Supersession (Terminsverlegung / Umladung): when an action carries
    supersedes_date, existing items at that date are marked DISMISSED +
    superseded=True (tombstoned) instead of being deleted. The tombstone blocks
    any later doc from re-inserting the stale date, regardless of processing
    order. This fixes the race where an older scheduling notice enriches after
    the rescheduling notice has already established the supersession.

    Returns the count of rows created.
    """
    if not case_id:
        return 0

    if source_doc_id is not None:
        db.query(ActionItem).filter(
            ActionItem.source_document_id == source_doc_id,
            ActionItem.superseded.is_(
                False
            ),  # tombstones are permanent guards — never erase
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
            # Tombstone any item at the superseded date: mark DISMISSED +
            # superseded=True. Match by date alone — the AI may classify the
            # same real-world hearing with different action_types across docs
            # (a hearing showing up as "court_date" in the rescheduling notice
            # but "deadline" in the original Ladung). The tombstone persists so
            # a later-processing older doc can't re-insert the stale date.
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            updated = (
                db.query(ActionItem)
                .filter(
                    ActionItem.case_id == case_id,
                    ActionItem.due_date == supersedes_date,
                )
                .update(
                    {"status": ActionItemStatus.DISMISSED, "superseded": True},
                    synchronize_session=False,
                )
            )
            if updated:
                logger.info(
                    "ActionItem: tombstoned %d item(s) for case=%s "
                    "date=%s (superseded by %s)",
                    updated,
                    case_id,
                    supersedes_date.date(),
                    due_date.date(),
                )
            else:
                # No existing item at the superseded date — insert a sentinel
                # tombstone row so the guard fires even when an older doc
                # enriches later (reverse-processing-order case). on_conflict
                # is a no-op if a tombstone with the same key already exists.
                sentinel_stmt = (
                    sqlite_insert(ActionItem.__table__)
                    .values(
                        case_id=case_id,
                        source_document_id=None,
                        title=f"[Superseded {supersedes_date.date()}]",
                        due_date=supersedes_date,
                        action_type=ActionItemType.DEADLINE,
                        status=ActionItemStatus.DISMISSED,
                        superseded=True,
                        ingest_date=datetime.now(UTC),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["case_id", "due_date", "action_type"]
                    )
                )
                db.execute(sentinel_stmt)
                logger.info(
                    "ActionItem: inserted sentinel tombstone for case=%s "
                    "date=%s (superseded by %s — no existing item)",
                    case_id,
                    supersedes_date.date(),
                    due_date.date(),
                )
            existing_keys = {k for k in existing_keys if k[0] != supersedes_date.date()}

        key = (due_date.date(), raw_type)
        if key in existing_keys:
            continue

        # Tombstone guard: if any item at this date was previously superseded,
        # a rescheduling notice established it as void — don't re-insert.
        if (
            db.query(ActionItem)
            .filter(
                ActionItem.case_id == case_id,
                ActionItem.due_date == due_date,
                ActionItem.superseded.is_(True),
            )
            .first()
        ):
            logger.debug(
                "ActionItem: skipping %s for case=%s — tombstone exists for date=%s",
                raw_type,
                case_id,
                due_date.date(),
            )
            continue

        existing_keys.add(key)

        # Reject placeholder descriptions the AI occasionally emits.
        desc = action.get("description")
        _PLACEHOLDER_DESCS = {"...", "…", "TBD", "N/A", "n/a"}
        if desc and desc.strip() in _PLACEHOLDER_DESCS:
            desc = None

        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(ActionItem.__table__)
            .values(
                case_id=case_id,
                proceeding_id=proceeding_id,
                source_document_id=source_doc_id,
                title=action.get("title", "Extracted action item")[:255],
                description=desc,
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
