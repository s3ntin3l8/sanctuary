import json
import logging
from typing import Any, NamedTuple

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from app.config import GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REDIRECT_URI

logger = logging.getLogger(__name__)

# Sanctuary only ever reads the mailbox. This is the single scope we request,
# and the only one we accept on a stored or freshly-granted token — see
# assert_readonly_scopes. tests/unit/test_gmail_readonly_guard.py additionally
# fails the build if any Gmail mutating call appears in app/.
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [READONLY_SCOPE]


class GmailReconnectRequired(Exception):
    """The stored grant is unusable (revoked/expired/over-scoped): reconnect."""


class GmailScopeError(GmailReconnectRequired):
    """The token carries scopes beyond gmail.readonly."""


class GmailConnection(NamedTuple):
    service: Any
    # Serialized credentials, set only when the access token was refreshed
    # during this call so the caller can persist them (None = unchanged).
    refreshed_credentials_json: str | None


def assert_readonly_scopes(granted: list[str] | tuple[str, ...] | None) -> None:
    extra = sorted(set(granted or []) - {READONLY_SCOPE})
    if extra:
        raise GmailScopeError(
            "Gmail token carries scopes beyond read-only access: " + ", ".join(extra)
        )


def get_oauth_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        client_config, scopes=SCOPES, redirect_uri=GMAIL_REDIRECT_URI
    )


def get_gmail_service(credentials_json: str) -> GmailConnection:
    creds_dict = json.loads(credentials_json)
    # The stored "scopes" are what was requested; a token persisted by an older
    # flow could list more. The scopes Google actually granted are only visible
    # on the OAuth callback and on refresh responses (checked below).
    assert_readonly_scopes(creds_dict.get("scopes"))
    creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)

    refreshed_json: str | None = None
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise GmailReconnectRequired(
                "Google rejected the stored Gmail token (revoked or expired)."
            ) from exc
        assert_readonly_scopes(creds.granted_scopes)
        refreshed_json = creds.to_json()

    return GmailConnection(
        build("gmail", "v1", credentials=creds, cache_discovery=False),
        refreshed_json,
    )


def fetch_raw_message(service: Any, message_id: str) -> bytes:
    import base64

    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute()
    )
    return base64.urlsafe_b64decode(msg["raw"])


def build_query(
    allowlist: list[str],
    label_filter: str = "",
    *,
    after: int | None = None,
    before: int | None = None,
) -> str:
    """The one Gmail search query every list call goes through.

    ``after``/``before`` are epoch seconds (Gmail's ``after:``/``before:``
    operators accept them), so callers need no date formatting.
    """
    query = "(" + " OR ".join(f"from:{e}" for e in allowlist) + ")"
    if label_filter:
        query += f" label:{label_filter}"
    if after is not None:
        query += f" after:{after}"
    if before is not None:
        query += f" before:{before}"
    return query


def revoke_token(credentials_json: str) -> bool:
    """Best-effort revoke of Sanctuary's own grant at Google.

    Cancels this app's access only; it touches no mail. Returns False on any
    failure — disconnecting locally must not depend on Google being reachable.
    """
    import requests

    try:
        info = json.loads(credentials_json)
        token = info.get("refresh_token") or info.get("token")
        if not token:
            return False
        resp = requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token},
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        logger.warning("Gmail token revoke failed", exc_info=True)
        return False
