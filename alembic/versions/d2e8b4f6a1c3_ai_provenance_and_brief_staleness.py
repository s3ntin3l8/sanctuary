"""Pipeline stages and the case brief record the model and prompt version behind them

Revision ID: d2e8b4f6a1c3
Revises: c5d1f7a3b9e2
Create Date: 2026-11-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e8b4f6a1c3"  # pragma: allowlist secret
down_revision: str | Sequence[str] | None = "c5d1f7a3b9e2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGE_COLUMNS = ("model", "provider", "prompt_version")
_CASE_COLUMNS = ("ai_brief_model", "ai_brief_prompt_version", "ai_brief_inputs_hash")


def upgrade() -> None:
    for name in _STAGE_COLUMNS:
        op.add_column(
            "document_pipeline_stages", sa.Column(name, sa.String(), nullable=True)
        )
    for name in _CASE_COLUMNS:
        op.add_column("cases", sa.Column(name, sa.String(), nullable=True))


def downgrade() -> None:
    for name in reversed(_CASE_COLUMNS):
        op.drop_column("cases", name)
    for name in reversed(_STAGE_COLUMNS):
        op.drop_column("document_pipeline_stages", name)
