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
        )  # naive UTC, consistent with Document.ingest_date
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


def get_active_proceeding(case_id: str, db) -> int | None:
    settings = db.query(UserSettings).first()
    if not settings:
        return None
    value = settings.settings_json.get("active_proceeding", {}).get(case_id)
    return int(value) if value is not None else None


def set_active_proceeding(case_id: str, proceeding_id: int, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    active = dict(data.get("active_proceeding", {}))
    active[case_id] = proceeding_id
    data["active_proceeding"] = active
    settings.settings_json = data
    db.flush()


def get_dashboard_view(db) -> str:
    settings = db.query(UserSettings).first()
    if not settings:
        return "graph"
    return settings.settings_json.get("dashboard_view", "graph")


def set_dashboard_view(view: str, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    data["dashboard_view"] = view
    settings.settings_json = data
    db.flush()


def get_last_home_visit(db) -> datetime | None:
    """Return the datetime the user last visited the home page."""
    settings = db.query(UserSettings).first()
    if not settings or not settings.settings_json:
        return None
    raw = settings.settings_json.get("last_home_visit")
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def mark_home_visit(db, *, now: datetime | None = None) -> None:
    """Record that the user just visited the home page."""
    if now is None:
        now = datetime.now()
    settings = _get_or_create(db)
    # Reassign entire dict to trigger SQLAlchemy JSON mutation detection
    current = dict(settings.settings_json or {})
    current["last_home_visit"] = now.isoformat()
    settings.settings_json = current
    db.flush()


def get_theme(db) -> str:
    settings = db.query(UserSettings).first()
    if not settings:
        return "dark"
    return settings.settings_json.get("theme", "dark")


def set_theme(theme: str, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    data["theme"] = theme
    settings.settings_json = data
    db.flush()


def get_dashboard_cards(db) -> dict:
    defaults = {"action_items": True, "costs": True, "documents": True}
    settings = db.query(UserSettings).first()
    if not settings:
        return defaults
    return settings.settings_json.get("dashboard_cards", defaults)


def set_dashboard_cards(cards: dict, db) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json)
    data["dashboard_cards"] = cards
    settings.settings_json = data
    db.flush()


def count_new_since(case_id: str, since: datetime | None, db) -> int:
    """Count documents added to the case after `since`. Returns 0 if since is None."""
    if since is None:
        return 0
    from app.models.database import Document

    return (
        db.query(Document)
        .filter(Document.case_id == case_id, Document.ingest_date > since)
        .count()
    )
