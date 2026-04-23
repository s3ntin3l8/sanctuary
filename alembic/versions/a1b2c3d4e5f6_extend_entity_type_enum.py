"""extend entity type enum with court, law_firm, citation

Revision ID: a1b2c3d4e5f6
Revises: 9dc0989a8342
Branch_labels: None
depends_on: None
"""

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "9dc0989a8342"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.alter_column(
            "type",
            existing_type=sa.Enum(
                "PERSON",
                "ORGANIZATION",
                "DATE",
                "FINANCIAL",
                "LEGAL_CATEGORY",
                name="entitytype",
            ),
            type_=sa.Enum(
                "PERSON",
                "ORGANIZATION",
                "DATE",
                "FINANCIAL",
                "LEGAL_CATEGORY",
                "COURT",
                "LAW_FIRM",
                "CITATION",
                name="entitytype",
            ),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("entities", schema=None) as batch_op:
        batch_op.alter_column(
            "type",
            existing_type=sa.Enum(
                "PERSON",
                "ORGANIZATION",
                "DATE",
                "FINANCIAL",
                "LEGAL_CATEGORY",
                "COURT",
                "LAW_FIRM",
                "CITATION",
                name="entitytype",
            ),
            type_=sa.Enum(
                "PERSON",
                "ORGANIZATION",
                "DATE",
                "FINANCIAL",
                "LEGAL_CATEGORY",
                name="entitytype",
            ),
            existing_nullable=False,
        )
