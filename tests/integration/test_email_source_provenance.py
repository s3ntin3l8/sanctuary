"""Source boundaries, accounting, dates, replay, and access isolation."""

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import patch

import pytest

from app.dependencies import get_current_user
from app.main import app
from app.models.database import Document, IngestBatch, User
from app.models.enums import DocumentRole, OriginatorType
from app.services import gmail_cache, gmail_replay
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.email_parser import _parse_attachment_manifest, parse_rfc822
from app.services.ingestion.email_sources import bind_manifests, source_groups
from app.services.intelligence.batch_analyzer import _apply_batch_results
from app.services.intelligence.email_context import forwarding_context
from app.services.triage_bundles import get_triage_bundles

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_replay_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_replay, "manifest_path", lambda: tmp_path / "replay.json")


def message(message_id="<source-test@example.com>"):
    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Cc"] = "assistant@example.com"
    msg["Bcc"] = "private@example.com"
    msg["Reply-To"] = "reply@example.com"
    msg["Date"] = "Tue, 26 May 2026 08:00:00 +0200"
    msg["Message-ID"] = message_id
    msg["Subject"] = "Legal delivery"
    msg.set_content("Current forwarding context.")
    return msg


def ingest(db, msg, **kwargs):
    with patch("app.services.ingestion.batch_orchestrator.dispatch_task"):
        return ingest_raw_email(db, msg.as_bytes(), **kwargs)


@pytest.mark.parametrize(
    "encoded", [None, "base64", "quoted-printable", "octet-stream"]
)
def test_nested_email_keeps_headers_and_pdf_boundaries(db_session, encoded):
    import quopri

    inner = message("<inner@example.com>")
    inner.replace_header("Subject", "Original court delivery")
    inner.add_attachment(
        b"%PDF inner", maintype="application", subtype="pdf", filename="inner.pdf"
    )
    outer = message()
    if encoded is None:
        outer.add_attachment(inner, filename="forward.eml")
    elif encoded == "octet-stream":
        outer.add_attachment(
            inner.as_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename="forward.eml",
        )
    else:
        wrapper = EmailMessage()
        wrapper["Content-Type"] = "message/rfc822"
        wrapper["Content-Disposition"] = 'attachment; filename="forward.eml"'
        wrapper["Content-Transfer-Encoding"] = encoded
        data = (
            base64.b64encode(inner.as_bytes())
            if encoded == "base64"
            else quopri.encodestring(inner.as_bytes())
        )
        wrapper.set_payload(data.decode())
        outer.make_mixed()
        outer.attach(wrapper)
    parsed = parse_rfc822(outer.as_bytes())
    assert len(parsed["sources"]) == 2
    assert "Original court" not in parsed["body"]
    batch = ingest(db_session, outer)
    assert len(batch.email_sources) == 2
    nested = batch.email_sources[1]
    assert nested["message_id"] == "<inner@example.com>"
    assert nested["subject"] == "Original court delivery"
    assert nested["bcc"] == ["private@example.com"]
    assert nested["parent_id"] == "0"
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()
    assert doc.email_source_id == nested["id"]
    assert doc.original_filename == "inner.pdf"


def test_one_pdf_preserves_note_for_metadata_and_uses_arrival(db_session):
    msg = message()
    msg.set_content(
        "Use proceeding 123 F 456/26 for the attached document.\nOn Monday somebody wrote:\nOld irrelevant note"
    )
    msg.add_attachment(
        b"%PDF one", maintype="application", subtype="pdf", filename="ruling.pdf"
    )
    arrival = datetime(2026, 5, 28, 15, tzinfo=UTC)
    batch = ingest(
        db_session,
        msg,
        mailbox_received_at=arrival,
        gmail_id="abc123",
        gmail_thread_id="thread",
    )
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()
    assert batch.received_at == doc.received_date == arrival
    assert batch.received_source == "gmail"
    context = forwarding_context(db_session, doc)
    assert "123 F 456/26" in context
    assert "Old irrelevant" not in context
    assert "not document evidence" in context
    assert batch.email_sources[0]["sent_at"] != arrival.isoformat()


def test_long_note_is_read_in_full_and_reductions_are_cached(db_session):
    from app.services.intelligence.email_context import RoutingHints

    msg = message()
    msg.set_content("Routing text. " * 3000 + "Last reference 123 F 456/26")
    msg.add_attachment(
        b"%PDF one", maintype="application", subtype="pdf", filename="ruling.pdf"
    )
    batch = ingest(db_session, msg)
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()
    with patch(
        "app.services.intelligence._ai_call.call_json_ai",
        return_value=RoutingHints(hints=["Reference 123 F 456/26"]),
    ) as called:
        forwarding_context(db_session, doc)
        assert called.call_count > 1
        assert "Last reference" in called.call_args_list[-1].kwargs["user_prompt"]
        count = called.call_count
        forwarding_context(db_session, doc)
        assert called.call_count == count


def test_unnamed_inline_text_and_unsupported_parts_have_outcomes(db_session):
    msg = message()
    msg.add_attachment(b"%PDF unnamed", maintype="application", subtype="pdf")
    msg.add_attachment(
        "Named inline text",
        subtype="plain",
        filename="statement.txt",
        disposition="inline",
    )
    msg.add_attachment(
        b"zip bytes", maintype="application", subtype="zip", filename="archive.zip"
    )
    msg.add_attachment(b"", maintype="application", subtype="pdf", filename="empty.pdf")
    batch = ingest(db_session, msg)
    parts = batch.email_sources[0]["parts"]
    assert [p["outcome"] for p in parts] == [
        "ingested",
        "ingested",
        "unsupported",
        "empty",
    ]
    assert parts[0]["original_filename"] is None
    assert parts[0]["filename"].endswith(".pdf")
    assert (
        db_session.query(Document).filter(Document.ingest_batch_id == batch.id).count()
        == 2
    )
    assert "attachment_unsupported" in batch.source_review_reasons


def test_embedded_logo_is_accounted_but_does_not_discard_body():
    msg = message()
    msg.add_alternative('<p>Body</p><img src="cid:logo">', subtype="html")
    html = msg.get_payload()[1]
    html.add_related(
        b"image",
        maintype="image",
        subtype="png",
        cid="<logo>",
        filename="logo.png",
        disposition="inline",
    )
    parsed = parse_rfc822(msg.as_bytes())
    assert parsed["parts"][0]["outcome"] == "ignored_inline"
    assert not parsed["has_attachments"]


@pytest.mark.parametrize(
    "line",
    [
        "  > SCHR.PDF: 26.05.2026 08:24:30 - “Landgericht Ingolstadt”",
        'SCHR.PDF:\n 26.05.2026 08:24:30\n - "Landgericht Ingolstadt"',
    ],
)
def test_manifest_variations_match_same_unique_part(line):
    entries = _parse_attachment_manifest(line)
    assert entries[0]["timestamp"] == "2026-05-26T06:24:30+00:00"
    source = {
        "id": "0",
        "parts": [
            {
                "id": "0.1",
                "filename": "schr.pdf",
                "document_ids": [10],
                "outcome": "ingested",
            }
        ],
        "attachment_manifest": entries,
    }
    assert bind_manifests([source]) == []
    assert entries[0]["doc_id"] == 10


@pytest.mark.parametrize(
    "date", ["30.02.2026 08:24", "25.10.2026 02:30", "29.03.2026 02:30"]
)
def test_invalid_and_ambiguous_local_manifest_dates_require_review(date):
    entry = _parse_attachment_manifest(f'A.PDF: {date} - "Court"')[0]
    assert entry["issue"] == "invalid_manifest_timestamp"


def test_duplicate_names_and_conflicting_history_do_not_get_guessed():
    entries = _parse_attachment_manifest(
        'A.PDF: 26.05.2026 08:24 - "Court"\nA.PDF: 27.05.2026 08:24 - "Court"'
    )
    parts = [
        {"id": "0.1", "filename": "a.pdf", "document_ids": [10], "outcome": "ingested"}
    ]
    source = {"id": "0", "parts": parts, "attachment_manifest": entries}
    assert "ambiguous_manifest_history" in bind_manifests([source])
    batch = IngestBatch(email_sources=[source])
    doc = Document(id=10, email_source_id="0", email_part_id="0.1")
    assert source_groups(batch, [doc]) == ({}, {10})
    source["parts"].append({**parts[0], "id": "0.2"})
    assert "ambiguous_manifest_filename" in bind_manifests([source])


def test_source_membership_blocks_cross_transmission_sweep_and_rebuilds_edges(
    db_session,
):
    batch = ingest(db_session, message())
    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
    docs[0].content = "Court forwarding letter content"
    docs[0].originator_type = OriginatorType.COURT
    for n in range(2):
        doc = Document(
            title=f"Court {n}",
            content="Court content with enough text",
            owner_id=batch.owner_id,
            ingest_batch_id=batch.id,
            case_id="_TRIAGE",
            originator_type=OriginatorType.COURT,
        )
        db_session.add(doc)
        docs.append(doc)
    db_session.commit()
    cover, enclosed, unrelated = docs
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [
                    {"enclosed_doc_id": enclosed.id},
                    {"enclosed_doc_id": unrelated.id},
                ],
            }
        ]
    }
    groups = {cover.id: "a", enclosed.id: "a", unrelated.id: "b"}
    _apply_batch_results(batch.id, docs, result, db_session, source_membership=groups)
    assert enclosed.parent_id == cover.id
    assert unrelated.parent_id is None
    _apply_batch_results(
        batch.id, docs, {"bundles": []}, db_session, source_membership=groups
    )
    assert all(d.parent_id is None and d.role == DocumentRole.STANDALONE for d in docs)


def test_zero_document_problem_is_visible_and_owner_scoped(
    db_session, app_client, sample_user
):
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    msg = message()
    msg.add_attachment(
        b"zip", maintype="application", subtype="zip", filename="archive.zip"
    )
    batch = ingest(db_session, msg, owner_id=owner.id)
    assert not batch.documents
    bundles = get_triage_bundles(db_session, enrich=False, owner_id=owner.id)
    assert batch.id in [b.batch_id for b in bundles]
    assert batch.id not in [
        b.batch_id
        for b in get_triage_bundles(db_session, enrich=False, owner_id=sample_user.id)
    ]
    url = f"/api/v1/triage/bundles/{batch.id}/sources"
    assert app_client.get(url).status_code == 200
    source = batch.email_sources[0]
    part_url = url + f"/{source['id']}/parts/{source['parts'][0]['id']}"
    downloaded = app_client.get(part_url)
    assert downloaded.content == b"zip"
    assert downloaded.headers["content-type"] == "application/octet-stream"
    app.dependency_overrides[get_current_user] = lambda: sample_user
    try:
        assert app_client.get(url).status_code == 404
        assert app_client.get(part_url).status_code == 404
        assert app_client.post(url + "/acknowledge").status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert app_client.post(url + "/acknowledge").status_code == 200
    assert batch.id not in [
        b.batch_id
        for b in get_triage_bundles(db_session, enrich=False, owner_id=owner.id)
    ]


def test_exact_replay_without_message_id_is_offline_and_idempotent(
    db_session, sample_user
):
    msg = message()
    del msg["Message-ID"]
    raw = msg.as_bytes()
    owner = sample_user.id
    arrival = datetime(2026, 5, 28, tzinfo=UTC)
    gmail_cache.write(owner, "abcd", raw)
    gmail_cache.write_metadata(
        owner, "abcd", {"received_at": arrival.isoformat(), "thread_id": "thread"}
    )
    ingest(
        db_session,
        msg,
        owner_id=owner,
        gmail_id="abcd",
        gmail_thread_id="thread",
        mailbox_received_at=arrival,
    )
    manifest = gmail_replay.capture(db_session)
    assert manifest["ready"]
    assert len(manifest["messages"]) == 1
    with pytest.raises(ValueError, match="already exists"):
        gmail_replay.capture(db_session)
    with patch("app.services.ingestion.batch_orchestrator.dispatch_task"):
        result = gmail_replay.replay(db_session)
    assert result["missing"] == 0
    assert (
        db_session.query(IngestBatch).filter(IngestBatch.gmail_id == "abcd").count()
        == 1
    )
    gmail_cache.write(owner, "abcd", b"changed")
    with pytest.raises(ValueError, match="cache changed"):
        gmail_replay.load_manifest()


def test_analysis_partitions_transmissions_and_flags_omitted_members(
    db_session, db_session_factory
):
    from app.services.intelligence.batch_analyzer import analyze

    msg = message()
    msg.set_content(
        "\n".join(f'{name}.PDF: 26.05.2026 08:24 - "Court"' for name in ["A", "B", "C"])
    )
    for name in ["A", "B", "C"]:
        msg.add_attachment(
            f"%PDF {name}".encode(),
            maintype="application",
            subtype="pdf",
            filename=f"{name}.PDF",
        )
    batch = ingest(db_session, msg)
    docs = (
        db_session.query(Document)
        .filter(Document.ingest_batch_id == batch.id)
        .order_by(Document.id)
        .all()
    )
    for doc in docs:
        doc.content = "Court document content with enough text for analysis"
    db_session.commit()
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": docs[0].id,
                "enclosed": [{"enclosed_doc_id": docs[1].id}],
            }
        ]
    }
    with (
        patch("app.config.SessionLocal", db_session_factory),
        patch("app.services.ai_provider.chat_provider.reload_from_db"),
        patch(
            "app.services.intelligence.batch_analyzer._call_batch_analyzer_sync",
            return_value=result,
        ),
    ):
        assert analyze(batch.id)
    db_session.expire_all()
    assert docs[2].parent_id is None
    assert "unresolved_source_group" in batch.source_review_reasons
    # Acknowledgement never suppresses a genuinely new problem found later.
    batch.source_review_acknowledged = True
    batch.source_review_reasons = ["mailbox_arrival_unavailable"]
    db_session.commit()
    with (
        patch("app.config.SessionLocal", db_session_factory),
        patch("app.services.ai_provider.chat_provider.reload_from_db"),
        patch(
            "app.services.intelligence.batch_analyzer._call_batch_analyzer_sync",
            return_value=result,
        ),
    ):
        analyze(batch.id)
    db_session.refresh(batch)
    assert not batch.source_review_acknowledged


@pytest.mark.parametrize(
    "issue",
    [
        "attachment_unsupported",
        "unmatched_manifest_entry",
        "ambiguous_manifest_history",
        "invalid_manifest_timestamp",
        "unresolved_source_group",
    ],
)
def test_source_cleanup_preserves_unresolved_parts_and_bundle_delete_removes_raw(
    db_session,
    issue,
):
    from app.core.paths import resolve_storage_path
    from app.services.document_service import DocumentService
    from app.services.triage_dismissal import delete_bundle

    msg = message()
    msg.add_attachment(
        b"%PDF one", maintype="application", subtype="pdf", filename="one.pdf"
    )
    if issue == "attachment_unsupported":
        msg.add_attachment(
            b"zip", maintype="application", subtype="zip", filename="unsupported.zip"
        )
    batch = ingest(db_session, msg)
    batch.source_review_reasons = [issue]
    db_session.commit()
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()
    raw_path = resolve_storage_path(batch.raw_source_path)
    assert raw_path.exists()
    DocumentService(db_session).delete_document(doc.id)
    db_session.expire_all()
    retained = db_session.get(IngestBatch, batch.id)
    assert retained is not None
    assert retained.email_sources[0]["parts"][0]["outcome"] == "removed"
    assert raw_path.exists()
    assert delete_bundle(db_session, batch_id=batch.id)
    assert not raw_path.exists()


def test_case_purge_removes_retained_email_source(db_session, sample_case):
    from app.core.paths import resolve_storage_path
    from app.services.case_service import CaseService

    batch = ingest(db_session, message())
    batch.case_id = sample_case.id
    doc = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).one()
    doc.case_id = sample_case.id
    db_session.commit()
    raw_path = resolve_storage_path(batch.raw_source_path)
    assert raw_path.exists()
    CaseService(db_session).purge(sample_case.id)
    assert not raw_path.exists()


def test_raw_gmail_fetch_keeps_metadata_from_same_response():
    from unittest.mock import MagicMock

    from app.services.ingestion.gmail import fetch_raw_message

    service = MagicMock()
    raw = message().as_bytes()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "abcd",
        "threadId": "thread",
        "internalDate": "1779976800000",
        "raw": base64.urlsafe_b64encode(raw).decode(),
    }
    result = fetch_raw_message(service, "abcd")
    assert result == raw
    assert result.metadata["thread_id"] == "thread"
    assert result.metadata["received_at"] is not None
    assert result.metadata["message_id"] == "<source-test@example.com>"


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("SCHR_26_05_26.pdf", "2026-05-26"),
        ("Court_26.05.2026.pdf", "2026-05-26"),
        ("Court_31_02_2026.pdf", None),
        ("Anlage_3_4_25.pdf", None),
        ("Court_3_4_2025.pdf", "2025-04-03"),
    ],
)
def test_german_filename_dates_are_validated_low_confidence_hints(filename, expected):
    from app.services.ingestion.extractors import extract_issued_date

    result = extract_issued_date("", filename)
    assert (result["value"].date().isoformat() if result["value"] else None) == expected
    assert result["confidence"] == "low"


def test_identical_content_in_two_transmissions_requires_review(db_session):
    msg = message()
    msg.set_content(
        'A.PDF: 26.05.2026 08:24 - "Court"\nB.PDF: 27.05.2026 08:24 - "Court"'
    )
    for filename in ["A.PDF", "B.PDF"]:
        msg.add_attachment(
            b"%PDF same bytes", maintype="application", subtype="pdf", filename=filename
        )
    batch = ingest(db_session, msg)
    docs = db_session.query(Document).filter(Document.ingest_batch_id == batch.id).all()
    assert len(docs) == 1
    assert source_groups(batch, docs) == ({}, {docs[0].id})


def test_source_references_survive_slicing(db_session):
    from app.services.ingestion.email_sources import sync_document_parts

    msg = message()
    msg.add_attachment(
        b"%PDF source", maintype="application", subtype="pdf", filename="source.pdf"
    )
    batch = ingest(db_session, msg)
    doc = batch.documents[0]
    sync_document_parts(db_session, doc.id, [1001, 1002])
    db_session.commit()
    db_session.refresh(batch)
    assert batch.email_sources[0]["parts"][0]["document_ids"] == [1001, 1002]


def test_raw_gmail_related_attachment_indicator():
    from unittest.mock import MagicMock

    from app.services.ingestion.gmail import fetch_raw_message

    msg = message()
    msg.make_related()
    part = EmailMessage()
    part.set_content(b"%PDF one", maintype="application", subtype="pdf")
    part["Content-Disposition"] = 'inline; filename="one.pdf"'
    msg.attach(part)
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "abcd",
        "threadId": "thread",
        "internalDate": "1779976800000",
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(),
    }
    assert fetch_raw_message(service, "abcd").metadata["has_attachments"]


def test_invalid_attached_email_encoding_has_one_recoverable_outcome():
    outer = message()
    wrapper = EmailMessage()
    wrapper["Content-Type"] = "message/rfc822"
    wrapper["Content-Transfer-Encoding"] = "base64"
    wrapper["Content-Disposition"] = 'attachment; filename="broken.eml"'
    wrapper.set_payload("!!!! invalid base64")
    outer.make_mixed()
    outer.attach(wrapper)
    parsed = parse_rfc822(outer.as_bytes())
    assert len(parsed["sources"]) == 1
    assert len(parsed["parts"]) == 1
    assert parsed["parts"][0]["outcome"] == "failed"
    assert b"invalid base64" in parsed["parts"][0]["content"]


def test_trimmed_standalone_slice_keeps_its_confirmed_placement(db_session):
    batch = ingest(db_session, message())
    cover = batch.documents[0]
    trimmed = Document(
        title="User-trimmed standalone",
        owner_id=batch.owner_id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        role=DocumentRole.STANDALONE,
        meta={"slice_range": [1, 2]},
    )
    db_session.add(trimmed)
    db_session.commit()
    result = {
        "bundles": [
            {
                "cover_letter_doc_id": cover.id,
                "enclosed": [{"enclosed_doc_id": trimmed.id}],
            }
        ]
    }
    _apply_batch_results(
        batch.id,
        [cover, trimmed],
        result,
        db_session,
        source_membership={cover.id: "source:0", trimmed.id: "source:0"},
    )
    assert trimmed.parent_id is None
    assert trimmed.role == DocumentRole.STANDALONE


def test_replay_capture_preserves_oldest_first_order(db_session, sample_user):
    from app.services.gmail_index_service import upsert_metadata

    for gmail_id, day in [("aaa", 28), ("zzz", 26)]:
        msg = message(f"<{gmail_id}@example.com>")
        raw = msg.as_bytes()
        sent_at = datetime(2026, 5, day, tzinfo=UTC)
        gmail_cache.write(sample_user.id, gmail_id, raw)
        upsert_metadata(
            db_session,
            sample_user.id,
            [
                {
                    "gmail_id": gmail_id,
                    "thread_id": "thread",
                    "message_id": msg["Message-ID"],
                    "sent_at": sent_at,
                    "received_at": sent_at,
                    "has_attachments": False,
                }
            ],
        )
        ingest(
            db_session,
            msg,
            owner_id=sample_user.id,
            gmail_id=gmail_id,
            mailbox_received_at=sent_at,
        )
    manifest = gmail_replay.capture(db_session)
    assert manifest["ready"]
    assert [entry["gmail_id"] for entry in manifest["messages"]] == ["zzz", "aaa"]

    # Cached transport metadata is timezone-aware, while legacy index dates
    # come back from Postgres without tzinfo. Both must sort together offline.
    from app.models.database import GmailMessageIndex

    gmail_cache.write_metadata(
        sample_user.id,
        "zzz",
        {
            "sent_at": "2026-05-26T00:00:00+00:00",
            "received_at": "2026-05-26T00:00:00+00:00",
            "thread_id": "thread",
        },
    )
    db_session.query(GmailMessageIndex).filter_by(
        owner_id=sample_user.id, gmail_id="zzz"
    ).delete()
    db_session.commit()
    manifest = gmail_replay.capture(db_session, replace=True)
    assert manifest["ready"]
    assert [entry["gmail_id"] for entry in manifest["messages"]] == ["zzz", "aaa"]


def test_online_replay_capture_skips_a_message_deleted_in_gmail(
    db_session, sample_user
):
    from app.services import user_settings_service
    from app.services.ingestion.gmail import GmailMessageGone

    user_settings_service._get_or_create_user(db_session, sample_user.id)
    db_session.commit()
    msg = message("<deleted@example.com>")
    sent_at = datetime(2026, 5, 26, tzinfo=UTC)
    ingest(
        db_session,
        msg,
        owner_id=sample_user.id,
        gmail_id="gone",
        mailbox_received_at=sent_at,
    )
    # Index and transport metadata survive; only the raw message is gone.
    gmail_cache.write_metadata(
        sample_user.id,
        "gone",
        {
            "sent_at": sent_at.isoformat(),
            "received_at": sent_at.isoformat(),
            "thread_id": "thread",
        },
    )

    with (
        patch.object(gmail_replay, "connect_gmail"),
        patch.object(gmail_replay, "fetch_metadata", return_value=[]),
        patch(
            "app.tasks.gmail_sync._raw_message", side_effect=GmailMessageGone("gone")
        ),
    ):
        manifest = gmail_replay.capture(db_session, online=True)

    assert manifest["unavailable"] == [{"owner_id": sample_user.id, "gmail_id": "gone"}]
    assert [entry["sha256"] for entry in manifest["messages"]] == [None]
    assert manifest["ready"] is False


def test_dismissed_only_source_issues_stay_visible_until_acknowledged(
    db_session, app_client
):
    from app.models.enums import DocumentStatus, IngestBatchStatus

    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    batch = ingest(db_session, message(), owner_id=owner.id)
    batch.documents[0].status = DocumentStatus.DISMISSED
    batch.source_review_reasons = ["unresolved_source_group"]
    db_session.commit()
    bundles = get_triage_bundles(db_session, enrich=False, owner_id=owner.id)
    source_only = next(b for b in bundles if b.batch_id == batch.id)
    assert not source_only.documents
    response = app_client.post(f"/api/v1/triage/bundles/{batch.id}/sources/acknowledge")
    assert response.status_code == 200
    db_session.refresh(batch)
    assert batch.status == IngestBatchStatus.CONFIRMED
    assert batch.id not in [
        b.batch_id
        for b in get_triage_bundles(db_session, enrich=False, owner_id=owner.id)
    ]


def test_legacy_migration_preserves_grouping_and_labels_unknown_receipt(
    db_session, app_client
):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from app.services.ingestion.email_sources import provenance
    from app.services.intelligence.batch_analyzer import _analyze_email_sources

    batch = ingest(db_session, message())
    cover = batch.documents[0]
    cover.role = DocumentRole.COVER_LETTER
    child = Document(
        title="Enclosure",
        owner_id=batch.owner_id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        parent_id=cover.id,
        role=DocumentRole.ENCLOSURE,
    )
    db_session.add(child)
    db_session.commit()
    batch_id, cover_id, child_id = batch.id, cover.id, child.id
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/c2e6a4b8d013_email_source_provenance.py"
    )
    spec = importlib.util.spec_from_file_location("source_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        # Old data can contain a manifest but no exact MIME part identities.
        db_session.execute(
            text(
                'UPDATE ingest_batches SET email_note = \'Preserved note\', attachment_manifest = \'[{"filename":"A.pdf","source_label":"Court"}]\'::jsonb WHERE id = :id'
            ),
            {"id": batch_id},
        )
        migration.upgrade()
    db_session.commit()
    db_session.expire_all()
    batch = db_session.get(IngestBatch, batch_id)
    cover, child = (
        db_session.get(Document, cover_id),
        db_session.get(Document, child_id),
    )
    assert cover.email_source_id == child.email_source_id == "0"
    assert provenance(batch).received_source == "legacy_unknown"
    assert batch.email_sources[0]["email_note"] == "Preserved note"
    response = app_client.post(f"/api/v1/triage/bundles/{batch.id}/sources/acknowledge")
    assert response.status_code == 200
    assert response.json()["acknowledged"] is True
    assert "legacy_source_identity" in response.json()["review_reasons"]
    db_session.refresh(batch)
    with patch(
        "app.services.intelligence.batch_analyzer._call_batch_analyzer_sync",
        side_effect=AssertionError("Legacy boundaries must await replay"),
    ):
        assert not _analyze_email_sources(batch, [cover, child], db_session, "unused")
    assert child.parent_id == cover.id
    assert cover.role == DocumentRole.COVER_LETTER
    assert child.role == DocumentRole.ENCLOSURE


@pytest.mark.parametrize("change_case", [False, True])
def test_subject_assignment_preserves_only_a_same_case_proceeding(
    db_session, sample_case, sample_user, change_case
):
    from app.models.database import Case, Proceeding
    from app.models.enums import IngestBatchSourceType, ProceedingCourtLevel
    from app.services.ingestion.batch_orchestrator import _try_assign_case_from_subject

    sample_case.id = "8372-25"
    sample_case.owner_id = sample_user.id
    db_session.flush()
    proceeding = Proceeding(
        case_id=sample_case.id,
        court_name="Test court",
        court_level=ProceedingCourtLevel.AG,
    )
    db_session.add(proceeding)
    db_session.flush()
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        owner_id=sample_user.id,
    )
    db_session.add(batch)
    target_id = sample_case.id
    if change_case:
        target_id = "8373-25"
        target_case = Case(
            id=target_id,
            title="Other case",
            owner_id=sample_user.id,
            jurisdiction=sample_case.jurisdiction,
        )
        db_session.add(target_case)
    db_session.commit()
    _try_assign_case_from_subject(
        db_session,
        batch,
        f"{target_id.replace('-', '/')} Correspondence",
        sample_user.id,
    )
    db_session.commit()
    db_session.refresh(batch)
    assert batch.case_id == target_id
    assert batch.proceeding_id == (None if change_case else proceeding.id)
