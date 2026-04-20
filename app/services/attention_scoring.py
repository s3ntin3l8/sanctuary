from datetime import datetime

from app.models.database import ActionItem, IngestBatch
from app.models.enums import ActionItemType, SignificanceTier


def score_action_item(item: ActionItem) -> int:
    """Score an action item based on its urgency and significance.

    Higher score means more urgent/significant.
    """
    now = datetime.now()
    # Ensure due_date is compared correctly (both offset-naive or both offset-aware)
    # Project seems to use offset-naive datetime.now()
    due_date = item.due_date
    if due_date is None:
        return 0

    delta = due_date - now
    days_until = delta.days

    # Overdue items get highest baseline
    if days_until < 0:
        # The more overdue, the higher the score (capped to reasonable limit)
        score = 1000 + min(abs(days_until), 100)
    elif days_until == 0:  # Today
        score = 900
    elif days_until == 1:
        score = 800
    elif days_until <= 7:
        score = 700
    elif days_until <= 14:
        score = 600
    elif days_until <= 30:
        score = 500
    else:
        score = 100

    # Boost by type
    if item.action_type == ActionItemType.DEADLINE:
        score += 50
    elif item.action_type == ActionItemType.COURT_DATE:
        score += 30

    # Boost if source document is critical
    if (
        item.source_document
        and item.source_document.significance_tier == SignificanceTier.CRITICAL
    ):
        score += 20

    return score


def score_triage_batch(batch: IngestBatch) -> int:
    """Score an ingest batch for triage priority.

    Higher score means it should be triaged sooner.
    """
    now = datetime.now()
    age_days = (now - batch.received_at).days

    # Older batches get higher score
    score = age_days * 10

    # More documents = higher priority
    doc_count = len(batch.documents) if batch.documents else 0
    score += doc_count * 5

    # If no case ID suggested, it needs more immediate attention to categorize
    if not batch.case_id:
        score += 50

    return score
