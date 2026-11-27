"""Approve, withdraw or discard a document's Management Summary.

Shared by ``POST /documents/{id}/summary`` and the second-opinion confirm of a
``summary`` finding, which replaces the bullets and approves them in one step.
The caller commits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models.database import Document
from app.services.case_dashboard_service import summary_bullets_from_ai_summary


class NoSummary(Exception):
    """There is no summary to approve."""


def approve(doc: Document, replacement: dict[str, Any] | None = None) -> None:
    """Mark the summary as approved, first replacing it by ``replacement``
    (``{legal_significance, required_action, financial_impact}``) when given."""
    if replacement is not None:
        doc.ai_summary = replacement
    if not summary_bullets_from_ai_summary(doc.ai_summary):
        raise NoSummary
    doc.ai_summary_approved_at = datetime.now(UTC)


def unapprove(doc: Document) -> None:
    doc.ai_summary_approved_at = None


def discard(doc: Document) -> None:
    doc.ai_summary = None
    doc.ai_summary_approved_at = None
