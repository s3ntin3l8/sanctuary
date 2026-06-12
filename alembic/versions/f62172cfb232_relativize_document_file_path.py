"""relativize document file_path

``Document.file_path`` is now persisted relative to ``DATA_DIR`` so the database
is portable across hosts (the dev box and the deployment have different
``DATA_DIR`` roots). Existing rows were written as host-absolute paths baked to
the dev machine, e.g.::

    /home/bjoern/projects/sanctuary/data/_TRIAGE/1_Schr.pdf

On any other host those absolute paths fall outside that host's ``DATA_DIR`` and
the ``/document/{id}/original`` route returns 404. We cannot ``relative_to`` the
running host's ``DATA_DIR`` (the stored prefix is the *dev* machine's), so we
strip everything up to and including the first ``/data/`` segment, yielding e.g.
``_TRIAGE/1_Schr.pdf``. Idempotent: rows already relative are skipped.

Revision ID: f62172cfb232
Revises: 1193a719f300
Create Date: 2026-06-12 20:46:01.402713

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f62172cfb232"
down_revision: str | Sequence[str] | None = "1193a719f300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DATA_SEG = "/data/"


def relativize(file_path: str) -> str | None:
    """Strip a host-absolute path down to the part after the first ``/data/``.

    Returns the relative remainder, or ``None`` when there is no ``/data/``
    segment to strip (leave the row untouched).
    """
    idx = file_path.find(_DATA_SEG)
    if idx == -1:
        return None
    return file_path[idx + len(_DATA_SEG) :]


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, file_path FROM documents WHERE file_path LIKE '/%'")
    ).fetchall()
    for doc_id, file_path in rows:
        rel = relativize(file_path)
        if rel is None:
            continue
        bind.execute(
            sa.text("UPDATE documents SET file_path = :fp WHERE id = :id"),
            {"fp": rel, "id": doc_id},
        )


def downgrade() -> None:
    # Normalization only — the original per-host absolute prefixes are
    # unrecoverable (pre-release test data). No-op.
    pass
