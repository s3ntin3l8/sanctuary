import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, NamedTuple

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.services.gmail_oauth_config import OAuthClient

logger = logging.getLogger(__name__)

# Sanctuary only ever reads the mailbox. This is the single scope we request,
# and the only one we accept on a stored or freshly-granted token — see
# assert_readonly_scopes. tests/unit/test_gmail_readonly_guard.py additionally
# fails the build if any Gmail mutating call appears in app/.
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = [READONLY_SCOPE]


class GmailReconnectRequired(Exception):
    """The stored grant is unusable (revoked/expired/over-scoped): reconnect."""


class GmailMessageGone(Exception):
    """The message was deleted in Gmail after it was listed: nothing to retry."""


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


def get_oauth_flow(client: OAuthClient) -> Flow:
    client_config = {
        "web": {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        client_config, scopes=SCOPES, redirect_uri=client.redirect_uri
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


OWN_ADDRESSES_TTL = timedelta(hours=24)


def fetch_own_addresses(service: Any) -> list[str]:
    """The addresses the connected account sends from: its own plus send-as aliases.

    Read-only (``getProfile`` and ``settings.sendAs.list`` are covered by
    gmail.readonly). Aliases still awaiting verification can't send, so they are
    skipped; the primary address always counts.
    """
    profile = service.users().getProfile(userId="me").execute()
    found = {str(profile.get("emailAddress") or "").strip().lower()}
    aliases = service.users().settings().sendAs().list(userId="me").execute()
    for alias in aliases.get("sendAs", []):
        if alias.get("verificationStatus", "accepted") == "accepted":
            found.add(str(alias.get("sendAsEmail") or "").strip().lower())
    return sorted(a for a in found if a)


def detect_own_addresses(credentials_json: str) -> list[str] | None:
    """``fetch_own_addresses`` for fresh credentials; None when Google can't say.

    Goes through ``get_gmail_service`` so the read-only scope check and the
    credential wiring stay in one place.
    """
    try:
        return fetch_own_addresses(get_gmail_service(credentials_json).service)
    except Exception:
        logger.warning("Could not read the Gmail account's addresses", exc_info=True)
        return None


def _own_addresses_stale(settings: dict) -> bool:
    checked = settings.get("gmail_own_addresses_checked_at")
    if not settings.get("gmail_own_addresses") or not checked:
        return True
    try:
        return datetime.now(UTC) - datetime.fromisoformat(checked) > OWN_ADDRESSES_TTL
    except ValueError:
        return True


def _refresh_own_addresses(db: Any, user_id: int, service: Any, settings: dict) -> None:
    """Re-read the account's addresses when missing or older than a day.

    Never fails the caller: on any error the stored list stays as it was.
    """
    from app.services import user_settings_service

    if not _own_addresses_stale(settings):
        return
    try:
        addresses = fetch_own_addresses(service)
        user_settings_service.set_gmail_own_addresses(
            db, user_id, addresses, datetime.now(UTC).isoformat()
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Could not refresh the Gmail account's addresses", exc_info=True)


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
    _refresh_own_addresses(db, user_id, connection.service, settings)
    return connection.service


class GmailRaw(bytes):
    """RFC822 bytes carrying the transport metadata returned by the same GET."""

    metadata: dict


def fetch_raw_message(service: Any, message_id: str) -> bytes:
    import base64
    from email import message_from_bytes
    from email.parser import BytesHeaderParser
    from email.policy import default

    try:
        message = execute_with_retry(
            service.users().messages().get(userId="me", id=message_id, format="raw")
        )
    except HttpError as exc:
        if _http_status(exc) == 404:
            raise GmailMessageGone(message_id) from exc
        raise
    raw = GmailRaw(base64.urlsafe_b64decode(message["raw"]))
    headers = BytesHeaderParser(policy=default).parsebytes(raw)
    metadata = parse_metadata(
        {
            **message,
            "id": message.get("id", message_id),
            "payload": {
                "mimeType": headers.get_content_type(),
                "headers": [{"name": k, "value": str(v)} for k, v in headers.items()],
            },
        }
    )
    mime = message_from_bytes(raw, policy=default)
    metadata["has_attachments"] = any(
        part.get_content_disposition() == "attachment"
        or bool(part.get_filename())
        and not (part.get_content_maintype() == "image" and part.get("Content-ID"))
        for part in mime.walk()
    )
    raw.metadata = {
        k: v.isoformat() if isinstance(v, datetime) else v for k, v in metadata.items()
    }
    return raw


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
    include_sent: bool = False,
    after: int | None = None,
    before: int | None = None,
) -> str:
    """The one Gmail search query every list call goes through.

    ``after``/``before`` are epoch seconds (Gmail's ``after:``/``before:``
    operators accept them), so callers need no date formatting. Needs a sender
    or a label; an unbounded query is refused here as the last line of defence.

    ``include_sent`` also matches mail the account itself sent *to* an allowlisted
    address (``from:me`` resolves to the account and its aliases in Gmail).
    """
    label = (label_filter or "").strip()
    if any(c in label for c in LABEL_FORBIDDEN):
        raise ValueError("Gmail label can't contain quotes or line breaks")
    if not has_filter(allowlist, label):
        raise ValueError("Gmail query needs a sender allowlist or a label")
    parts = []
    if allowlist:
        incoming = " OR ".join(f"from:{e}" for e in allowlist)
        if include_sent:
            recipients = " OR ".join(f"to:{e} OR cc:{e} OR bcc:{e}" for e in allowlist)
            parts.append(f"(({incoming}) OR (from:me ({recipients})))")
        else:
            parts.append(f"({incoming})")
    if label:
        # Quoted so a label can't smuggle extra operators into the search.
        parts.append(f'label:"{label}"')
    if after is not None:
        parts.append(f"after:{after}")
    if before is not None:
        parts.append(f"before:{before}")
    # Every save of a draft is a new message; an unsent one is not correspondence.
    parts.append("-in:drafts")
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

_METADATA_HEADERS = ["From", "To", "Subject", "Date", "Message-ID"]
# messages.get costs 5 units. Gmail allows 250 units/s but also a per-minute
# budget per user (≈15,000 units), so 50 gets/s sits exactly on that limit and
# a full-mailbox index tripped it. 25 gets per batch with a ~1s pause stays at
# about half; rate-limited requests are retried (see fetch_metadata).
_METADATA_BATCH = 25
_METADATA_BATCH_MIN = 5
_BATCH_PAUSE_SECONDS = 1.0
_BATCH_PAUSE_MAX_SECONDS = 8.0
# Waits before each retry round; the per-minute quota refills within a minute.
_RETRY_WAITS_SECONDS = (5, 15, 45, 60)
_METADATA_RETRY_ROUNDS = len(_RETRY_WAITS_SECONDS)
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
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
        results = execute_with_retry(request)
        for message in results.get("messages", []):
            yield message["id"]
        page_token = results.get("nextPageToken")
        if not page_token:
            return
        time.sleep(_LIST_PAGE_PAUSE_SECONDS)


def _http_status(exc: Exception) -> int | None:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None)


def _is_rate_limited(exc: Exception) -> bool:
    """Gmail said "slow down" (or hiccuped): the same request can work later.

    Rate limits come as 429 ("Too many concurrent requests") *and* as 403 with
    reason ``rateLimitExceeded`` / ``userRateLimitExceeded`` ("Quota exceeded …
    Units per minute per user"). Any other 403 (missing permission) is final.
    """
    if not isinstance(exc, HttpError):
        return False
    status = _http_status(exc)
    if status in (429, 500, 502, 503, 504):
        return True
    # error_details is a plain string when Google's body has no `errors` list.
    details = exc.error_details if isinstance(exc.error_details, list) else []
    return status == 403 and any(
        isinstance(d, dict) and d.get("reason") in _RATE_LIMIT_REASONS for d in details
    )


def execute_with_retry(request: Any) -> Any:
    """Run one Gmail request, waiting out rate limits like the metadata path.

    A grant that died mid-run (``RefreshError``, or a 401) ends the run with a
    reconnect request instead of failing every remaining message on its own.
    """
    for wait in (*_RETRY_WAITS_SECONDS, None):
        try:
            return request.execute()
        except RefreshError as exc:
            raise GmailReconnectRequired("Gmail access was revoked") from exc
        except HttpError as exc:
            if _http_status(exc) == 401:
                raise GmailReconnectRequired("Gmail access was revoked") from exc
            if wait is None or not _is_rate_limited(exc):
                raise
            logger.warning("Gmail rate-limited a request; retrying in %ds", wait)
            time.sleep(wait)


def fetch_metadata(
    service: Any, gmail_ids: list[str], *, gone: set[str] | None = None
) -> list[dict]:
    """Headers-only fetch for ``gmail_ids`` via Gmail batch requests.

    Requests Gmail rate-limits are retried with a growing wait (the per-minute
    quota needs up to a minute to refill) and smaller batches. Ids that stay
    failed are logged and left out, so the next index refresh (which only
    fetches ids it doesn't have yet) retries them. Ids Gmail reports as 404
    (deleted since they were listed) are added to ``gone`` when given, so
    callers don't count them as failures.
    """
    found: list[dict] = []
    retry: list[str] = []
    answered: set[str] = set()
    missing: set[str] = set()

    def _collect(request_id: str, response: dict | None, exception: Exception | None):
        answered.add(request_id)
        if exception is None and response is not None:
            found.append(response)
        elif exception is not None and _http_status(exception) == 404:
            missing.add(request_id)
        elif exception is not None and _is_rate_limited(exception):
            retry.append(request_id)
        else:
            logger.warning(
                "Gmail metadata fetch failed for %s: %s", request_id, exception
            )

    pending = list(gmail_ids)
    batch_size, pause = _METADATA_BATCH, _BATCH_PAUSE_SECONDS
    for attempt in range(_METADATA_RETRY_ROUNDS + 1):
        retry.clear()
        for start in range(0, len(pending), batch_size):
            before = len(retry)
            ids = pending[start : start + batch_size]
            batch = service.new_batch_http_request(callback=_collect)
            for gmail_id in ids:
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
            try:
                batch.execute()
            except HttpError as exc:
                # The batch call itself was refused (its own quota), so no
                # sub-request was answered: retry them like any other.
                if not _is_rate_limited(exc):
                    raise
                retry.extend(i for i in ids if i not in answered)
            if len(retry) > before:
                pause = min(pause * 2, _BATCH_PAUSE_MAX_SECONDS)  # back off at once
            if start + batch_size < len(pending):
                time.sleep(pause)
        if not retry or attempt == _METADATA_RETRY_ROUNDS:
            break
        wait = _RETRY_WAITS_SECONDS[attempt]
        logger.warning(
            "Gmail rate-limited %d metadata request(s); retrying in %ds",
            len(retry),
            wait,
        )
        time.sleep(wait)
        pending = list(retry)
        batch_size = max(_METADATA_BATCH_MIN, batch_size // 2)
    if retry:
        logger.warning(
            "Gmail metadata fetch gave up on %d rate-limited message(s)", len(retry)
        )
    if missing:
        logger.info("%d Gmail message(s) no longer exist (deleted)", len(missing))
        if gone is not None:
            gone.update(missing)
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

    received_at = None
    if message.get("internalDate"):
        received_at = datetime.fromtimestamp(
            int(message["internalDate"]) / 1000, tz=UTC
        )

    _, sender = parseaddr(headers.get("from", ""))
    mime_type = payload.get("mimeType", "")
    return {
        "gmail_id": message["id"],
        "thread_id": message.get("threadId") or message["id"],
        "message_id": (headers.get("message-id") or "").strip() or None,
        "sender": sender.lower() or None,
        "subject": _decode_header_value(headers.get("subject", "")) or None,
        "sent_at": sent_at,
        "received_at": received_at,
        # The metadata format may omit parts; multipart/mixed is the usual
        # envelope for a message that carries attachments.
        "has_attachments": _payload_has_attachment(payload)
        or mime_type == "multipart/mixed",
        "size_estimate": message.get("sizeEstimate"),
    }
