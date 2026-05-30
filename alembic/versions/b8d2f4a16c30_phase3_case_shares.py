"""Phase 3 — case sharing: case_shares table.

Adds the join table that grants non-owner users viewer/editor access to a case.

Revision ID: b8d2f4a16c30
Revises: 9f3a1c7e5b20
Create Date: 2026-05-29
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d2f4a16c30"
down_revision = "9f3a1c7e5b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "case_shares",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "case_id",
            sa.String(),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("permission", sa.String(), nullable=False),
        sa.Column(
            "granted_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "user_id", name="uq_case_shares_case_user"),
    )
    op.create_index("ix_case_shares_case", "case_shares", ["case_id"])
    op.create_index("ix_case_shares_user", "case_shares", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_case_shares_user", table_name="case_shares")
    op.drop_index("ix_case_shares_case", table_name="case_shares")
    op.drop_table("case_shares")
