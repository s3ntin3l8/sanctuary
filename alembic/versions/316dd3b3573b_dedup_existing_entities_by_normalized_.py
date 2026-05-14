"""dedup existing entities by normalized name

Revision ID: 316dd3b3573b
Revises: dbfeb8b8bc2f
Create Date: 2026-05-14 16:19:46.044838

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "316dd3b3573b"
down_revision: str | Sequence[str] | None = "dbfeb8b8bc2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Collapse duplicate Entity rows that share (case_id, type, normalized_name).

    Keeps the row with the LONGEST original `name` (preserves diacritics and
    full honorifics over folded/stripped variants); deletes the others.
    """
    from collections import defaultdict

    from app.models.enums import EntityType
    from app.services.normalization import normalize_entity_name

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, case_id, type, name FROM entities")
    ).fetchall()

    # group by (case_id, type, canonical) → list[(id, name)]
    groups: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        try:
            et = EntityType[row.type]  # SAEnum stores uppercase NAME
        except KeyError:
            continue
        canonical = normalize_entity_name(row.name or "", et)
        if not canonical:
            continue
        groups[(row.case_id, row.type, canonical)].append((row.id, row.name or ""))

    delete_ids: list[int] = []
    for members in groups.values():
        if len(members) <= 1:
            continue
        # Keep the longest original name (preserves "Björn" over "Bjoern").
        members.sort(key=lambda m: (-len(m[1]), m[0]))
        delete_ids.extend(mid for mid, _ in members[1:])

    if delete_ids:
        for i in range(0, len(delete_ids), 500):
            chunk = delete_ids[i : i + 500]
            conn.execute(
                sa.text(
                    "DELETE FROM entities WHERE id IN ("
                    + ",".join(str(x) for x in chunk)
                    + ")"
                )
            )


def downgrade() -> None:
    """Data merge — no automatic downgrade."""
    pass
