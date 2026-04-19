from __future__ import annotations

from datetime import UTC, datetime

from app.models.database import UserSettings


def get_last_viewed(case_id: str, db) -> datetime | None:
    """Return the datetime the current user last viewed this case, or None."""
    settings = db.query(UserSettings).first()
    if not settings or not settings.settings_json:
        return None
    last_viewed = settings.settings_json.get("last_viewed_cases", {})
    raw = last_viewed.get(case_id)
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def mark_viewed(case_id: str, db, *, now: datetime | None = None) -> None:
    """Record that the user just viewed this case."""
    if now is None:
        now = datetime.now(UTC).replace(
            tzinfo=None
        )  # naive UTC, consistent with Document.created_at
    settings = db.query(UserSettings).first()
    if not settings:
        return
    # Reassign entire dict to trigger SQLAlchemy JSON mutation detection
    current = dict(settings.settings_json or {})
    last_viewed = dict(current.get("last_viewed_cases", {}))
    last_viewed[case_id] = now.isoformat()
    current["last_viewed_cases"] = last_viewed
    settings.settings_json = current
    db.flush()


def count_new_since(case_id: str, since: datetime | None, db) -> int:
    """Count documents added to the case after `since`. Returns 0 if since is None."""
    if since is None:
        return 0
    from app.models.database import Document

    return (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.created_at > since)
        .count()
    )
