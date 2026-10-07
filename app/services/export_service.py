"""GDPR data export as zips of DB rows (JSON Lines) plus document files.

Two scopes: :func:`build_export_zip` dumps the whole workspace (admin
maintenance), :func:`build_user_export_zip` is the Art. 15/20 subject-access
copy — only what the requesting user owns.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.core.timezone import now_utc
from app.models.database import (
    ActionItem,
    AuditLog,
    Base,
    Case,
    CaseShare,
    Claim,
    ClaimEvidence,
    Conversation,
    ConversationMessage,
    CostSignal,
    Document,
    DocumentPin,
    DocumentRelationship,
    Entity,
    IngestBatch,
    LegalCost,
    Proceeding,
    User,
    UserReaction,
    UserSettings,
)

# All user-data tables in export order (avoids FK ordering concerns for import).
_TABLES = [
    "user_settings",
    "cases",
    "proceedings",
    "ingest_batches",
    "documents",
    "document_relationships",
    "entities",
    "claims",
    "claim_evidence",
    "action_items",
    "legal_costs",
    "user_reactions",
    "document_pins",
    "conversations",
    "conversation_messages",
    "audit_logs",
]


def build_export_zip(db: Session) -> tuple[bytes, dict]:
    """Return (zip_bytes, manifest) for a whole-workspace export (admin)."""
    buf = io.BytesIO()
    table_counts: dict[str, int] = {}
    files_included = 0

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # --- DB tables ---
        for table in _TABLES:
            try:
                # Postgres aborts the whole transaction on any failed statement
                # (unlike SQLite, which just fails that one query) — a
                # SAVEPOINT scopes the failure to this table alone so a
                # missing/renamed table doesn't poison every query after it.
                with db.begin_nested():
                    rows = db.execute(text(f"SELECT * FROM {table}")).mappings().all()  # noqa: S608 — table names are literals
            except Exception:
                continue
            table_counts[table] = len(rows)
            lines = "\n".join(json.dumps(dict(row), default=str) for row in rows)
            zf.writestr(f"data/{table}.jsonl", lines)

        # --- Document files ---
        data_dir = Path(DATA_DIR)
        if data_dir.exists():
            for f in data_dir.rglob("*"):
                if (
                    f.is_file()
                    and not f.name.endswith(".db")
                    and not f.name.endswith(".db-wal")
                    and not f.name.endswith(".db-shm")
                ):
                    rel = f.relative_to(data_dir)
                    try:
                        zf.write(f, arcname=f"files/{rel}")
                        files_included += 1
                    except (PermissionError, OSError):
                        pass

        _write_manifest(zf, "workspace", table_counts, files_included)

    return buf.getvalue(), manifest_of("workspace", table_counts, files_included)


def manifest_of(scope: str, table_counts: dict[str, int], files_included: int) -> dict:
    return {
        "export_date": now_utc().date().isoformat(),
        "schema_note": "sanctuary GDPR export",
        "scope": scope,
        "table_counts": table_counts,
        "files_included": files_included,
    }


def _write_manifest(
    zf: zipfile.ZipFile, scope: str, table_counts: dict[str, int], files_included: int
) -> None:
    zf.writestr(
        "manifest.json",
        json.dumps(manifest_of(scope, table_counts, files_included), indent=2),
    )
    what = (
        "original document files from the data directory"
        if scope == "workspace"
        else "the original files of the documents listed in data/documents.jsonl"
    )
    readme = (
        "# Sanctuary Data Export\n\n"
        f"Export date: {now_utc().date().isoformat()}\n"
        f"Scope: {scope}\n\n"
        "## Contents\n"
        "- `manifest.json` — table row counts and export metadata\n"
        "- `data/*.jsonl` — one file per database table, one JSON object per line\n"
        f"- `files/` — {what}\n\n"
        "## Tables\n" + "\n".join(f"- {t}: {n} rows" for t, n in table_counts.items())
    )
    zf.writestr("README.md", readme)


# --- per-user export -----------------------------------------------------------

REDACTED = "[redacted]"
_CaseScoped = TypeVar(
    "_CaseScoped", Proceeding, Entity, ActionItem, LegalCost, CostSignal
)
_SETTINGS_SECRETS = ("gmail_credentials_json",)


def _row(obj: Base, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        c.name: getattr(obj, c.name)
        for c in obj.__table__.columns
        if c.name not in drop
    }


def _redact_settings(row: dict[str, Any]) -> dict[str, Any]:
    settings = row.get("settings_json")
    if isinstance(settings, dict):
        row["settings_json"] = {
            k: (REDACTED if k in _SETTINGS_SECRETS and v else v)
            for k, v in settings.items()
        }
    return row


def build_user_export_zip(db: Session, user: User) -> tuple[bytes, dict]:
    """Return (zip_bytes, manifest) with only the rows ``user`` owns.

    Owned means: the user's own account rows, the cases they own (and every
    row hanging off those cases), the triage documents and batches they
    ingested, and the reactions, pins, conversations and audit entries they
    authored. Cases merely shared with the user belong to someone else and
    are left out. OAuth credentials are redacted even from the user's own
    settings row."""
    case_ids = [c.id for c in db.query(Case).filter(Case.owner_id == user.id)]
    docs = (
        db.query(Document)
        .filter(
            or_(
                Document.case_id.in_(case_ids),
                # Pre-case documents (triage) are visible by owner only; they
                # carry "_TRIAGE" or no case id at all (see the invariant on
                # Document.owner_id and access_guards._NO_CASE).
                or_(Document.case_id.is_(None), Document.case_id == "_TRIAGE")
                & (Document.owner_id == user.id),
            )
        )
        .all()
    )
    doc_ids = [d.id for d in docs]
    evidence = (
        db.query(ClaimEvidence).filter(ClaimEvidence.document_id.in_(doc_ids)).all()
    )
    claim_ids = sorted({e.claim_id for e in evidence})

    def by_case(model: type[_CaseScoped]) -> list[_CaseScoped]:
        return db.query(model).filter(model.case_id.in_(case_ids)).all()

    settings_row = (
        db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    )
    tables: dict[str, list[dict[str, Any]]] = {
        "users": [_row(user, drop=("password_hash",))],
        "user_settings": [_redact_settings(_row(settings_row))] if settings_row else [],
        "cases": [_row(c) for c in db.query(Case).filter(Case.id.in_(case_ids))],
        "case_shares": [
            _row(s)
            for s in db.query(CaseShare).filter(
                or_(CaseShare.case_id.in_(case_ids), CaseShare.user_id == user.id)
            )
        ],
        "proceedings": [_row(p) for p in by_case(Proceeding)],
        "ingest_batches": [
            _row(b)
            for b in db.query(IngestBatch).filter(
                or_(IngestBatch.case_id.in_(case_ids), IngestBatch.owner_id == user.id)
            )
        ],
        "documents": [_row(d) for d in docs],
        "document_relationships": [
            _row(r)
            for r in db.query(DocumentRelationship).filter(
                DocumentRelationship.from_document_id.in_(doc_ids),
                DocumentRelationship.to_document_id.in_(doc_ids),
            )
        ],
        "entities": [_row(e) for e in by_case(Entity)],
        "claims": [_row(c) for c in db.query(Claim).filter(Claim.id.in_(claim_ids))],
        "claim_evidence": [_row(e) for e in evidence],
        "action_items": [_row(a) for a in by_case(ActionItem)],
        "legal_costs": [_row(c) for c in by_case(LegalCost)],
        "cost_signals": [_row(c) for c in by_case(CostSignal)],
        "user_reactions": [
            _row(r)
            for r in db.query(UserReaction).filter(UserReaction.user_id == user.id)
        ],
        "document_pins": [
            _row(p)
            for p in db.query(DocumentPin).filter(DocumentPin.user_id == user.id)
        ],
    }
    conversations = db.query(Conversation).filter(Conversation.user_id == user.id).all()
    tables["conversations"] = [_row(c) for c in conversations]
    tables["conversation_messages"] = [
        _row(m)
        for m in db.query(ConversationMessage).filter(
            ConversationMessage.conversation_id.in_([c.id for c in conversations])
        )
    ]
    tables["audit_logs"] = [
        _row(a) for a in db.query(AuditLog).filter(AuditLog.actor_user_id == user.id)
    ]

    buf = io.BytesIO()
    files_included = 0
    data_dir = Path(DATA_DIR).resolve()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for table, rows in tables.items():
            zf.writestr(
                f"data/{table}.jsonl",
                "\n".join(json.dumps(r, default=str) for r in rows),
            )
        for d in docs:
            if not d.file_path:
                continue
            path = Path(d.file_path)
            if not path.is_absolute():
                path = data_dir / path
            path = path.resolve()
            # Only files under the data directory; a stray absolute path
            # elsewhere is not the user's document.
            if not path.is_file() or not path.is_relative_to(data_dir):
                continue
            zf.write(path, arcname=f"files/{path.relative_to(data_dir)}")
            files_included += 1
        table_counts = {t: len(rows) for t, rows in tables.items()}
        _write_manifest(zf, "user", table_counts, files_included)

    return buf.getvalue(), manifest_of("user", table_counts, files_included)
