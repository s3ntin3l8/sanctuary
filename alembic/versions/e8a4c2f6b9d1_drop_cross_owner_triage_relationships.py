"""drop cross-owner relationships between untriaged documents

Relationship detection used to pick candidates from the shared ``_TRIAGE``
bucket without an owner filter, so an untriaged document could be linked to
(and show the title of) another user's untriaged document. The detector is now
owner-scoped; this removes the edges it already created.

Revision ID: e8a4c2f6b9d1
Revises: a3c9e5b7d1f4
Create Date: 2026-10-08 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8a4c2f6b9d1"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "a3c9e5b7d1f4"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM document_relationships r
        USING documents a, documents b
        WHERE a.id = r.from_document_id
          AND b.id = r.to_document_id
          AND (a.case_id = '_TRIAGE' OR b.case_id = '_TRIAGE')
          AND a.owner_id IS DISTINCT FROM b.owner_id
        """
    )


def downgrade() -> None:
    # The deleted edges linked different users' documents; they are not restored.
    pass
