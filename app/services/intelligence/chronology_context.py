"""Build a prompt-ready chronology of a case from the timeline service.

The timeline is a pure function over durable rows (documents, action items,
costs, proceedings), so nothing needs persisting — but neither the case brief
nor case chat used to see it. This module renders it as a dated, oldest-first
list so the model can reason about *sequence*: what is still open past its due
date, which deadlines were met, how long the case went quiet.

Every line is a recorded fact; an overdue item is "open, past its due date",
never "missed" — the user may simply not have ticked it off.
"""

from sqlalchemy.orm import Session

from app.services.case_timeline_service import CaseTimelineService, TimelineEvent
from app.services.intelligence.prompts import sanitize_oneline

# A silence shorter than this is not worth a line.
_SILENCE_DAYS = 30

# Keep-priority when the chronology exceeds the budget; lower is kept first.
_PRIO_ESSENTIAL = 0  # deadlines/hearings, milestones, critical documents
_PRIO_SIGNIFICANT = 1
_PRIO_PAYMENT = 2
_PRIO_MINOR = 3


def _priority(ev: TimelineEvent) -> int:
    if ev.kind in ("hearing", "deadline", "milestone") or ev.sig == "critical":
        return _PRIO_ESSENTIAL
    if ev.sig == "significant":
        return _PRIO_SIGNIFICANT
    if ev.kind == "payment":
        return _PRIO_PAYMENT
    return _PRIO_MINOR


def _flag(ev: TimelineEvent, today) -> str:
    if ev.kind in ("hearing", "deadline"):
        if ev.status == "completed":
            return "done "
        if ev.is_overdue:
            days = (today.date() - ev.date.date()).days
            label = "date passed" if ev.kind == "hearing" else "past due"
            return f"OVERDUE (open, {days} days {label}) "
        if ev.is_future:
            return "UPCOMING "
        return ""
    if ev.kind == "payment":
        amount = f"€{ev.amount_eur:.2f} " if ev.amount_eur is not None else ""
        return f"{ev.direction or 'debit'} {amount}"
    if ev.sig and ev.sig != "milestone":
        return f"[{ev.sig}] "
    return ""


def _line(ev: TimelineEvent, today) -> str:
    doc = f" [DOC:{ev.source_document_id}]" if ev.source_document_id else ""
    note = (
        f" — {sanitize_oneline(ev.note, 150)}"
        if ev.note and ev.kind in ("hearing", "deadline")
        else ""
    )
    return (
        f"  {ev.date.date()}  {ev.actor:<8} {ev.kind:<9} "
        f"{_flag(ev, today)}{sanitize_oneline(ev.title, 100)}{note}{doc}"
    )


def format_chronology_for_case(
    db: Session, case_id: str, *, max_events: int = 60
) -> str:
    """Return the case's events as a dated, oldest-first block.

    Returns an empty string for a case with no dated events (safe to skip from
    a prompt). Dismissed action items are left out. Over `max_events`, the
    lowest-priority events go first (minor documents, payments, then oldest
    significant documents; deadlines, milestones and critical documents last)
    and a footer counts what was omitted.
    """
    payload = CaseTimelineService(db).build_payload(case_id)
    today = payload["today"]
    events = [e for e in payload["events"] if e.status != "dismissed"]
    if not events:
        return ""

    # Silences are measured on the full past sequence, before any trimming.
    gap_before: dict[str, int] = {}
    past = [e for e in events if not e.is_future]
    for prev, cur in zip(past, past[1:], strict=False):
        days = (cur.date - prev.date).days
        if days >= _SILENCE_DAYS:
            gap_before[cur.id] = days

    kept = events
    if len(events) > max_events:
        ranked = sorted(events, key=lambda e: (_priority(e), -e.date.timestamp()))
        keep_ids = {e.id for e in ranked[:max_events]}
        kept = [e for e in events if e.id in keep_ids]

    lines = ["Case chronology (oldest first):"]
    for ev in kept:
        if ev.id in gap_before:
            lines.append(f"  … {gap_before[ev.id]} days without activity")
        lines.append(_line(ev, today))
    if len(kept) < len(events):
        lines.append(f"  (+{len(events) - len(kept)} lower-priority events omitted)")
    return "\n".join(lines)
