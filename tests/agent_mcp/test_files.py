"""MCP get_original_file: bytes round-trip, path containment, size cap, scope, audit."""

import base64
import os

import pytest

import app.config
from app.models.database import AuditLog, Document
from app.models.enums import ApiTokenAccess, AuditEventType

PDF = b"%PDF-1.4\n%fake pdf bytes\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def stored(db_session):
    """Create a Document whose original lives under the test DATA_DIR."""

    def _make(name, data, *, owner, case_id, file_path=None):
        folder = app.config.DATA_DIR / "mcp_files"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        doc = Document(
            title=name,
            owner_id=owner.id,
            case_id=case_id,
            file_path=file_path if file_path is not None else f"mcp_files/{name}",
        )
        db_session.add(doc)
        db_session.commit()
        return doc

    return _make


def _fetch(session, document_id):
    return session.rpc(
        "tools/call",
        {"name": "get_original_file", "arguments": {"document_id": document_id}},
    )["result"]


def _blob(result):
    resource = next(c for c in result["content"] if c["type"] == "resource")["resource"]
    return resource["mimeType"], base64.b64decode(resource["blob"])


@pytest.mark.parametrize(
    ("name", "data", "mime"),
    [("letter.pdf", PDF, "application/pdf"), ("scan.png", PNG, "image/png")],
)
def test_returns_the_exact_bytes_with_the_right_mime(
    mcp_session, make_token, make_case, stored, alice, name, data, mime
):
    make_case("A-1", alice.id)
    doc = stored(name, data, owner=alice, case_id="A-1")
    result = _fetch(mcp_session(make_token(alice)), doc.id)
    assert not result.get("isError")
    assert _blob(result) == (mime, data)
    text = next(c for c in result["content"] if c["type"] == "text")["text"]
    assert name in text and str(len(data)) in text


def test_read_only_token_may_download(
    mcp_session, make_token, make_case, stored, alice
):
    make_case("A-1", alice.id)
    doc = stored("letter.pdf", PDF, owner=alice, case_id="A-1")
    token = make_token(alice, access=ApiTokenAccess.READ)
    assert _blob(_fetch(mcp_session(token), doc.id))[1] == PDF


def test_download_is_audited_without_content(
    mcp_session, make_token, make_case, stored, db_session, alice
):
    make_case("A-1", alice.id)
    doc = stored("letter.pdf", PDF, owner=alice, case_id="A-1")
    _fetch(mcp_session(make_token(alice)), doc.id)
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.event_type == AuditEventType.DATA_EXPORTED)
        .one()
    )
    assert row.actor_user_id == alice.id and row.actor_label == "mcp:agent"
    assert row.target_type == "document" and row.target_id == str(doc.id)
    assert row.payload["tool"] == "get_original_file"
    assert row.payload["size_bytes"] == len(PDF)
    assert "blob" not in row.payload and PDF.decode(errors="ignore") not in str(
        row.payload
    )


def test_paths_escaping_the_data_dir_are_refused(
    mcp_session, make_token, make_case, stored, alice, tmp_path
):
    make_case("A-1", alice.id)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    link = app.config.DATA_DIR / "mcp_files"
    link.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link / "link.txt")
    session = mcp_session(make_token(alice))
    for path in ("../../etc/passwd", "/etc/passwd", str(outside), "mcp_files/link.txt"):
        doc = stored("x.txt", b"x", owner=alice, case_id="A-1", file_path=path)
        result = _fetch(session, doc.id)
        assert result["isError"], path
        assert "not found on disk" in result["content"][0]["text"]
        assert "secret" not in str(result)


def test_missing_file_and_no_file_path_are_tool_errors(
    mcp_session, make_token, make_case, stored, db_session, alice
):
    make_case("A-1", alice.id)
    session = mcp_session(make_token(alice))
    ghost = stored(
        "ghost.pdf", PDF, owner=alice, case_id="A-1", file_path="mcp_files/ghost-x.pdf"
    )
    assert _fetch(session, ghost.id)["isError"]
    bare = Document(title="bare", owner_id=alice.id, case_id="A-1")
    db_session.add(bare)
    db_session.commit()
    assert "No original file" in _fetch(session, bare.id)["content"][0]["text"]


def test_size_cap_points_to_read_document_text(
    mcp_session, make_token, make_case, stored, alice, monkeypatch
):
    make_case("A-1", alice.id)
    doc = stored("big.pdf", PDF * 100, owner=alice, case_id="A-1")
    monkeypatch.setattr(app.config, "MCP_MAX_DOWNLOAD_MB", 0)
    result = _fetch(mcp_session(make_token(alice)), doc.id)
    assert result["isError"]
    assert "read_document_text" in result["content"][0]["text"]
    assert f"/document/{doc.id}" in result["content"][0]["text"]


def test_out_of_scope_documents_are_neutral_not_found(
    mcp_session, make_token, make_case, stored, alice, bob
):
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)
    make_case("B-1", bob.id)
    theirs = stored("b.pdf", PDF, owner=bob, case_id="B-1")
    other_case = stored("a2.pdf", PDF, owner=alice, case_id="A-2")
    bobs_triage = stored("t.pdf", PDF, owner=bob, case_id=None)
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    everything = mcp_session(make_token(alice))
    for session, doc in (
        (scoped, theirs),
        (scoped, other_case),
        (everything, theirs),
        (everything, bobs_triage),
    ):
        result = _fetch(session, doc.id)
        assert result["isError"]
        assert "outside this token's scope" in result["content"][0]["text"]


def test_all_cases_token_can_fetch_its_own_triage_document(
    mcp_session, make_token, stored, alice
):
    doc = stored("t.pdf", PDF, owner=alice, case_id=None)
    assert _blob(_fetch(mcp_session(make_token(alice)), doc.id))[1] == PDF


def test_per_case_token_cannot_fetch_a_triage_document(
    mcp_session, make_token, make_case, stored, alice
):
    make_case("A-1", alice.id)
    doc = stored("t.pdf", PDF, owner=alice, case_id=None)
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    assert _fetch(scoped, doc.id)["isError"]


# --- the matching resource template ------------------------------------------


def _read_original_resource(session, document_id):
    body = session.rpc(
        "resources/read", {"uri": f"sanctuary://document/{document_id}/original"}
    )
    if "error" in body:
        return True, body["error"]["message"]
    return False, body["result"]["contents"][0]


def test_original_resource_returns_the_exact_bytes_and_is_audited(
    mcp_session, make_token, make_case, stored, alice, db_session
):
    make_case("A-1", alice.id)
    doc = stored("letter.pdf", PDF, owner=alice, case_id="A-1")
    error, content = _read_original_resource(mcp_session(make_token(alice)), doc.id)
    assert not error
    assert base64.b64decode(content["blob"]) == PDF
    audit = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.DATA_EXPORTED)
        .one()
    )
    assert audit.target_id == str(doc.id) and audit.actor_label == "mcp:agent"
    assert "blob" not in str(audit.payload)


def test_original_resource_is_scoped_and_capped(
    mcp_session, make_token, make_case, stored, alice, bob, monkeypatch
):
    make_case("A-1", alice.id)
    make_case("B-1", bob.id)
    mine = stored("a.pdf", PDF, owner=alice, case_id="A-1")
    theirs = stored("b.pdf", PDF, owner=bob, case_id="B-1")
    session = mcp_session(make_token(alice))
    error, message = _read_original_resource(session, theirs.id)
    assert error and "outside this token's scope" in message
    monkeypatch.setattr(app.config, "MCP_MAX_DOWNLOAD_MB", 0)
    error, message = _read_original_resource(session, mine.id)
    assert error and "read_document_text" in message
