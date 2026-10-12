"""The instance-wide Google OAuth client used for the Gmail connection.

Stored in ``AppSettings.settings_json["gmail_oauth"]`` (client secret encrypted);
the ``GMAIL_*`` environment variables are the fallback, field by field. The
redirect URI falls back to the callback route on the host the request came in
on. Each user's own grant stays per-user in ``UserSettings``.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from app import config
from app.core import secrets as secrets_store
from app.models.enums import AuditEventType
from app.services import audit_service
from app.services.app_settings_service import _get_or_create, get_json

_KEY = "gmail_oauth"

Source = Literal["ui", "env"]


class OAuthClient(NamedTuple):
    client_id: str
    client_secret: str
    redirect_uri: str
    # Where client id/secret come from; None when nothing is configured.
    source: Source | None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def _stored(db) -> dict:
    section = get_json(db).get(_KEY)
    return section if isinstance(section, dict) else {}


def stored_summary(db) -> tuple[str, bool, str]:
    """(client_id, has_secret, redirect_uri) as stored, without decrypting."""
    stored = _stored(db)
    return (
        stored.get("client_id") or "",
        bool(stored.get("client_secret")),
        stored.get("redirect_uri") or "",
    )


def has_usable_secret(db) -> bool:
    """A secret is stored or in env. Does not decrypt, so a wrong key can't fail it."""
    return bool(_stored(db).get("client_secret") or config.GMAIL_CLIENT_SECRET)


def suggested_redirect_uri(request) -> str:
    return str(request.url_for("gmail_oauth_callback"))


def get_oauth_client(db, request=None) -> OAuthClient:
    """Effective client: stored values win, env fills the gaps."""
    stored = _stored(db)
    client_id = stored.get("client_id") or config.GMAIL_CLIENT_ID
    stored_secret = stored.get("client_secret")
    client_secret = (
        secrets_store.decrypt(stored_secret)
        if stored_secret
        else config.GMAIL_CLIENT_SECRET
    )
    redirect_uri = stored.get("redirect_uri") or config.GMAIL_REDIRECT_URI
    if not redirect_uri and request is not None:
        redirect_uri = suggested_redirect_uri(request)
    # A client_id or secret can only be stored together (the save route requires
    # a secret on first save), so a stored redirect_uri alone leaves this "env".
    source: Source | None = None
    if client_id or client_secret:
        source = "ui" if stored.get("client_id") or stored_secret else "env"
    return OAuthClient(client_id, client_secret, redirect_uri, source)


def save_oauth_client(
    db,
    *,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    actor_user_id: int | None = None,
) -> None:
    """Store the client. ``client_secret=None`` keeps the stored secret."""
    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    section = dict(data.get(_KEY) or {})
    section["client_id"] = client_id
    if client_secret:
        section["client_secret"] = secrets_store.encrypt(client_secret)
    section["redirect_uri"] = redirect_uri
    data[_KEY] = section
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.SETTINGS_INGESTION_CHANGED,
        actor_user_id=actor_user_id,
        payload={"gmail_oauth_client": "saved"},
    )


def clear_oauth_client(db, *, actor_user_id: int | None = None) -> None:
    settings = _get_or_create(db)
    data = dict(settings.settings_json or {})
    data.pop(_KEY, None)
    settings.settings_json = data
    audit_service.record(
        db,
        AuditEventType.SETTINGS_INGESTION_CHANGED,
        actor_user_id=actor_user_id,
        payload={"gmail_oauth_client": "cleared"},
    )
