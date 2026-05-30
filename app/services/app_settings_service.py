"""Global (non-per-user) application settings.

A single ``AppSettings`` row holds configuration shared by every user and read
by background workers that have no request/user context: AI instance config,
extraction/ingestion engine, reindex/dedup job state, global party identity,
display timezone.

NOTE (revisit when sharing lands — Phase 3): ``party_identity`` (own_self /
own_parties) is conceptually per-owner, but lives here for Phase 1 because the
single admin owns everything and AI workers need it without a user.
"""

from __future__ import annotations

from app.models.database import AppSettings


def _get_or_create(db) -> AppSettings:
    row = db.query(AppSettings).first()
    if row is None:
        row = AppSettings(settings_json={})
        db.add(row)
        db.flush()
    return row


def get_json(db) -> dict:
    row = db.query(AppSettings).first()
    if row and isinstance(row.settings_json, dict):
        return row.settings_json
    return {}
