import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
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


def connect_gmail(db: Any, user_id: int, settings: dict) -> Any:
    """Build the Gmail client from the user's stored (encrypted) credentials.

    A token refreshed along the way is persisted immediately, so it survives
    even if the rest of the caller's work fails.
    """
    from app.services import user_settings_service

    credentials_json = user_settings_service.decrypt_gmail_credentials(
        settings.get("gmail_credentials_json")
    )
    connection = get_gmail_service(credentials_json or "")
    if connection.refreshed_credentials_json:
        user_settings_service.update_gmail_token(
            db, user_id, connection.refreshed_credentials_json
        )
        db.commit()
    return connection.service


def fetch_raw_message(service: Any, message_id: str) -> bytes:
    import base64

    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute()
    )
    return base64.urlsafe_b64decode(msg["raw"])


# Characters that would end the quoted label term (ASCII and typographic quotes)
# or break the query line; shared with the Settings schema's validation.
LABEL_FORBIDDEN = '"“”„‟«»\n\r'


def has_filter(allowlist: list[str] | None, label_filter: str | None) -> bool:
    """True when at least one of sender allowlist / label narrows the mailbox.

    Every Gmail list call needs this: with neither, a query would match the
    whole mailbox, not just the lawyer's mail.
    """
    return bool(allowlist) or bool((label_filter or "").strip())


def build_query(
    allowlist: list[str],
    label_filter: str = "",
    *,
    after: int | None = None,
    before: int | None = None,
) -> str:
    """The one Gmail search query every list call goes through.

    ``after``/``before`` are epoch seconds (Gmail's ``after:``/``before:``
    operators accept them), so callers need no date formatting. Needs a sender
    or a label; an unbounded query is refused here as the last line of defence.
    """
    label = (label_filter or "").strip()
    if any(c in label for c in LABEL_FORBIDDEN):
        raise ValueError("Gmail label can't contain quotes or line breaks")
    if not has_filter(allowlist, label):
        raise ValueError("Gmail query needs a sender allowlist or a label")
    parts = []
    if allowlist:
        parts.append("(" + " OR ".join(f"from:{e}" for e in allowlist) + ")")
    if label:
        # Quoted so a label can't smuggle extra operators into the search.
        parts.append(f'label:"{label}"')
    if after is not None:
        parts.append(f"after:{after}")
    if before is not None:
        parts.append(f"before:{before}")
    return " ".join(parts)


def estimate_matches(service: Any, query: str) -> int:
    """Gmail's own estimate of how many messages ``query`` matches (one list call)."""
    results = (
        service.users().messages().list(userId="me", q=query, maxResults=1).execute()
    )
    return int(results.get("resultSizeEstimate", 0))


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


# --- Metadata-only access (for the history import page) ----------------------

_METADATA_HEADERS = ["From", "Subject", "Date", "Message-ID"]
# Gmail's per-user quota is 250 units/s and messages.get costs 5 units, so 50
# gets per batch with a ~1s pause keeps a full-mailbox index inside the limit.
_METADATA_BATCH = 50
_BATCH_PAUSE_SECONDS = 1.0
_LIST_PAGE_PAUSE_SECONDS = 0.5


def list_message_ids(service: Any, query: str) -> Iterator[str]:
    """Every message id matching ``query`` (newest first), page by page."""
    page_token = None
    while True:
        request = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=500,
                **({"pageToken": page_token} if page_token else {}),
            )
        )
        results = request.execute()
        for message in results.get("messages", []):
            yield message["id"]
        page_token = results.get("nextPageToken")
        if not page_token:
            return
        time.sleep(_LIST_PAGE_PAUSE_SECONDS)


def fetch_metadata(service: Any, gmail_ids: list[str]) -> list[dict]:
    """Headers-only fetch for ``gmail_ids`` via Gmail batch requests.

    Ids whose request fails are logged and left out, so the next index refresh
    (which only fetches ids it doesn't have yet) retries them.
    """
    found: list[dict] = []

    def _collect(request_id: str, response: dict | None, exception: Exception | None):
        if exception is not None or response is None:
            logger.warning(
                "Gmail metadata fetch failed for %s: %s", request_id, exception
            )
            return
        found.append(response)

    for start in range(0, len(gmail_ids), _METADATA_BATCH):
        batch = service.new_batch_http_request(callback=_collect)
        for gmail_id in gmail_ids[start : start + _METADATA_BATCH]:
            batch.add(
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=gmail_id,
                    format="metadata",
                    metadataHeaders=_METADATA_HEADERS,
                ),
                request_id=gmail_id,
            )
        batch.execute()
        if start + _METADATA_BATCH < len(gmail_ids):
            time.sleep(_BATCH_PAUSE_SECONDS)
    return found


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _payload_has_attachment(payload: dict) -> bool:
    if payload.get("filename"):
        return True
    parts = payload.get("parts") or []
    if parts:
        return any(_payload_has_attachment(part) for part in parts)
    return False


def parse_metadata(message: dict) -> dict:
    """Normalise a ``format=metadata`` Gmail message for the index."""
    payload = message.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    sent_at = None
    if headers.get("date"):
        try:
            sent_at = parsedate_to_datetime(headers["date"])
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            sent_at = None
    if sent_at is None:  # unparseable/missing Date: fall back to Gmail's receipt time
        sent_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=UTC)

    _, sender = parseaddr(headers.get("from", ""))
    mime_type = payload.get("mimeType", "")
    return {
        "gmail_id": message["id"],
        "thread_id": message.get("threadId") or message["id"],
        "message_id": (headers.get("message-id") or "").strip() or None,
        "sender": sender.lower() or None,
        "subject": _decode_header_value(headers.get("subject", "")) or None,
        "sent_at": sent_at,
        # The metadata format may omit parts; multipart/mixed is the usual
        # envelope for a message that carries attachments.
        "has_attachments": _payload_has_attachment(payload)
        or mime_type == "multipart/mixed",
        "size_estimate": message.get("sizeEstimate"),
    }
