"""Add composite indexes for query performance

Revision ID: composite_indexes_001
Revises: afa8c6ca0cf2
Create Date: 2026-04-15 20:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "composite_indexes_001"
down_revision: str | Sequence[str] | None = "afa8c6ca0cf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Indexes that may not exist in older databases
INDEXES_TO_CREATE = [
    # Document indexes
    ("ix_documents_case_needs_review", "documents", ["case_id", "needs_review"]),
    ("ix_documents_case_created", "documents", ["case_id", "created_at"]),
    ("ix_documents_needs_review_created", "documents", ["needs_review", "created_at"]),
    # Deadline indexes
    ("ix_deadlines_case_due", "deadlines", ["case_id", "due_at"]),
    ("ix_deadlines_due_completed", "deadlines", ["due_at", "completed"]),
    # Hearing indexes
    ("ix_hearings_case_scheduled", "hearings", ["case_id", "scheduled_for"]),
    # LegalCost indexes
    ("ix_legal_costs_case_status", "legal_costs", ["case_id", "status"]),
    ("ix_legal_costs_status_due", "legal_costs", ["status", "due_at"]),
    # Entity indexes
    ("ix_entities_case_type", "entities", ["case_id", "type"]),
]


def upgrade() -> None:
    """Add composite indexes for common query patterns (idempotent)."""
    for index_name, table_name, columns in INDEXES_TO_CREATE:
        try:
            op.create_index(index_name, table_name, columns, unique=False)
        except Exception:
            pass  # Index may already exist


def downgrade() -> None:
    """Remove composite indexes."""
    for index_name, table_name, columns in reversed(INDEXES_TO_CREATE):
        try:
            op.drop_index(index_name, table_name=table_name)
        except Exception:
            pass  # Index may not exist
