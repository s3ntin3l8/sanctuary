"""Capture and reconcile the exact previous Gmail import set before a test reset.

The manifest contains IDs/hashes, never email bodies or OAuth credentials.
It lives outside disposable case artifacts and survives clear_all_data.
"""

import hashlib
import json
import os
from datetime import datetime
from email.parser import BytesHeaderParser
from email.policy import default
from pathlib import Path

from app import config
from app.core.timezone import ensure_utc, now_utc
from app.models.database import GmailMessageIndex, IngestBatch, UserSettings
from app.models.enums import IngestBatchSourceType
from app.services import gmail_cache, gmail_index_service
from app.services.ingestion.gmail import (
    GmailMessageGone,
    GmailMessageTooLarge,
    connect_gmail,
    fetch_metadata,
    parse_metadata,
)


def manifest_path() -> Path:
    return config.DATA_DIR / "gmail_replay" / "previous_imports.json"


def capture(db, *, online: bool = False, replace: bool = False) -> dict:
    """Refuse a destructive replay when identities or source bytes are missing."""
    from app.tasks.gmail_sync import _raw_message

    path = manifest_path()
    if path.exists() and not replace:
        existing = json.loads(path.read_text())
        if existing.get("ready"):
            raise ValueError(
                "A ready replay manifest already exists; preserve it or explicitly replace the capture"
            )

    selected = set()
    unresolved = []
    batches = (
        db.query(IngestBatch)
        .filter(IngestBatch.source_type == IngestBatchSourceType.EMAIL)
        .all()
    )
    for batch in batches:
        candidates = (
            [batch.gmail_id]
            if batch.gmail_id
            else [
                row.gmail_id
                for row in db.query(GmailMessageIndex)
                .filter(
                    GmailMessageIndex.owner_id == batch.owner_id,
                    GmailMessageIndex.message_id == batch.message_id,
                )
                .all()
            ]
            if batch.message_id
            else []
        )
        if not candidates and batch.owner_id is not None:
            for path in gmail_cache.cache_dir(batch.owner_id).glob("*.eml"):
                try:
                    cached_raw = gmail_cache.read(batch.owner_id, path.stem)
                except ValueError:  # a leftover temp file, not a message id
                    continue
                if cached_raw is None:
                    continue
                header = BytesHeaderParser(policy=default).parsebytes(cached_raw)
                if (
                    batch.message_id
                    and str(header.get("Message-ID", "")).strip() == batch.message_id
                ) or (
                    batch.source_hash
                    and hashlib.sha256(cached_raw).hexdigest() == batch.source_hash
                ):
                    candidates.append(path.stem)
        if len(candidates) != 1 or batch.owner_id is None:
            unresolved.append(batch.id)
        else:
            selected.add((batch.owner_id, candidates[0]))
    services = {}

    def service(owner):
        if owner not in services:
            settings = (
                db.query(UserSettings).filter(UserSettings.user_id == owner).one()
            )
            services[owner] = connect_gmail(db, owner, settings.settings_json or {})
        return services[owner]

    entries, unavailable = [], []
    for owner, gmail_id in sorted(selected):
        raw = gmail_cache.read(owner, gmail_id)
        if raw is None and online:
            try:
                raw = _raw_message(owner, gmail_id, lambda owner=owner: service(owner))
            except (GmailMessageGone, GmailMessageTooLarge):
                raw = None  # deleted in Gmail since it was listed, or too large to hold
        row = (
            db.query(GmailMessageIndex)
            .filter(
                GmailMessageIndex.owner_id == owner,
                GmailMessageIndex.gmail_id == gmail_id,
            )
            .first()
        )
        if online and (row is None or row.received_at is None):
            metas = [
                parse_metadata(m) for m in fetch_metadata(service(owner), [gmail_id])
            ]
            gmail_index_service.upsert_metadata(db, owner, metas)
            db.commit()
            if row:
                db.refresh(row)
            else:
                row = (
                    db.query(GmailMessageIndex)
                    .filter(
                        GmailMessageIndex.owner_id == owner,
                        GmailMessageIndex.gmail_id == gmail_id,
                    )
                    .first()
                )
        metadata = gmail_cache.read_metadata(owner, gmail_id) or {}
        arrival = row.received_at if row else None
        arrival = arrival or (
            datetime.fromisoformat(metadata["received_at"])
            if metadata.get("received_at")
            else None
        )
        thread = row.thread_id if row else metadata.get("thread_id")
        if not raw or not arrival or not thread:
            unavailable.append({"owner_id": owner, "gmail_id": gmail_id})
        entries.append(
            {
                "owner_id": owner,
                "gmail_id": gmail_id,
                "sha256": hashlib.sha256(raw).hexdigest() if raw else None,
                "received_at": arrival.isoformat() if arrival else None,
                "sent_at": row.sent_at.isoformat() if row else metadata.get("sent_at"),
                "thread_id": thread,
            }
        )
    entries.sort(
        key=lambda entry: (
            entry["owner_id"],
            ensure_utc(
                datetime.fromisoformat(
                    entry.get("sent_at")
                    or entry.get("received_at")
                    or "0001-01-01T00:00:00+00:00"
                )
            ),
            entry["gmail_id"],
        )
    )
    manifest = {
        "version": 1,
        "captured_at": now_utc().isoformat(),
        "messages": entries,
        "unresolved_batch_ids": unresolved,
        "unavailable": unavailable,
        "ready": not unresolved and not unavailable,
    }
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(manifest, handle, indent=2)
    os.replace(tmp, path)
    gmail_cache.log_undecryptable_summary("Gmail replay capture")
    return manifest


def load_manifest() -> dict:
    value = json.loads(manifest_path().read_text())
    if value.get("version") != 1 or not value.get("ready"):
        raise ValueError(
            "Replay capture has unresolved identities, bytes, or mailbox metadata; do not clear the workspace"
        )
    for entry in value["messages"]:
        raw = gmail_cache.read(entry["owner_id"], entry["gmail_id"])
        if raw is None or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(
                "Replay cache changed or is incomplete; do not clear the workspace"
            )
    return value


def replay(db) -> dict:
    from app.services.ingestion.batch_orchestrator import ingest_raw_email

    manifest = load_manifest()
    failed = []
    settings = {}
    for entry in manifest["messages"]:
        owner = entry["owner_id"]
        if owner not in settings:
            row = db.query(UserSettings).filter(UserSettings.user_id == owner).first()
            settings[owner] = row.settings_json if row else {}
        try:
            raw = gmail_cache.read(owner, entry["gmail_id"])
            if raw is None:
                raise ValueError("Cached message disappeared during replay")
            ingest_raw_email(
                db,
                raw,
                owner_id=owner,
                own_addresses=settings[owner].get("gmail_own_addresses", []),
                gmail_id=entry["gmail_id"],
                gmail_thread_id=entry["thread_id"],
                mailbox_received_at=datetime.fromisoformat(entry["received_at"]),
            )
        except Exception:
            db.rollback()
            failed.append({"owner_id": owner, "gmail_id": entry["gmail_id"]})
    gmail_cache.log_undecryptable_summary("Gmail replay")
    return {**verify(db), "import_errors": failed}


def verify(db) -> dict:
    manifest = load_manifest()
    counts = {
        "selected": len(manifest["messages"]),
        "completed": 0,
        "processing": 0,
        "duplicate_only": 0,
        "review_required": 0,
        "missing": 0,
        "failed": 0,
    }
    for entry in manifest["messages"]:
        batch = (
            db.query(IngestBatch)
            .filter(
                IngestBatch.owner_id == entry["owner_id"],
                IngestBatch.gmail_id == entry["gmail_id"],
            )
            .first()
        )
        if not batch:
            counts["missing"] += 1
        elif any(d.pipeline_state.value == "failed" for d in batch.documents):
            counts["failed"] += 1
        elif batch.received_source != "gmail" or (
            batch.source_review_reasons and not batch.source_review_acknowledged
        ):
            counts["review_required"] += 1
        elif not batch.documents:
            counts["duplicate_only"] += 1
        elif any(
            d.pipeline_state.value in ("pending", "running", "partial")
            for d in batch.documents
        ):
            counts["processing"] += 1
        else:
            counts["completed"] += 1
    return counts
