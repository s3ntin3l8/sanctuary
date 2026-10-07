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

# Gmail API resource getters (`service.users().messages()`, ...). The scan is a
# WHITELIST: wherever the app obtains one of these, the only thing it may do with
# it is call `.get(...)` or `.list(...)`. Anything else — a known mutator
# (`trash`, `modify`, `delete`, `send`, ...), a mutator added to the API later,
# or stashing the resource in a variable where it could be used out of sight — is
# a violation. The chain does not have to start at `.users()`.
_GMAIL_RESOURCES = {"messages", "threads", "labels", "drafts", "history", "settings"}
_READ_VERBS = {"get", "list"}


def _non_read_gmail_uses(source: str) -> list[int]:
    """Line numbers where a Gmail resource is used for anything but get/list."""
    tree = ast.parse(source)
    parent = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    hits = []
    for node in ast.walk(tree):
        # Gmail's resource getters take no arguments; `ChatRepository.messages(id)`
        # and the like are unrelated and must not trip the scan.
        is_resource_getter = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _GMAIL_RESOURCES
            and not node.args
            and not node.keywords
        )
        if not is_resource_getter:
            continue
        verb = parent.get(node)  # `<resource>()` -> `.verb`
        call = parent.get(verb) if verb is not None else None
        read_only = (
            isinstance(verb, ast.Attribute)
            and verb.attr in _READ_VERBS
            and isinstance(call, ast.Call)
            and call.func is verb
        )
        if not read_only:
            hits.append(node.lineno)
    return hits


def test_only_the_readonly_scope_is_requested():
    assert SCOPES == [READONLY_SCOPE]
    assert READONLY_SCOPE.endswith("/gmail.readonly")


def test_no_gmail_mutating_calls_in_app():
    offenders = {
        str(path.relative_to(APP_DIR.parent)): lines
        for path in APP_DIR.rglob("*.py")
        if (lines := _non_read_gmail_uses(path.read_text()))
    }
    assert not offenders, f"Gmail mutating calls found: {offenders}"


@pytest.mark.parametrize(
    "source",
    [
        'service.users().messages().trash(userId="me", id=x).execute()',
        "svc.users().messages().modify(userId='me', id=x, body={}).execute()",
        "svc.users().labels().create(userId='me').execute()",
        # No `.users()` in front of the resource: still caught.
        "service.messages().delete(id='y').execute()",
        "client.threads().trash(id='t')",
        # A mutator the API grows later is caught too (whitelist, not blacklist).
        "svc.users().messages().someFutureWriteVerb(id='y')",
        # The resource stashed away, to be mutated out of sight.
        "msgs = svc.users().messages()\nmsgs.trash(id='y')",
        "x = svc.users().drafts()",
        # `.get` / `.list` as bare attributes, not calls, do not count as reads.
        "svc.users().messages().get",
    ],
)
def test_scanner_flags_anything_but_get_and_list(source):
    """Guard the guard: each of these must be reported, or the scan is blind to it."""
    assert _non_read_gmail_uses(source)


@pytest.mark.parametrize(
    "source",
    [
        "svc.users().messages().get(userId='me', id=x, format='raw').execute()",
        "svc.users().messages().list(userId='me', q='x')",
        "service.messages().get(id='y')",
        "session.delete(obj); some_list.update({}); payload.send(x)",
        "repo.messages(conversation.id)[:-1]",  # same name, takes an argument
    ],
)
def test_scanner_ignores_reads_and_unrelated_code(source):
    assert not _non_read_gmail_uses(source)


def test_scanner_resource_list_is_what_the_test_says_it_is():
    """Mutation check: the positives above only mean something because these
    names are in the scanned set — dropping one must not be silently survivable."""
    assert {"messages", "threads", "labels", "drafts"} <= _GMAIL_RESOURCES


def test_assert_readonly_scopes():
    assert_readonly_scopes([READONLY_SCOPE])
    assert_readonly_scopes(None)
    with pytest.raises(GmailScopeError, match="beyond read-only"):
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
