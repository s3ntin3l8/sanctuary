"""5a — Case-level AI brief: posture, pressure_points, next_move."""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy.orm import Session, defer

from app.config import SessionLocal
from app.models.database import ActionItem, Case, Document
from app.models.enums import ActionItemStatus
from app.services.ai_config import get_chat_config
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import CASE_BRIEF_SYSTEM
from app.services.intelligence.reaction_context import format_reactions_for_case
from app.services.intelligence.schemas import CaseBrief

logger = logging.getLogger(__name__)

_TRIAGE = "_TRIAGE"


def _compute_parties(docs: list) -> list[dict]:
    """Aggregate attributed originators from documents into sorted party list.

    Pure function — never calls db.commit().
    """
    counts: dict[tuple, int] = defaultdict(int)
    roles: dict[tuple, str] = {}

    for doc in docs:
        if doc.attributed_originator is None:
            continue
        key = (doc.attributed_originator, str(doc.originator_type))
        counts[key] += 1
        roles[key] = str(doc.originator_type)

    result = [
        {
            "name": name,
            "role": role,
            "document_count": counts[(name, role)],
        }
        for (name, role), _ in counts.items()
    ]

    result.sort(key=lambda x: x["document_count"], reverse=True)
    return result


def _apply_brief(case: Case, result: dict) -> None:
    """Write AI brief results to the case object (caller commits).

    Pure function — never calls db.commit().
    """
    posture = str(result.get("posture", ""))

    pressure_raw = result.get("pressure_points") or []
    pressure_points = [p for p in pressure_raw if isinstance(p, str)]

    next_move = str(result.get("next_move", ""))

    case.ai_brief = {
        "posture": posture,
        "pressure_points": pressure_points,
        "next_move": next_move,
    }
    case.ai_brief_updated_at = datetime.now(UTC)


def _mark_processing(case_id: str, db: Session) -> None:
    """Set case.ai_brief to processing status and commit."""
    case = db.query(Case).filter(Case.id == case_id).first()
    if case:
        case.ai_brief = {"status": "processing"}
        db.commit()


def _call_brief_sync(
    case: Case,
    docs: list,
    action_items: list,
    reactions_context: str,
    model: str = "",
    db=None,
) -> dict:
    """Synchronous AI call to generate the case brief."""
    prompt = f"""Case: {case.title} ({case.id}) — Status: {case.status}
Cost exposure: {case.total_cost_exposure or 0} cents

Documents ({len(docs)}):
{
        chr(10).join(
            f"- [{d.significance_tier}] {d.title or 'Untitled'} ({d.issued_date}) — by {d.attributed_originator or 'unknown'} ({d.originator_type})\n  Summary: {(d.ai_summary or {}).get('legal_significance', 'N/A')}"
            for d in docs
        )
    }

Open action items:
{
        chr(10).join(
            f"- {a.title} ({a.action_type}) due {a.due_date}" for a in action_items
        )
        or "None"
    }
{("\n" + reactions_context) if reactions_context else ""}"""

    result = call_json_ai(
        system_prompt=CASE_BRIEF_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["case_brief"],
        debug_label=f"case_{case.id}_brief",
        schema=CaseBrief,
        model=model or None,
        db=db,
        case_id=case.id,
        two_pass=True,
    )
    return result.model_dump()


def generate(case_id: str) -> None:
    """Run AI case brief generation for a single case."""
    if case_id == _TRIAGE:
        logger.info("Skipping case brief generation for _TRIAGE")
        return

    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        case = db.query(Case).filter(Case.id == case_id).first()
        if not case:
            logger.warning(f"Case {case_id} not found for brief generation")
            return

        _mark_processing(case_id, db)

        # Re-query case after commit (expire_on_commit=True means attributes are stale)
        case = db.query(Case).filter(Case.id == case_id).first()
        if not case:
            logger.warning(f"Case {case_id} not found after marking processing")
            return

        docs = (
            db.query(Document)
            .options(
                defer(Document.content),
            )
            .filter(Document.case_id == case_id)
            .order_by(Document.issued_date.asc())
            .all()
        )

        action_items = (
            db.query(ActionItem)
            .filter(
                ActionItem.case_id == case_id,
                ActionItem.status == ActionItemStatus.OPEN,
            )
            .order_by(ActionItem.due_date.asc())
            .all()
        )

        reactions_context = format_reactions_for_case(db, case_id)

        try:
            result = _call_brief_sync(
                case,
                docs,
                action_items,
                reactions_context,
                model=cfg.summary_model,
                db=db,
            )
            _apply_brief(case, result)

            parties = _compute_parties(docs)
            case.parties = parties

            logger.info(f"Case {case_id} brief generated successfully")
        except Exception as e:
            logger.error(f"Case {case_id} brief generation failed: {e}", exc_info=True)
            case.ai_brief = {"status": "failed", "error": str(e)}

        db.commit()
    finally:
        db.close()
