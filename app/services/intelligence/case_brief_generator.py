"""5a — Case-level AI brief: posture, pressure_points, next_move."""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session, defer

from app.config import DATA_DIR, SessionLocal
from app.core.async_utils import run_async
from app.models.database import ActionItem, Case, Document
from app.models.enums import ActionItemStatus
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.intelligence._json import parse_json_response
from app.services.intelligence.prompts import CASE_BRIEF_SYSTEM
from app.services.intelligence.reaction_context import format_reactions_for_case

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
    debug_file: str,
    model: str = "",
) -> dict:
    """Synchronous AI call to generate the case brief."""
    prompt = f"""Case: {case.title} ({case.id}) — Status: {case.status}
Cost exposure: {case.total_cost_exposure or 0} cents

Documents ({len(docs)}):
{
        chr(10).join(
            f"- [{d.significance_tier}] {d.title or 'Untitled'} ({d.received_date}) — by {d.attributed_originator or 'unknown'} ({d.originator_type})\n  Summary: {(d.ai_summary or {}).get('legal_significance', 'N/A')}"
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

    params = run_async(
        ai_provider.get_generate_params(
            model=model or get_effective_config().summary_model,
            prompt=prompt,
            system_prompt=CASE_BRIEF_SYSTEM,
            stream=True,
            options={
                "num_ctx": 8192,
                "temperature": 0.2,
                "num_predict": 1000,
                "max_tokens": 1000,
            },
        )
    )
    ptype = run_async(ai_provider.get_type())

    full_response = ""
    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        with open(debug_file, "a") as f:
            f.write(f"--- CASE BRIEF case_id={case.id} ---\n")
            f.write(f"Payload: {json.dumps(params['json'])}\n\n")

        with client.stream(
            "POST", params["url"], json=params["json"], headers=params["headers"]
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = ai_provider.parse_stream_line(line, ptype)
                if not chunk:
                    continue
                if "response" in chunk:
                    full_response += chunk["response"]
                if chunk.get("done"):
                    break

        with open(debug_file, "a") as f:
            f.write(f"\n--- END. Length: {len(full_response)} ---\n")

    if not full_response.strip():
        raise ValueError(
            f"Case brief generator returned empty response for case {case.id}"
        )

    return parse_json_response(full_response)


def generate(case_id: str) -> None:
    """Run AI case brief generation for a single case."""
    if case_id == _TRIAGE:
        logger.info("Skipping case brief generation for _TRIAGE")
        return

    db: Session = SessionLocal()
    try:
        cfg = get_effective_config(db)
        ai_provider.reload_from_db(db)
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
            .order_by(Document.received_date.asc())
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

        ts = int(datetime.now().timestamp())
        debug_dir = DATA_DIR / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = str(debug_dir / f"case_{case_id}_{ts}_brief.log")

        try:
            result = _call_brief_sync(
                case,
                docs,
                action_items,
                reactions_context,
                debug_file,
                model=cfg.summary_model,
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
