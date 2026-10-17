"""Stored mail is encrypted on disk and still usable by every reader."""

from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app import config
from app.core import secrets
from app.core.paths import resolve_storage_path
from app.services import gmail_cache, gmail_replay
from app.services.ingestion.batch_orchestrator import ingest_raw_email
from app.services.ingestion.email_sources import raw_source

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_replay_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_replay, "manifest_path", lambda: tmp_path / "replay.json")


def _mail(message_id="<rest@example.com>"):
    msg = EmailMessage()
    msg["From"] = "lawyer@example.com"
    msg["To"] = "client@example.com"
    msg["Date"] = "Tue, 26 May 2026 08:00:00 +0200"
    msg["Message-ID"] = message_id
    msg["Subject"] = "Klage gegen Mustermann"
    msg.set_content("Vertraulicher Inhalt.")
    return msg


def _ingest(db, msg, **kwargs):
    with patch("app.services.ingestion.batch_orchestrator.dispatch_task"):
        return ingest_raw_email(db, msg.as_bytes(), **kwargs)


def test_the_stored_eml_is_encrypted_and_readable_through_raw_source(db_session):
    msg = _mail()
    batch = _ingest(db_session, msg)
    on_disk = resolve_storage_path(batch.raw_source_path).read_bytes()
    assert b"Mustermann" not in on_disk
    assert secrets.is_encrypted_bytes(on_disk)
    assert raw_source(batch) == msg.as_bytes()


def test_a_wrong_key_is_an_error_not_a_missing_email(db_session, monkeypatch):
    batch = _ingest(db_session, _mail("<wrongkey@example.com>"))
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with pytest.raises(secrets.SecretsError):
        raw_source(batch)


def test_a_legacy_plaintext_eml_is_still_readable(db_session):
    msg = _mail("<legacy@example.com>")
    batch = _ingest(db_session, msg)
    resolve_storage_path(batch.raw_source_path).write_bytes(msg.as_bytes())
    assert raw_source(batch) == msg.as_bytes()


def test_replay_capture_reads_an_encrypted_cache(db_session, sample_user):
    from app.services.gmail_index_service import upsert_metadata

    msg = _mail("<replay@example.com>")
    sent_at = datetime(2026, 5, 26, tzinfo=UTC)
    gmail_cache.write(sample_user.id, "enc1", msg.as_bytes())
    upsert_metadata(
        db_session,
        sample_user.id,
        [
            {
                "gmail_id": "enc1",
                "thread_id": "t",
                "message_id": msg["Message-ID"],
                "sent_at": sent_at,
                "received_at": sent_at,
                "has_attachments": False,
            }
        ],
    )
    batch = _ingest(
        db_session, msg, owner_id=sample_user.id, mailbox_received_at=sent_at
    )
    # No gmail_id on the batch: capture must find the message by scanning the
    # (encrypted) cache for its Message-ID.
    batch.gmail_id = None
    db_session.commit()
    manifest = gmail_replay.capture(db_session)
    assert [m["gmail_id"] for m in manifest["messages"]] == ["enc1"]
    assert manifest["messages"][0]["sha256"] is not None


def test_the_workspace_export_carries_stored_mail_as_plaintext(db_session, monkeypatch):
    import io
    import zipfile

    from app.services import export_service
    from app.services.export_service import build_export_zip

    monkeypatch.setattr(export_service, "DATA_DIR", config.DATA_DIR)

    msg = _mail("<export@example.com>")
    batch = _ingest(db_session, msg)
    gmail_cache.write(424242, "exp1", msg.as_bytes())
    try:
        data, _ = build_export_zip(db_session)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            eml = next(
                n
                for n in names
                if n.endswith(f"email_sources/{batch.owner_id}/{batch.id}.eml")
            )
            assert b"Mustermann" in archive.read(eml)
            cached = next(n for n in names if n.endswith("gmail_raw/424242/exp1.eml"))
            assert b"Mustermann" in archive.read(cached)
            assert "could not be decrypted" not in archive.read("README.md").decode()
    finally:
        gmail_cache.clear(424242)


def test_the_export_says_so_when_mail_cannot_be_decrypted(db_session, monkeypatch):
    import io
    import zipfile

    from app.services import export_service
    from app.services.export_service import build_export_zip

    monkeypatch.setattr(export_service, "DATA_DIR", config.DATA_DIR)
    gmail_cache.write(424243, "exp2", _mail("<exp2@example.com>").as_bytes())
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    try:
        data, _ = build_export_zip(db_session)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert "could not be decrypted" in archive.read("README.md").decode()
    finally:
        gmail_cache.clear(424243)


def test_replay_capture_skips_a_leftover_temp_file(db_session, sample_user):
    msg = _mail("<tmpfile@example.com>")
    sent_at = datetime(2026, 5, 26, tzinfo=UTC)
    gmail_cache.write(sample_user.id, "good1", msg.as_bytes())
    (gmail_cache.cache_dir(sample_user.id) / ".tmp-abc123.eml").write_bytes(b"partial")
    batch = _ingest(
        db_session, msg, owner_id=sample_user.id, mailbox_received_at=sent_at
    )
    batch.gmail_id = None
    db_session.commit()
    manifest = gmail_replay.capture(db_session)
    assert [m["gmail_id"] for m in manifest["messages"]] == ["good1"]


def test_the_manifest_counts_undecryptable_mail(db_session, monkeypatch):
    import io
    import json
    import zipfile

    from app.services import export_service
    from app.services.export_service import build_export_zip

    monkeypatch.setattr(export_service, "DATA_DIR", config.DATA_DIR)
    gmail_cache.write(424244, "exp3", _mail("<exp3@example.com>").as_bytes())
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    try:
        data, manifest = build_export_zip(db_session)
        assert manifest["undecryptable"] >= 1
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert (
                json.loads(archive.read("manifest.json"))["undecryptable"]
                == manifest["undecryptable"]
            )
    finally:
        gmail_cache.clear(424244)


def test_the_user_export_carries_their_own_raw_mail_only(
    db_session, sample_user, monkeypatch
):
    import io
    import zipfile

    from app.services import export_service
    from app.services.export_service import build_user_export_zip

    monkeypatch.setattr(export_service, "DATA_DIR", config.DATA_DIR)
    own = _ingest(
        db_session, _mail("<own-export@example.com>"), owner_id=sample_user.id
    )
    foreign = _ingest(db_session, _mail("<foreign-export@example.com>"), owner_id=None)
    # Cached but never ingested; another user's cache is none of their business.
    gmail_cache.write(sample_user.id, "loose1", _mail("<loose@example.com>").as_bytes())
    gmail_cache.write(
        sample_user.id + 1000, "theirs", _mail("<t@example.com>").as_bytes()
    )
    try:
        data, manifest = build_user_export_zip(db_session, sample_user)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            assert f"email_sources/{own.id}.eml" in names
            assert b"Mustermann" in archive.read(f"email_sources/{own.id}.eml")
            assert f"email_sources/{foreign.id}.eml" not in names
            assert "gmail_cache/loose1.eml" in names
            assert "gmail_cache/theirs.eml" not in names
        assert manifest["undecryptable"] == 0
    finally:
        gmail_cache.clear(sample_user.id)
        gmail_cache.clear(sample_user.id + 1000)


def test_the_user_export_leaves_out_mail_it_cannot_decrypt(
    db_session, sample_user, monkeypatch
):
    import io
    import zipfile

    from app.services import export_service
    from app.services.export_service import build_user_export_zip

    monkeypatch.setattr(export_service, "DATA_DIR", config.DATA_DIR)
    own = _ingest(
        db_session, _mail("<own-locked@example.com>"), owner_id=sample_user.id
    )
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    data, manifest = build_user_export_zip(db_session, sample_user)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert f"email_sources/{own.id}.eml" not in archive.namelist()
        assert "not included" in archive.read("README.md").decode()
    assert manifest["undecryptable"] >= 1


def test_undecryptable_cache_files_are_counted_and_summarised_once(
    sample_user, monkeypatch, caplog
):
    import logging

    gmail_cache.write(sample_user.id, "rot1", _mail("<rot1@example.com>").as_bytes())
    gmail_cache.write(sample_user.id, "rot2", _mail("<rot2@example.com>").as_bytes())
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    gmail_cache.take_undecryptable()
    try:
        with caplog.at_level(logging.DEBUG, logger="app.services.gmail_cache"):
            assert gmail_cache.read(sample_user.id, "rot1") is None
            assert gmail_cache.read(sample_user.id, "rot2") is None
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        caplog.clear()
        gmail_cache.log_undecryptable_summary("test run")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "2 cached message(s)" in warnings[0].getMessage()
        gmail_cache.log_undecryptable_summary("test run")
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    finally:
        gmail_cache.clear(sample_user.id)
