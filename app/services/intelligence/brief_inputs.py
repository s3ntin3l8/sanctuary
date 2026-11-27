"""Fingerprint of what a case brief is written from, to tell a stale brief.

``case_brief_generator.generate`` stores the hash next to the brief;
``case_overview_service.serialize_brief`` recomputes it and reports the brief
stale when they differ. It covers what the brief prompt reads and a user can
change: each document's classification and summary, claims (text and
status), proceedings (court, Aktenzeichen, status), action items and costs
(the chronology the prompt reads), reactions and the cost exposure. The
case's own status is left out, since the brief itself sets it; so are
entities, which are only ever added by extraction alongside a document change
that already moves the hash.
"""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import (
    ActionItem,
    Case,
    Document,
    LegalCost,
    Proceeding,
    UserReaction,
)
from app.repositories.claim import ClaimRepository


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def brief_inputs_hash(db: Session, case: Case) -> str:
    docs = db.execute(
        select(
            Document.id,
            Document.title,
            Document.significance_tier,
            Document.document_type,
            Document.issued_date,
            Document.attributed_originator,
            Document.originator_type,
            Document.ai_summary,
            Document.ai_summary_approved_at,
        )
        .where(Document.case_id == case.id)
        .order_by(Document.id)
    ).all()
    proceedings = db.execute(
        select(
            Proceeding.id,
            Proceeding.court_name,
            Proceeding.court_level,
            Proceeding.az_court,
            Proceeding.status,
            Proceeding.started_at,
            Proceeding.ended_at,
        )
        .where(Proceeding.case_id == case.id)
        .order_by(Proceeding.id)
    ).all()
    actions = db.execute(
        select(
            ActionItem.id,
            ActionItem.title,
            ActionItem.action_type,
            ActionItem.due_date,
            ActionItem.status,
        )
        .where(ActionItem.case_id == case.id)
        .order_by(ActionItem.id)
    ).all()
    costs = db.execute(
        select(
            LegalCost.id,
            LegalCost.status,
            LegalCost.amount_gross,
            LegalCost.amount_paid,
        )
        .where(LegalCost.case_id == case.id)
        .order_by(LegalCost.id)
    ).all()
    reactions = db.execute(
        select(UserReaction.id, UserReaction.reaction, UserReaction.notes)
        .join(Document, Document.id == UserReaction.document_id)
        .where(Document.case_id == case.id)
        .order_by(UserReaction.id)
    ).all()
    claims = sorted(
        (c.id, c.claim_text, str(c.status))
        for c in ClaimRepository(db).claims_for_case(case.id)
    )
    payload = {
        "documents": [
            [
                d.id,
                d.title,
                str(d.significance_tier) if d.significance_tier else None,
                str(d.document_type) if d.document_type else None,
                _iso(d.issued_date),
                d.attributed_originator,
                str(d.originator_type) if d.originator_type else None,
                (d.ai_summary or {}).get("legal_significance"),
                _iso(d.ai_summary_approved_at),
            ]
            for d in docs
        ],
        "proceedings": [
            [
                p.id,
                p.court_name,
                str(p.court_level),
                p.az_court,
                str(p.status),
                _iso(p.started_at),
                _iso(p.ended_at),
            ]
            for p in proceedings
        ],
        "action_items": [
            [a.id, a.title, str(a.action_type), _iso(a.due_date), str(a.status)]
            for a in actions
        ],
        "costs": [[c.id, str(c.status), c.amount_gross, c.amount_paid] for c in costs],
        "reactions": [[r.id, str(r.reaction), r.notes] for r in reactions],
        "claims": claims,
        "cost_exposure": case.total_cost_exposure or 0,
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()
