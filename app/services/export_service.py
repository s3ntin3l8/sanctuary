"""GDPR data export: zip all user data (DB rows + document files)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import DATA_DIR
from app.core.timezone import now_utc

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
    """Return (zip_bytes, manifest) for a full data export."""
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

        # --- Manifest ---
        manifest = {
            "export_date": now_utc().date().isoformat(),
            "schema_note": "sanctuary GDPR export",
            "table_counts": table_counts,
            "files_included": files_included,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # --- README ---
        readme = (
            "# Sanctuary Data Export\n\n"
            f"Export date: {now_utc().date().isoformat()}\n\n"
            "## Contents\n"
            "- `manifest.json` — table row counts and export metadata\n"
            "- `data/*.jsonl` — one file per database table, one JSON object per line\n"
            "- `files/` — original document files from the data directory\n\n"
            "## Tables\n"
            + "\n".join(f"- {t}: {table_counts.get(t, 0)} rows" for t in _TABLES)
        )
        zf.writestr("README.md", readme)

    return buf.getvalue(), manifest
