"""MCP ingest tools: upload_document and assign_to_case."""

import base64
from unittest.mock import patch

import pytest

from app import config
from app.models.database import AuditLog, Document, IngestBatch
from app.models.enums import ApiTokenAccess, AuditEventType, IngestBatchSourceType

RW = ApiTokenAccess.READ_WRITE
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def b64(data: bytes = PDF) -> str:
    return base64.b64encode(data).decode()


@pytest.fixture(autouse=True)
def no_dispatch():
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        yield dispatch


@pytest.fixture
def triage_doc(db_session):
    def _make(owner, title="Loose letter", case_id="_TRIAGE"):
        doc = Document(title=title, owner_id=owner.id, case_id=case_id)
        db_session.add(doc)
        db_session.commit()
        return doc

    return _make


# --- upload_document -----------------------------------------------------------


def test_per_case_token_uploads_into_its_case(
    mcp_session, make_token, make_case, alice, db_session, no_dispatch
):
    make_case("A-1", alice.id)
    s = mcp_session(make_token(alice, access=RW, all_cases=False, case_ids=["A-1"]))
    error, out = s.call(
        "upload_document", filename="letter.pdf", content_base64=b64(), case_id="A-1"
    )
    assert not error, out
    assert out["case_id"] == "A-1" and out["status"] == "queued"
    doc = db_session.get(Document, out["document_id"])
    assert doc.owner_id == alice.id and doc.case_id == "A-1"
    batch = db_session.get(IngestBatch, out["ingest_batch_id"])
    assert batch.agent_token_id is not None
    no_dispatch.assert_called_once()


def test_per_case_token_must_name_a_writable_case(
    mcp_session, make_token, make_case, alice
):
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)
    s = mcp_session(make_token(alice, access=RW, all_cases=False, case_ids=["A-1"]))
    error, msg = s.call("upload_document", filename="a.pdf", content_base64=b64())
    assert error and "case_id is required" in msg
    error, msg = s.call(
        "upload_document", filename="a.pdf", content_base64=b64(), case_id="A-2"
    )
    assert error and "outside this token's scope" in msg


def test_all_cases_token_without_a_case_lands_in_triage(
    mcp_session, make_token, alice, db_session
):
    s = mcp_session(make_token(alice, access=RW))
    error, out = s.call("upload_document", filename="a.pdf", content_base64=b64())
    assert not error, out
    assert out["case_id"] is None
    doc = db_session.get(Document, out["document_id"])
    assert doc.case_id == "_TRIAGE" and doc.owner_id == alice.id


def test_read_only_token_cannot_upload(mcp_session, make_token, alice):
    s = mcp_session(make_token(alice))
    error, msg = s.call("upload_document", filename="a.pdf", content_base64=b64())
    assert error and "read-only" in msg


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"filename": "evil.exe", "content_base64": b64()}, "Unsupported file type"),
        ({"filename": "mail.eml", "content_base64": b64()}, "Gmail import"),
        ({"filename": "a.pdf", "content_base64": "not base64!!"}, "not valid base64"),
        ({"filename": "a.pdf", "content_base64": ""}, "empty"),
    ],
)
def test_bad_uploads_are_rejected(mcp_session, make_token, alice, kwargs, fragment):
    s = mcp_session(make_token(alice, access=RW))
    error, msg = s.call("upload_document", **kwargs)
    assert error and fragment in msg


def test_oversize_upload_is_rejected(mcp_session, make_token, alice, monkeypatch):
    monkeypatch.setattr(config, "MCP_MAX_UPLOAD_MB", 1)
    s = mcp_session(make_token(alice, access=RW))
    big = b64(PDF + b"0" * (1024 * 1024 + 1))
    error, msg = s.call("upload_document", filename="big.pdf", content_base64=big)
    assert error and "too large" in msg


def test_upload_audit_has_metadata_but_no_content(
    mcp_session, make_token, alice, db_session
):
    s = mcp_session(make_token(alice, access=RW))
    error, out = s.call("upload_document", filename="a.pdf", content_base64=b64())
    assert not error, out
    row = (
        db_session.query(AuditLog).filter_by(event_type=AuditEventType.MCP_UPLOAD).one()
    )
    assert row.actor_label == "mcp:agent"
    assert row.payload["size_bytes"] == len(PDF) and len(row.payload["sha256"]) == 64
    assert b64() not in str(row.payload) and "PDF" not in str(row.payload)


def test_duplicate_upload_reports_the_conflict(mcp_session, make_token, alice):
    s = mcp_session(make_token(alice, access=RW))
    assert not s.call("upload_document", filename="a.pdf", content_base64=b64())[0]
    error, msg = s.call("upload_document", filename="b.pdf", content_base64=b64())
    assert error and msg


# --- assign_to_case ------------------------------------------------------------


def test_assign_document_moves_it_into_the_case(
    mcp_session, make_token, make_case, alice, triage_doc, db_session
):
    make_case("A-1", alice.id)
    doc = triage_doc(alice)
    s = mcp_session(make_token(alice, access=RW))
    with patch("app.services.triage_routing_service.reset_and_reenrich"):
        error, out = s.call("assign_to_case", case_id="A-1", document_id=doc.id)
    assert not error, out
    assert out["case_id"] == "A-1" and out["document_ids"] == [doc.id]
    db_session.expire_all()
    assert db_session.get(Document, doc.id).case_id == "A-1"
    audit = (
        db_session.query(AuditLog).filter_by(event_type=AuditEventType.MCP_WRITE).one()
    )
    assert audit.payload["tool"] == "assign_to_case"


def test_assign_batch_moves_its_triage_documents(
    mcp_session, make_token, make_case, alice, db_session
):
    make_case("A-1", alice.id)
    batch = IngestBatch(
        owner_id=alice.id, case_id="_TRIAGE", source_type=IngestBatchSourceType.MANUAL
    )
    db_session.add(batch)
    db_session.flush()
    doc = Document(
        title="In bundle",
        owner_id=alice.id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
    )
    db_session.add(doc)
    db_session.commit()
    s = mcp_session(make_token(alice, access=RW))
    with patch("app.services.triage_routing_service.reset_and_reenrich"):
        error, out = s.call("assign_to_case", case_id="A-1", batch_id=batch.id)
    assert not error, out
    db_session.expire_all()
    assert db_session.get(Document, doc.id).case_id == "A-1"


def test_assign_rejects_per_case_and_read_only_tokens(
    mcp_session, make_token, make_case, alice, triage_doc
):
    make_case("A-1", alice.id)
    doc = triage_doc(alice)
    per_case = mcp_session(
        make_token(alice, access=RW, all_cases=False, case_ids=["A-1"])
    )
    error, msg = per_case.call("assign_to_case", case_id="A-1", document_id=doc.id)
    assert error and "all-cases" in msg
    read_only = mcp_session(make_token(alice))
    error, msg = read_only.call("assign_to_case", case_id="A-1", document_id=doc.id)
    assert error and "read-only" in msg


def test_assign_rejects_foreign_assigned_and_ambiguous_targets(
    mcp_session, make_token, make_case, alice, bob, triage_doc
):
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)
    foreign = triage_doc(bob, "Bob's")
    filed = triage_doc(alice, "Filed", case_id="A-2")
    mine = triage_doc(alice)
    s = mcp_session(make_token(alice, access=RW))
    error, msg = s.call("assign_to_case", case_id="A-1", document_id=foreign.id)
    assert error and "outside this token's scope" in msg
    error, msg = s.call("assign_to_case", case_id="A-1", document_id=filed.id)
    assert error and "already filed" in msg
    error, msg = s.call("assign_to_case", case_id="A-1")
    assert error and "exactly one" in msg
    error, msg = s.call(
        "assign_to_case", case_id="A-1", document_id=mine.id, batch_id=1
    )
    assert error and "exactly one" in msg


def test_assign_requires_a_case_the_owner_can_edit(
    mcp_session, make_token, make_case, alice, bob, triage_doc
):
    make_case("B-1", bob.id)
    doc = triage_doc(alice)
    s = mcp_session(make_token(alice, access=RW))
    error, msg = s.call("assign_to_case", case_id="B-1", document_id=doc.id)
    assert error and "outside this token's scope" in msg
