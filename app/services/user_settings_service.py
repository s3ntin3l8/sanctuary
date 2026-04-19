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


def _get_or_create(db) -> UserSettings:
    settings = db.query(UserSettings).first()
    if not settings:
        settings = UserSettings(settings_json={})
        db.add(settings)
        db.flush()
    return settings


def get_active_proceeding(case_id: str, db) -> str | None:
    settings = _get_or_create(db)
    active = settings.settings_json.get("active_proceeding", {})
    return active.get(case_id)


def set_active_proceeding(case_id: str, proceeding_id: str, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    active = dict(data.get("active_proceeding", {}))
    active[case_id] = proceeding_id
    data["active_proceeding"] = active
    settings.settings_json = data
    db.flush()


def get_dashboard_default_view(db) -> str:
    settings = _get_or_create(db)
    return settings.settings_json.get("dashboard_default_view", "graph")


def set_dashboard_default_view(view: str, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    data["dashboard_default_view"] = view
    settings.settings_json = data
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
