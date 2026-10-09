"""drop the never-editable az_court confidence; re-derive low_confidence

`az_court` confidence defaulted to "low" for every extraction and nothing on a
document can raise it, so `low_confidence` held nearly every document in review
forever. It is no longer written or read. Strip it from existing rows and clear
`low_confidence` (and `needs_review`, when it was the only reason) where no
remaining tracked field is low/medium.

Downgrade is a no-op: the dropped key carried no information.

Revision ID: c9d1e5a7b3f2
Revises: b4e8c2a6d0f3
Create Date: 2026-10-09 15:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d1e5a7b3f2"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "b4e8c2a6d0f3"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRACKED = ("internal_id", "sender", "issued_date", "originator_type")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, extraction_confidence, review_reasons FROM documents "
            "WHERE extraction_confidence IS NOT NULL"
        )
    ).fetchall()
    for doc_id, conf, reasons in rows:
        conf = conf if isinstance(conf, dict) else json.loads(conf or "{}")
        reasons = reasons if isinstance(reasons, list) else json.loads(reasons or "[]")
        if "az_court" not in conf and "low_confidence" not in reasons:
            continue
        conf.pop("az_court", None)
        if "low_confidence" in reasons and not any(
            conf.get(k) in ("low", "medium") for k in TRACKED
        ):
            reasons = [r for r in reasons if r != "low_confidence"]
        conn.execute(
            sa.text(
                "UPDATE documents SET extraction_confidence = CAST(:conf AS json), "
                "review_reasons = CAST(:reasons AS json), needs_review = :nr "
                "WHERE id = :id"
            ),
            {
                "conf": json.dumps(conf),
                "reasons": json.dumps(reasons),
                "nr": bool(set(reasons) - {"missing_parent"}),
                "id": doc_id,
            },
        )


def downgrade() -> None:
    pass
