"""add_fk_documents_case_id_set_null

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-04-29 00:00:00.000000

The Document model declared `case_id` as a plain String — no FK. With
PRAGMA foreign_keys=ON now in effect (set in app.config), every other
case_id column has FK protection but Document.case_id remained a free string.
Deleting a Case would strand Document.case_id pointing at a ghost id.

Add ForeignKey("cases.id", ondelete="SET NULL"). On case delete, the
document falls back to the Triage Inbox (`case_id IS NULL`), matching the
project-wide convention ("no case_id → Triage Inbox").

The pre-step `UPDATE` defensively cleans any orphan case_ids before the FK
constraint is added — without this the migration would fail mid-flight on
real data with orphans (production currently has 0).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b2c3d4e5f6a8"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE documents SET case_id = NULL "
        "WHERE case_id IS NOT NULL "
        "AND case_id NOT IN (SELECT id FROM cases)"
    )

    with op.batch_alter_table("documents") as batch_op:
        batch_op.create_foreign_key(
            "fk_documents_case_id_cases",
            "cases",
            ["case_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch_op:
        batch_op.drop_constraint("fk_documents_case_id_cases", type_="foreignkey")
