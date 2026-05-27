"""5a — Case-level AI brief: posture, pressure_points, next_move."""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy.orm import Session, defer

from app.config import SessionLocal
from app.models.database import ActionItem, Case, Document
from app.models.enums import (
    ActionItemStatus,
    CaseStatus,
    OriginatorType,
)
from app.services.ai_config import get_chat_config
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence._court_identity import (
    is_court_name,
    is_third_party_default_name,
)
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import CASE_BRIEF_SYSTEM
from app.services.intelligence.reaction_context import format_reactions_for_case
from app.services.intelligence.schemas import CaseBrief

logger = logging.getLogger(__name__)

_TRIAGE = "_TRIAGE"


def _normalize_originator(name: str, canonical_names: set[str]) -> str:
    """Normalize an attributed_originator string for consistent party grouping.

    Two transformations, both conservative:
    1. Comma reversal: "Liu, Yingying" → "Yingying Liu" (single-token surname before comma).
    2. Sub-unit collapse: "Landratsamt X, Amt Y" → "Landratsamt X" only when the
       prefix "Landratsamt X" already exists as a distinct originator in this case.
    """
    # Comma reversal: "Nachname, Vorname" → "Vorname Nachname"
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and " " not in parts[0]:
            name = f"{parts[1]} {parts[0]}"

    # Sub-unit collapse: only when the parent is already a known canonical name
    if ", " in name:
        prefix = name.split(", ", 1)[0]
        if prefix in canonical_names:
            name = prefix

    return name


def _canonical_name(
    name: str,
    own_self: str,
    own_parties: list[str],
    opposing_parties: list[str],
) -> str:
    """Return the canonical form of name if it matches a known party, else name itself.

    Handles both "Vorname Nachname" and "Nachname Vorname" orderings so that
    "Liu Yingying" and "Yingying Liu" both resolve to the canonical form stored
    in settings (whichever ordering the user used).
    """
    all_known: list[str] = [n for n in [own_self] + own_parties + opposing_parties if n]

    name_cf = name.casefold()
    for canonical in all_known:
        if canonical.casefold() == name_cf:
            return canonical
        parts = canonical.split()
        if len(parts) == 2:
            reversed_form = f"{parts[1]} {parts[0]}"
            if reversed_form.casefold() == name_cf:
                return canonical
    return name


def _compute_parties(
    docs: list,
    own_self: str = "",
    own_parties: list[str] | None = None,
    opposing_parties: list[str] | None = None,
) -> list[dict]:
    """Aggregate attributed originators from documents into sorted party list.

    Groups by name only — a party has one stable role within a case.
    When a name appears with multiple roles (e.g. COURT on a cover letter
    and OPPOSING on its enclosures), the most frequent non-COURT role wins.

    own_self / own_parties / opposing_parties canonicalise name variants and
    ensure the user's own name appears even when no document has originator_type=OWN.

    Pure function — never calls db.commit().
    """
    own_self = (own_self or "").strip()
    own_parties = own_parties or []
    opposing_parties = opposing_parties or []

    # First pass: collect raw names to enable sub-unit collapse in normalization.
    raw_names = {doc.attributed_originator for doc in docs if doc.attributed_originator}

    role_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for doc in docs:
        if doc.attributed_originator is None:
            continue
        name = _normalize_originator(doc.attributed_originator, raw_names)
        name = _canonical_name(name, own_self, own_parties, opposing_parties)
        role_counts[name][str(doc.originator_type)] += 1

    result = []
    court_role_key = str(OriginatorType.COURT)
    third_party_role_key = str(OriginatorType.THIRD_PARTY)
    for name, counts in role_counts.items():
        total = sum(counts.values())
        # Courts don't switch sides. When the NAME itself identifies the
        # entity as a German court (Amtsgericht, OLG, Verwaltungsgericht,
        # etc.), lock role to COURT — no amount of misclassified docs can
        # flip OLG München into an opposing party. For non-court names,
        # keep the original "discount COURT" heuristic: party documents
        # that misfire as COURT (court Rubrum header) should resolve to
        # the more frequent party role — this is the a73cdcf intent.
        #
        # Round 7: parallel lock for the third_party defaults the metadata
        # system prompt enumerates (Landesjustizkasse, Verfahrensbeistand,
        # banks, notaries, …). Without this, a single misclassified doc
        # (e.g. doc 7 Landesjustizkasse Bamberg voted opposing) propagates
        # into `case.parties` and then feeds back to Phase 1 metadata as
        # "authoritative" via the Known Party Identity block — the prompt
        # rule never wins. Locking here breaks the feedback loop.
        if is_court_name(name):
            canonical_role = court_role_key
        elif is_third_party_default_name(name):
            canonical_role = third_party_role_key
        else:
            non_court = {r: c for r, c in counts.items() if r != court_role_key}
            best_pool = non_court if non_court else counts
            canonical_role = max(best_pool, key=best_pool.get)
        result.append(
            {
                "name": name,
                "role": canonical_role,
                "document_count": total,
            }
        )

    # Ensure the user's own self appears in the party list even when no document
    # carries them as attributed_originator (common when all correspondence arrives
    # through their lawyer).
    if own_self and not any(p["name"] == own_self for p in result):
        result.append(
            {
                "name": own_self,
                "role": str(OriginatorType.OWN),
                "document_count": 0,
            }
        )

    result.sort(key=lambda x: x["document_count"], reverse=True)
    return result


def _apply_brief(case: Case, result: dict, db: Session) -> None:
    """Write AI brief results to the case object (caller commits).

    Applies detected_status to case.status when it changed, cascading
    closure to proceedings when the new status is CLOSED. Mirrors the
    cascade in the PATCH /cases/{case_id} handler.

    Never calls db.commit() — the caller owns the transaction.
    """
    posture = str(result.get("posture", ""))

    pressure_raw = result.get("pressure_points") or []
    pressure_points = [p for p in pressure_raw if isinstance(p, str)]

    next_move = str(result.get("next_move", ""))

    detected_status_raw = result.get("detected_status")
    status_rationale = str(result.get("status_rationale", ""))

    new_status: CaseStatus | None = None
    if detected_status_raw:
        try:
            new_status = CaseStatus(detected_status_raw)
        except ValueError:
            logger.warning(
                f"Case {case.id}: invalid detected_status {detected_status_raw!r}, keeping {case.status}"
            )

    if new_status and new_status != case.status:
        if new_status == CaseStatus.CLOSED:
            # Do NOT close silently — raise a pending suggestion for user confirmation.
            logger.info(
                f"Case {case.id}: AI suggests CLOSED ({status_rationale}) → pending_close=True"
            )
            case.pending_close = True
        else:
            logger.info(
                f"Case {case.id}: status {case.status} → {new_status} ({status_rationale})"
            )
            case.status = new_status

    case.ai_brief = {
        "posture": posture,
        "pressure_points": pressure_points,
        "next_move": next_move,
        "detected_status": str(case.status),
        "status_rationale": status_rationale,
        "close_suggestion_rationale": status_rationale if case.pending_close else "",
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

    def _as_utc(dt: datetime | None) -> datetime:
        if dt is None:
            return datetime.min.replace(tzinfo=UTC)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    proceedings = sorted(case.proceedings, key=lambda p: _as_utc(p.started_at))
    proc_lines = [
        f"- {p.court_level} ({p.status})"
        + (f" — ended {p.ended_at.date()}" if p.ended_at else "")
        for p in proceedings
    ] or ["None"]

    from app.services.intelligence.prompts import sanitize_oneline

    doc_lines = chr(10).join(
        f"- [{d.significance_tier}/{d.document_type}] "
        f"{sanitize_oneline(d.title, 120) or 'Untitled'} "
        f"({d.issued_date}) — by "
        f"{sanitize_oneline(d.attributed_originator, 100) or 'unknown'} "
        f"({d.originator_type})\n"
        f"  Summary: {sanitize_oneline((d.ai_summary or {}).get('legal_significance', 'N/A'), 400)}"
        for d in docs
    )

    action_lines = (
        chr(10).join(
            f"- {sanitize_oneline(a.title, 200)} ({a.action_type}) due {a.due_date}"
            for a in action_items
        )
        or "None"
    )

    prompt = f"""Case: {sanitize_oneline(case.title, 200)} ({case.id}) — current_status: {case.status}
Cost exposure: {case.total_cost_exposure or 0} cents

Proceedings:
{chr(10).join(proc_lines)}

Documents ({len(docs)}):
{doc_lines}

Open action items:
{action_lines}
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
        from app.services.ai_provider import chat_provider

        chat_provider.reload_from_db(db)
        case = db.query(Case).filter(Case.id == case_id).first()
        if not case:
            raise ValueError(f"Case {case_id} not found")

        _mark_processing(case_id, db)

        # Re-query case after commit (expire_on_commit=True means attributes are stale)
        case = db.query(Case).filter(Case.id == case_id).first()
        if not case:
            raise ValueError(f"Case {case_id} not found after marking processing")

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
        from app.services.user_settings_service import get_party_identity

        party_identity = get_party_identity(db)
        own_self: str = party_identity.get("own_self", "")
        own_parties: list[str] = party_identity.get("own_parties", [])
        # Load per-case opposing parties (may be empty before first brief)
        case_opposing: list[str] = case.opposing_parties or []

        try:
            result = _call_brief_sync(
                case,
                docs,
                action_items,
                reactions_context,
                model=cfg.summary_model,
                db=db,
            )
            _apply_brief(case, result, db)

            parties = _compute_parties(
                docs,
                own_self=own_self,
                own_parties=own_parties,
                opposing_parties=case_opposing,
            )
            case.parties = parties

            logger.info(f"Case {case_id} brief generated successfully")
        except Exception as e:
            logger.error(f"Case {case_id} brief generation failed: {e}", exc_info=True)
            case.ai_brief = {"status": "failed", "error": str(e)}

        db.commit()
    finally:
        db.close()
