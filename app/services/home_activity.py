"""Recent activity across the caller's cases, derived from timestamps the
pipeline already writes — no event table (issue #176).

Each source is one bounded query ordered newest first; the results are
merged and cut to ``limit`` in Python. Events without a timestamp of their
own (bundle confirmations, action items ticked off) are not represented.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy.orm import InstrumentedAttribute, Query, Session, contains_eager

from app.models.database import (
    ActionItem,
    Case,
    CaseShare,
    Document,
    DocumentPipelineStage,
    LegalCost,
    User,
)
from app.models.enums import ActionItemType, PipelineStage, StageStatus

_T = TypeVar("_T")


def recent_activity(
    db: Session, user: User, visible: Iterable[str] | None, limit: int = 8
) -> list[dict[str, Any]]:
    """Newest-first ``{kind, title, detail, occurred_at, case_id, link}`` rows
    across the cases in ``visible`` (``None`` = every case)."""
    case_ids = None if visible is None else list(visible)
    if case_ids is not None and not case_ids:
        return []

    def scoped(q: Query[_T], column: InstrumentedAttribute[Any]) -> Query[_T]:
        return q if case_ids is None else q.filter(column.in_(case_ids))

    # (kind, title, detail, occurred_at, case_id, link); detail None means
    # "label the case" once titles are resolved below.
    raw: list[tuple[str, str, str | None, datetime | None, str | None, str]] = []

    # Pre-case (triage) documents are not case activity; the "_TRIAGE"
    # sentinel must never surface in the UI.
    cased = (Document.case_id.isnot(None)) & (Document.case_id != "_TRIAGE")

    for d in (
        scoped(db.query(Document), Document.case_id)
        .filter(cased, Document.ingest_date.isnot(None))
        .order_by(Document.ingest_date.desc())
        .limit(limit)
    ):
        raw.append(
            (
                "document_ingested",
                d.title or f"Document #{d.id}",
                None,
                d.ingest_date,
                d.case_id,
                f"/document/{d.id}",
            )
        )

    stages = (
        scoped(
            db.query(DocumentPipelineStage).join(DocumentPipelineStage.document),
            Document.case_id,
        )
        .options(contains_eager(DocumentPipelineStage.document))
        .filter(
            cased,
            (
                (DocumentPipelineStage.stage == PipelineStage.ENRICH)
                & (DocumentPipelineStage.status == StageStatus.COMPLETED)
            )
            | (DocumentPipelineStage.status == StageStatus.FAILED),
            DocumentPipelineStage.completed_at.isnot(None),
        )
        .order_by(DocumentPipelineStage.completed_at.desc())
        .limit(limit)
    )
    for s in stages:
        doc = s.document
        failed = s.status == StageStatus.FAILED
        raw.append(
            (
                "pipeline_failed" if failed else "document_enriched",
                doc.title or f"Document #{doc.id}",
                (f"{s.stage} stage" + (f" · {s.error}" if s.error else ""))
                if failed
                else None,
                s.completed_at,
                doc.case_id,
                f"/document/{doc.id}",
            )
        )

    for a in (
        scoped(db.query(ActionItem), ActionItem.case_id)
        .filter(ActionItem.superseded.is_(False))
        .order_by(ActionItem.ingest_date.desc())
        .limit(limit)
    ):
        hearing = a.action_type == ActionItemType.COURT_DATE
        raw.append(
            (
                "hearing_scheduled" if hearing else "deadline_extracted",
                a.title,
                None,
                a.ingest_date,
                a.case_id,
                f"/cases/{a.case_id}?view=review",
            )
        )

    for c in (
        scoped(db.query(LegalCost), LegalCost.case_id)
        .filter(LegalCost.paid_at.isnot(None))
        .order_by(LegalCost.paid_at.desc())
        .limit(limit)
    ):
        raw.append(("cost_paid", c.title, None, c.paid_at, c.case_id, "/costs"))

    for case in (
        scoped(db.query(Case), Case.id)
        .filter(Case.closed_at.isnot(None))
        .order_by(Case.closed_at.desc())
        .limit(limit)
    ):
        raw.append(
            (
                "case_closed",
                case.title,
                case.id,
                case.closed_at,
                case.id,
                f"/cases/{case.id}",
            )
        )

    for case in (
        scoped(db.query(Case), Case.id)
        .filter(Case.ai_brief_updated_at.isnot(None))
        .order_by(Case.ai_brief_updated_at.desc())
        .limit(limit)
    ):
        raw.append(
            (
                "brief_refreshed",
                case.title,
                case.id,
                case.ai_brief_updated_at,
                case.id,
                f"/cases/{case.id}",
            )
        )

    owned: dict[str, str] = {}
    for cid, title in db.query(Case.id, Case.title).filter(Case.owner_id == user.id):
        owned[cid] = title
    if owned:
        shares = (
            db.query(CaseShare)
            .filter(CaseShare.case_id.in_(list(owned)))
            .order_by(CaseShare.created_at.desc())
            .limit(limit)
            .all()
        )
        names = {
            u.id: u.display_name or u.email
            for u in db.query(User).filter(User.id.in_({sh.user_id for sh in shares}))
        }
        for sh in shares:
            raw.append(
                (
                    "case_shared",
                    owned[sh.case_id],
                    f"shared with {names.get(sh.user_id, 'someone')} · {sh.permission}",
                    sh.created_at,
                    sh.case_id,
                    f"/cases/{sh.case_id}",
                )
            )

    dated = [(r, r[3]) for r in raw if r[3] is not None]
    dated.sort(key=lambda pair: pair[1], reverse=True)
    top = [r for r, _ in dated[:limit]]

    titles = {
        c.id: c.title
        for c in db.query(Case).filter(
            Case.id.in_({r[4] for r in top if r[4] is not None})
        )
    }
    return [
        {
            "kind": kind,
            "title": title,
            "detail": detail
            if detail is not None
            else (f"{case_id} · {titles[case_id]}" if case_id in titles else None),
            "occurred_at": occurred_at,
            "case_id": case_id,
            "link": link,
        }
        for kind, title, detail, occurred_at, case_id, link in top
    ]
