"""Sanctuary must never change anything in the user's mailbox.

Three layers: the requested OAuth scope, rejection of any broader grant, and a
source scan that fails the build if a Gmail mutating call is ever written.
"""

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from app.services.ingestion import gmail
from app.services.ingestion.gmail import (
    READONLY_SCOPE,
    SCOPES,
    GmailReconnectRequired,
    GmailScopeError,
    assert_readonly_scopes,
    build_query,
    get_gmail_service,
)

pytestmark = pytest.mark.unit

APP_DIR = Path(__file__).resolve().parents[2] / "app"

# Gmail API resources and the verbs on them that write, delete, send or relabel.
_GMAIL_RESOURCES = {"messages", "threads", "labels", "drafts", "history", "settings"}
_GMAIL_MUTATORS = {
    "modify",
    "batchModify",
    "batchDelete",
    "trash",
    "untrash",
    "delete",
    "send",
    "insert",
    "import_",
    "create",
    "update",
    "patch",
}


def _mutating_gmail_calls(source: str) -> list[int]:
    """Line numbers of ``<...>.<gmail resource>().<mutator>(...)`` calls."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _GMAIL_MUTATORS:
            continue
        receiver = node.func.value
        while isinstance(receiver, ast.Call | ast.Attribute):
            if isinstance(receiver, ast.Call):
                receiver = receiver.func
                continue
            if (
                isinstance(receiver.value, ast.Call)
                and receiver.attr in _GMAIL_RESOURCES
            ):
                hits.append(node.lineno)
                break
            receiver = receiver.value
    return hits


def test_only_the_readonly_scope_is_requested():
    assert SCOPES == [READONLY_SCOPE]
    assert READONLY_SCOPE.endswith("/gmail.readonly")


def test_no_gmail_mutating_calls_in_app():
    offenders = {
        str(path.relative_to(APP_DIR.parent)): lines
        for path in APP_DIR.rglob("*.py")
        if (lines := _mutating_gmail_calls(path.read_text()))
    }
    assert not offenders, f"Gmail mutating calls found: {offenders}"


def test_scanner_flags_mutations_and_ignores_reads():
    """Guard the guard: the scan must actually catch what it claims to."""
    assert _mutating_gmail_calls(
        'service.users().messages().trash(userId="me", id=x).execute()'
    )
    assert _mutating_gmail_calls(
        "svc.users().messages().modify(userId='me', id=x, body={}).execute()"
    )
    assert _mutating_gmail_calls("svc.users().labels().create(userId='me').execute()")
    assert not _mutating_gmail_calls(
        "svc.users().messages().get(userId='me', id=x, format='raw').execute()"
    )
    assert not _mutating_gmail_calls("svc.users().messages().list(userId='me')")
    assert not _mutating_gmail_calls("session.delete(obj); some_list.update({})")


def test_assert_readonly_scopes():
    assert_readonly_scopes([READONLY_SCOPE])
    assert_readonly_scopes(None)
    with pytest.raises(GmailScopeError, match="mail.google.com"):
        assert_readonly_scopes([READONLY_SCOPE, "https://mail.google.com/"])


def test_stored_credentials_with_extra_scopes_are_rejected():
    with pytest.raises(GmailScopeError):
        get_gmail_service('{"scopes": ["https://mail.google.com/"]}')


def _creds(*, expired: bool, granted=(READONLY_SCOPE,), refresh_error=None):
    creds = MagicMock()
    creds.expired = expired
    creds.refresh_token = "refresh"  # pragma: allowlist secret
    creds.granted_scopes = list(granted)
    creds.to_json.return_value = '{"token": "new"}'
    if refresh_error:
        creds.refresh.side_effect = refresh_error
    return creds


def _service_for(creds):
    cred_cls = MagicMock()
    cred_cls.from_authorized_user_info.return_value = creds
    return (
        patch.object(gmail, "Credentials", cred_cls),
        patch.object(gmail, "build", return_value="SERVICE"),
    )


def test_expired_token_is_refreshed_and_returned_for_persistence():
    creds = _creds(expired=True)
    p1, p2 = _service_for(creds)
    with p1, p2:
        conn = get_gmail_service("{}")
    assert conn.service == "SERVICE"
    assert conn.refreshed_credentials_json == '{"token": "new"}'


def test_unexpired_token_is_not_refreshed_or_rewritten():
    creds = _creds(expired=False)
    p1, p2 = _service_for(creds)
    with p1, p2:
        conn = get_gmail_service("{}")
    creds.refresh.assert_not_called()
    assert conn.refreshed_credentials_json is None


def test_revoked_token_requires_reconnect():
    creds = _creds(expired=True, refresh_error=RefreshError("invalid_grant"))
    p1, p2 = _service_for(creds)
    with p1, p2, pytest.raises(GmailReconnectRequired):
        get_gmail_service("{}")


def test_refresh_that_reports_broader_scopes_is_rejected():
    creds = _creds(expired=True, granted=(READONLY_SCOPE, "https://mail.google.com/"))
    p1, p2 = _service_for(creds)
    with p1, p2, pytest.raises(GmailScopeError):
        get_gmail_service("{}")


def test_build_query():
    assert build_query(["a@x.de"]) == "(from:a@x.de)"
    assert (
        build_query(["a@x.de", "b.de"], "Sanctuary", after=10, before=20)
        == "(from:a@x.de OR from:b.de) label:Sanctuary after:10 before:20"
    )
