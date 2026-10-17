"""Transport provenance shared by ingest, analysis, review and replay."""

import hashlib
import json
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from app.core import secrets
from app.schemas.email_source import EmailProvenance, EmailSource
from app.services.ingestion.email_parser import normalize_filename

LEGACY_REGROUP_SKIP_REASON = "legacy_source_reimport_required"


def needs_source_reimport(batch) -> bool:
    return bool(
        batch and "legacy_source_identity" in (batch.source_review_reasons or [])
    )


def bind_manifests(sources: list[dict]) -> list[str]:
    """Match only unique source-local parts, never an arbitrary duplicate name."""
    reasons = set()
    for source in sources:
        parts = source.get("parts", [])
        for entry in source.get("attachment_manifest", []):
            if entry.get("issue") != "invalid_manifest_timestamp":
                for key in ("issue", "doc_id", "part_id", "group_id"):
                    entry.pop(key, None)
            matches = [
                p
                for p in parts
                if p.get("outcome")
                not in ("ignored_inline", "nested_source", "body_context")
                and normalize_filename(p.get("filename") or "")
                == normalize_filename(entry["filename"])
            ]
            if entry.get("issue"):
                reasons.add(entry["issue"])
                continue
            if len(matches) != 1:
                entry["issue"] = (
                    "ambiguous_manifest_filename"
                    if len(matches) > 1
                    else "unmatched_manifest_entry"
                )
                if not entry.get("historical") or matches:
                    reasons.add(entry["issue"])
                continue
            part = matches[0]
            entry["part_id"] = part["id"]
            ids = part.get("document_ids", [])
            entry["doc_id"] = ids[0] if len(ids) == 1 else None
            group_key = [
                source["id"],
                entry.get("timestamp"),
                normalize_filename(entry["source_label"]),
            ]
            entry["group_id"] = hashlib.sha256(
                json.dumps(group_key).encode()
            ).hexdigest()[:16]
        by_part: dict[str, set[str]] = {}
        for e in source.get("attachment_manifest", []):
            if e.get("part_id") and e.get("group_id"):
                by_part.setdefault(e["part_id"], set()).add(e["group_id"])
        for e in source.get("attachment_manifest", []):
            if len(by_part.get(e.get("part_id"), set())) > 1:
                e["issue"] = "ambiguous_manifest_history"
                reasons.add(e["issue"])
        for part in parts:
            if part["outcome"] in ("empty", "unsupported", "failed"):
                reasons.add("attachment_" + part["outcome"])
    return sorted(reasons)


def persist_sources(sources: list[dict]) -> list[dict]:
    return [EmailSource.model_validate(s).model_dump(mode="json") for s in sources]


def source_groups(batch, docs) -> tuple[dict[int, str], set[int]]:
    """Return hard membership constraints and docs that need human matching."""
    groups, blocked = {}, set()
    sources = {s["id"]: s for s in batch.email_sources or []}
    deliveries: dict[int, set[str]] = {}
    for source in sources.values():
        manifest = source.get("attachment_manifest", [])
        for part in source.get("parts", []):
            matches = [
                entry
                for entry in manifest
                if entry.get("part_id") == part["id"] and not entry.get("issue")
            ]
            keys = {entry["group_id"] for entry in matches if entry.get("group_id")}
            if not manifest:
                keys = {"source:" + source["id"]}
            elif not keys:
                keys = {"unresolved:" + source["id"]}
            for doc_id in part.get("document_ids", []):
                deliveries.setdefault(doc_id, set()).update(keys)
    for doc in docs:
        if len(deliveries.get(doc.id, set())) > 1:
            blocked.add(doc.id)
            continue
        source = sources.get(doc.email_source_id)
        if not source:
            blocked.add(doc.id)
            continue
        manifest = source.get("attachment_manifest", [])
        if manifest and (doc.email_part_id or "").endswith(".body"):
            continue  # The author's own body is independently substantive.
        entries = [e for e in manifest if e.get("part_id") == doc.email_part_id]
        # A source with a manifest has positive membership evidence. Missing,
        # conflicting or malformed links cannot be repaired by guessing.
        if manifest and (not entries or any(e.get("issue") for e in entries)):
            blocked.add(doc.id)
            continue
        keys = {e.get("group_id") for e in entries if e.get("group_id")}
        if manifest and len(keys) != 1:
            blocked.add(doc.id)
            continue
        groups[doc.id] = next(iter(keys)) if keys else "source:" + source["id"]
    return groups, blocked


def provenance(batch) -> EmailProvenance | None:
    if not batch or not batch.email_sources:
        return None
    return EmailProvenance(
        batch_id=batch.id,
        received_at=batch.received_at,
        received_source=batch.received_source or "legacy_unknown",
        gmail_id=batch.gmail_id,
        sources=[EmailSource.model_validate(s) for s in batch.email_sources],
        review_reasons=batch.source_review_reasons or [],
        acknowledged=batch.source_review_acknowledged,
    )


def sync_document_parts(db, old_doc_id: int, new_doc_ids: list[int]) -> None:
    """Slicing/deletion must not leave source inventories pointing at dead rows."""
    # Duplicate deliveries may reference a document belonging to another batch.
    from sqlalchemy import or_

    from app.models.database import Document, IngestBatch

    document = db.get(Document, old_doc_id)
    if document is None:
        return
    batches = (
        db.query(IngestBatch)
        .filter(
            IngestBatch.email_sources.isnot(None),
            IngestBatch.owner_id == document.owner_id,
            or_(
                IngestBatch.email_sources.op("@>")(
                    [{"parts": [{"document_ids": [old_doc_id]}]}]
                ),
                IngestBatch.email_sources.op("@>")(
                    [{"attachment_manifest": [{"doc_id": old_doc_id}]}]
                ),
            ),
        )
        .all()
    )
    for batch in batches:
        changed = False
        for source in batch.email_sources:
            for part in source.get("parts", []):
                if old_doc_id in part.get("document_ids", []):
                    part["document_ids"] = list(
                        dict.fromkeys(
                            [i for i in part["document_ids"] if i != old_doc_id]
                            + new_doc_ids
                        )
                    )
                    if not part["document_ids"]:
                        part.update(
                            outcome="removed", reason="The document was deleted"
                        )
                    changed = True
            for entry in source.get("attachment_manifest", []):
                if entry.get("doc_id") == old_doc_id:
                    entry["doc_id"] = new_doc_ids[0] if len(new_doc_ids) == 1 else None
                    changed = True
        if changed:
            flag_modified(batch, "email_sources")


def raw_source(batch) -> bytes | None:
    from app.core.paths import resolve_storage_path
    from app.services import gmail_cache

    if batch.gmail_id and batch.owner_id is not None:
        cached = gmail_cache.read(batch.owner_id, batch.gmail_id)
        if cached:
            return cached
    if batch.raw_source_path:
        try:
            data = Path(resolve_storage_path(batch.raw_source_path)).read_bytes()
        except OSError:
            return None
        # A key that no longer matches raises SecretsError (a 503 at the API),
        # not "no longer available": the mail is there, the key is not.
        return secrets.decrypt_bytes(data)
    return None
