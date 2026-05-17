import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.timezone import to_naive
from app.models.database import ActionItem, Case, Document, LegalCost, Proceeding
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    AuditEventType,
    CaseStatus,
    CaseType,
    CostStatus,
    Jurisdiction,
    ProceedingCourtLevel,
    ProceedingStatus,
    SignificanceTier,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.case import CaseRepository
from app.repositories.document import DocumentRepository
from app.repositories.entity import EntityRepository
from app.repositories.legal_cost import LegalCostRepository
from app.services import audit_service
from app.services.fees.calculator import (
    allocation_from_ruling,
    court_fees,
    default_allocation,
    lawyer_fees,
)

logger = logging.getLogger(__name__)

_TRIAGE = "_TRIAGE"


def get_case_opposing_parties(case_id: str, db: Session) -> list[str]:
    """Return the per-case opposing party list, or [] for _TRIAGE / missing case."""
    if not case_id or case_id == _TRIAGE:
        return []
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case or not case.opposing_parties:
        return []
    return [p for p in case.opposing_parties if p and str(p).strip()]


def set_case_opposing_parties(case_id: str, parties: list[str], db: Session) -> None:
    """Persist the per-case opposing party list. Caller must commit."""
    if not case_id or case_id == _TRIAGE:
        return
    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return
    case.opposing_parties = [p.strip() for p in parties if p and str(p).strip()]
    db.flush()


def seed_triage_case(db: Session) -> None:
    """Idempotently seed the `_TRIAGE` singleton Case.

    Many ingest paths set `case_id="_TRIAGE"` on Documents and IngestBatches.
    With FK enforcement on, that row must exist before any such insert.
    """
    if db.query(Case).filter_by(id="_TRIAGE").first() is not None:
        return
    db.add(
        Case(
            id="_TRIAGE",
            title="Triage Inbox",
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
        )
    )
    db.commit()


DORMANCY_DAYS = 90


# Canonical title shapes:
#   "Lastname1 ./. Lastname2 - Matter"          (two parties + matter)
#   "Matter - Lastname"                         (one party + matter)
#   "Matter"                                    (matter only)
#   any of the above + " (eA)"                  (einstweilige Anordnung)
#
# The normalizer below converts common drift patterns INTO this canonical
# form. Pre-existing titles produced under earlier rules (e.g. parens around
# the matter, or comma-eA suffix) get rewritten on the next AI hit via the
# draft-refresh path in get_or_create_case_from_reference.

# Matches "X ./. Y (Matter)" where the parens contain the matter (not just
# the eA marker). Captures: parties, matter.
_PAREN_MATTER_RE = re.compile(r"^(.+?\s+\./\.\s+.+?)\s+\((?!eA\)$)([^()]+?)\)\s*$")
# Matches a leading internal_id-like prefix the AI sometimes echoes, e.g.
# "8372/25 - " or "8372-25: ". Both slash and dash digit-separators.
_INTERNAL_ID_PREFIX_RE = re.compile(r"^\d{3,5}[/-]\d{2,4}\s*[-:/]\s*")
# eA variants we normalize to the canonical " (eA)" suffix.
_EA_TRAILING_COMMA_RE = re.compile(r"\s*,\s*eA\s*$", re.IGNORECASE)
_EA_TRAILING_BARE_RE = re.compile(r"\s+eA\s*$", re.IGNORECASE)
_EA_TRAILING_DASH_RE = re.compile(r"\s*[-–]\s*eA\s*$", re.IGNORECASE)
_EA_FULL_NAME_RE = re.compile(
    r"\s*[-–,(]?\s*einstweilige[r]?\s+Anordnung\s*\)?\s*$", re.IGNORECASE
)
_TRAILING_PUNCT_RE = re.compile(r"[\s\-–:/,;]+$")


def _normalize_case_title(title: str | None) -> str | None:
    """Coerce a raw AI / email-subject-derived title into canonical form.

    Operates in this order:
    1. extract any eA marker into a flag (so other rewrites don't disturb it),
    2. strip a leading echoed internal_id like "8372/25 - ",
    3. strip trailing punctuation artifacts ("-", ":", ",", " "),
    4. convert "X ./. Y (Matter)" → "X ./. Y - Matter",
    5. re-attach the eA marker as " (eA)" if present,
    6. cap at 120 chars.

    Idempotent: feeding the result back in returns the same string.
    """
    if title is None:
        return None
    s = title.strip()
    if not s:
        return None

    # 1. Detect + strip any eA marker. A bare " (eA)" suffix is preserved
    #    verbatim; other variants (", eA" / " - eA" / "einstweilige Anordnung")
    #    are stripped here and re-appended in canonical form at the end.
    has_ea = False
    if s.endswith(" (eA)") or s.endswith("(eA)"):
        has_ea = True
        s = s[: -len("(eA)")].rstrip().rstrip("(").rstrip()
    else:
        for pat in (
            _EA_FULL_NAME_RE,
            _EA_TRAILING_DASH_RE,
            _EA_TRAILING_COMMA_RE,
            _EA_TRAILING_BARE_RE,
        ):
            new_s, n = pat.subn("", s)
            if n:
                has_ea = True
                s = new_s
                break

    # 2. Strip leading internal_id echo.
    s = _INTERNAL_ID_PREFIX_RE.sub("", s, count=1).lstrip()

    # 3. Strip trailing punctuation/whitespace.
    s = _TRAILING_PUNCT_RE.sub("", s)

    # 4. Paren-style matter → dash-style. Only fires for "X ./. Y (Matter)";
    #    matter-only titles like "Kindesunterhalt" are untouched.
    m = _PAREN_MATTER_RE.match(s)
    if m:
        parties, matter = m.group(1).strip(), m.group(2).strip()
        s = f"{parties} - {matter}"

    # 5. Re-append eA in canonical form.
    if has_ea:
        s = f"{s} (eA)" if s else "(eA)"

    # 6. Cap. Smart-truncate so we don't slice mid-"(eA)".
    if len(s) > 120:
        if has_ea and s.endswith(" (eA)"):
            s = s[: 120 - len(" (eA)")].rstrip() + " (eA)"
        else:
            s = s[:120].rstrip()

    return s or None


def _is_better_title(new: str, current: str | None, internal_id: str) -> bool:
    """Decide whether to refresh a draft case's title from a fresh AI extract.

    Both sides are normalized first so the comparison is style-invariant.
    "Better" then means one of:
    - current is the bare fallback `Neuer Fall <id>` or empty;
    - current ends with a trailing separator artifact (rare after normalize,
      but defensive);
    - new carries an eA marker and current doesn't (or vice versa — eA is a
      load-bearing distinction the user can't get from internal_id);
    - new is meaningfully longer (50%+) AND adds a matter the current lacks.

    Returns False when current is already a good title and the new one isn't
    a clear improvement — avoids title churn during multi-doc ingestion.
    """
    new = _normalize_case_title(new) or ""
    current = _normalize_case_title(current) or ""
    if not new:
        return False
    if new == current:
        return False
    if not current:
        return True
    if current == f"Neuer Fall {internal_id}":
        return True
    if current.rstrip().endswith(("-", ":", "/", "–")):
        return True
    new_has_ea = new.endswith(" (eA)")
    cur_has_ea = current.endswith(" (eA)")
    if new_has_ea != cur_has_ea:
        # eA presence is a real semantic addition (or removal). Always pick
        # the eA-aware title — even if same length, the marker is signal.
        return new_has_ea
    # New title is at least 50% longer AND adds a "matter" segment (anything
    # after the " - " separator) that current lacks.
    new_has_matter = " - " in new
    cur_has_matter = " - " in current
    return len(new) > len(current) * 1.5 and new_has_matter and not cur_has_matter


def _derive_case_title_from_subject(
    subject: str | None, internal_id: str
) -> str | None:
    """Derive a short case title from an email subject line."""
    if not subject:
        return None
    stripped = subject.lstrip()
    if stripped.startswith(internal_id):
        remainder = stripped[len(internal_id) :].lstrip(" -:/")
    else:
        remainder = subject
    for sep in (" vor dem ", " wg. ", " bzgl. ", " betr. "):
        idx = remainder.lower().find(sep)
        if idx != -1:
            remainder = remainder[:idx]
    return remainder.strip()[:80] or None


def get_or_create_case_from_reference(
    db: Session,
    internal_id: str,
    *,
    az_court: str | None = None,
    court_name: str | None = None,
    court_level: ProceedingCourtLevel | None = None,
    batch_subject: str | None = None,
    ai_case_title: str | None = None,
    is_draft: bool = False,
) -> tuple[Case, Proceeding | None, bool]:
    """Return (case, proceeding, created).

    Race-safe: SELECT first, then INSERT only when missing. Never overwrites
    an existing case's is_draft flag. Caller is responsible for db.flush()/commit().

    Title logic:
    - On creation: prefer the AI-extracted `ai_case_title` (already a clean
      "Schmidt ./. Schmidt (Sorgerecht)"-style title from the metadata stage)
      over `_derive_case_title_from_subject(batch_subject, …)`, which often
      leaves trailing-dash artifacts after stripping " wg. " etc.
    - On retry while case is still a draft: refresh the existing title from
      `ai_case_title` if the new title looks more useful. Once the user
      ratifies the case (is_draft=False), the title is locked — manual edits
      survive.
    """
    from app.services.ingestion.extractors import infer_court_level, normalize_az_court

    az_court = normalize_az_court(az_court)
    existing = db.query(Case).filter(Case.id == internal_id).first()
    if existing:
        if existing.is_draft:
            # Two distinct refreshes for draft cases:
            #   (a) if the AI provided a richer title, apply it (normalized);
            #   (b) otherwise, if the current stored title is non-canonical
            #       (legacy paren-matter, comma-eA, leading id-echo, etc.),
            #       rewrite to canonical form. Style consistency is worth a
            #       no-op-looking write because every other retry of any doc
            #       in the case bundles will hit this path.
            normalized_current = _normalize_case_title(existing.title)
            new_candidate = (
                _normalize_case_title(ai_case_title) if ai_case_title else None
            )
            if new_candidate and _is_better_title(
                ai_case_title, existing.title, internal_id
            ):
                existing.title = new_candidate
                db.flush()
            elif normalized_current and normalized_current != existing.title:
                existing.title = normalized_current
                db.flush()
        matched_proc = None
        if az_court:
            matched_proc = (
                db.query(Proceeding)
                .filter(
                    Proceeding.case_id == internal_id, Proceeding.az_court == az_court
                )
                .first()
            )
        return existing, matched_proc, False

    title = (
        _normalize_case_title(ai_case_title)
        or _normalize_case_title(
            _derive_case_title_from_subject(batch_subject, internal_id)
        )
        or f"Neuer Fall {internal_id}"
    )
    new_case = Case(
        id=internal_id,
        title=title,
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
        is_draft=is_draft,
    )
    db.add(new_case)
    from sqlalchemy.exc import IntegrityError

    try:
        db.flush()
    except IntegrityError:
        # Race condition: someone else created it.
        db.rollback()
        existing = db.query(Case).filter(Case.id == internal_id).first()
        if existing:
            matched_proc = None
            if az_court:
                matched_proc = (
                    db.query(Proceeding)
                    .filter(
                        Proceeding.case_id == internal_id,
                        Proceeding.az_court == az_court,
                    )
                    .first()
                )
            return existing, matched_proc, False
        raise  # Should not happen if it was a UNIQUE constraint on ID

    resolved_level = (
        court_level or infer_court_level(court_name) or ProceedingCourtLevel.OTHER
    )
    # Always ensure a default proceeding exists. Inherit draft state from the
    # case so an AI auto-created proceeding is also marked draft until ratified.
    new_proc = Proceeding(
        case_id=internal_id,
        az_court=az_court,
        court_name=court_name or "General",
        court_level=resolved_level,
        status=ProceedingStatus.ACTIVE,
        is_draft=is_draft,
    )
    db.add(new_proc)
    db.flush()

    return new_case, new_proc, True


def _safe_dt(doc: Document) -> datetime:
    """Return a tz-naive datetime for sorting documents chronologically."""
    dt = doc.issued_date or doc.ingest_date or datetime.min
    return to_naive(dt)


def _latest_streitwert(docs: list[Document]) -> float | None:
    """Return the EUR amount from the most recent streitwert signal, or None."""
    candidates = [
        d
        for d in docs
        if isinstance(d.cost_delta, dict) and d.cost_delta.get("kind") == "streitwert"
    ]
    if not candidates:
        return None
    doc = max(candidates, key=_safe_dt)
    amount = doc.cost_delta.get("amount")
    return float(amount) if amount is not None else None


def _latest_ruling_allocation(docs: list[Document]) -> dict | None:
    """Return the resolved allocation from the most recent cost_ruling signal, or None."""
    candidates = [
        d
        for d in docs
        if isinstance(d.cost_delta, dict) and d.cost_delta.get("kind") == "cost_ruling"
    ]
    if not candidates:
        return None
    doc = max(candidates, key=_safe_dt)
    return allocation_from_ruling(doc.cost_delta.get("allocation") or {})


def _ledger_net_exposure(case_id: str, db: Session) -> float:
    """Net open financial exposure from LegalCost ledger rows in EUR.

    - ERSTATTET rows contribute 0 (fully reimbursed).
    - BEZAHLT rows reduce exposure by any overpayment (expected refund).
    - All other rows contribute (amount_gross - amount_paid - amount_reimbursed).
    """
    costs = db.query(LegalCost).filter(LegalCost.case_id == case_id).all()
    total = 0.0
    for c in costs:
        gross = c.amount_gross or 0.0
        paid = c.amount_paid or 0.0
        reimbursed = c.amount_reimbursed or 0.0
        if c.status == CostStatus.ERSTATTET:
            continue
        if c.status == CostStatus.BEZAHLT:
            total -= max(0.0, paid - gross)  # overpayment → expected refund
        else:
            total += max(0.0, gross - paid - reimbursed)
    return total


def recompute_total_cost_exposure(case_id: str, db: Session) -> int:
    """Recompute and persist Case.total_cost_exposure using the RVG/GKG calculator.

    Algorithm per proceeding:
      1. Find the latest streitwert signal — skip if none (no projection basis).
      2. Resolve allocation: own ruling > propagated ruling > family default >
         assume_worst_case toggle > civil placeholder.
      3. Add: own lawyer gross + court × own_court_share + opposing × own_opposing_share.

    Also adds open LegalCost ledger rows (invoices, vorschüsse, manual entries)
    and subtracts expected refunds from overpaid rows.

    Returns the new total in cents and persists it on Case.total_cost_exposure.
    """
    if not case_id or case_id == "_TRIAGE":
        return 0

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return 0

    is_family = (
        hasattr(case, "case_type")
        and case.case_type is not None
        and case.case_type == CaseType.FAMILY
    )
    case_type = CaseType.FAMILY if is_family else CaseType.CIVIL

    proceedings = (
        db.query(Proceeding)
        .filter(Proceeding.case_id == case_id)
        .order_by(Proceeding.started_at.asc().nullslast(), Proceeding.id.asc())
        .all()
    )

    total_eur = 0.0
    propagated_allocation: dict | None = None

    for proc in proceedings:
        docs = (
            db.query(Document)
            .filter(
                Document.proceeding_id == proc.id,
                Document.cost_delta.isnot(None),
            )
            .all()
        )

        streitwert = _latest_streitwert(docs)
        if not streitwert or streitwert <= 0:
            continue  # no basis for fee projection; skip this proceeding

        ruling_alloc = _latest_ruling_allocation(docs)
        if ruling_alloc:
            alloc = ruling_alloc
            propagated_allocation = ruling_alloc
        elif propagated_allocation:
            alloc = {**propagated_allocation, "source": "propagated"}
        elif is_family:
            alloc = default_allocation(CaseType.FAMILY, proc.court_level)
        elif getattr(case, "assume_worst_case", True):
            alloc = {
                "own_court_share": 1.0,
                "own_opposing_share": 1.0,
                "source": "worst_case",
            }
        else:
            alloc = default_allocation(case_type, proc.court_level)

        own = lawyer_fees(streitwert, proc.court_level, is_family)
        c_fee = court_fees(streitwert, proc.court_level, is_family)

        total_eur += own["gross"]
        total_eur += c_fee * alloc["own_court_share"]
        if alloc["own_opposing_share"] > 0:
            opp = lawyer_fees(streitwert, proc.court_level, is_family)
            total_eur += opp["gross"] * alloc["own_opposing_share"]

    total_eur += _ledger_net_exposure(case_id, db)

    total_cents = int(round(total_eur * 100))
    case.total_cost_exposure = total_cents
    db.commit()
    logger.info(f"Case {case_id}: total_cost_exposure updated to {total_cents} cents")

    return total_cents


def build_proceeding_exposure(case_id: str, db: Session) -> list[dict]:
    """Return a per-proceeding fee breakdown for the financials UI.

    Each entry contains proceeding object, streitwert, allocation source label,
    and computed fee components (own lawyer, court share, opposing share, subtotal).
    Proceedings with no streitwert signal are omitted.
    """
    if not case_id or case_id == "_TRIAGE":
        return []

    case = db.query(Case).filter(Case.id == case_id).first()
    if not case:
        return []

    is_family = (
        hasattr(case, "case_type")
        and case.case_type is not None
        and case.case_type == CaseType.FAMILY
    )

    proceedings = (
        db.query(Proceeding)
        .filter(Proceeding.case_id == case_id)
        .order_by(Proceeding.started_at.asc().nullslast(), Proceeding.id.asc())
        .all()
    )

    result = []
    propagated_allocation: dict | None = None

    for proc in proceedings:
        docs = (
            db.query(Document)
            .filter(
                Document.proceeding_id == proc.id,
                Document.cost_delta.isnot(None),
            )
            .all()
        )

        streitwert = _latest_streitwert(docs)
        if not streitwert or streitwert <= 0:
            continue

        ruling_alloc = _latest_ruling_allocation(docs)
        if ruling_alloc:
            alloc = ruling_alloc
            propagated_allocation = ruling_alloc
        elif propagated_allocation:
            alloc = {**propagated_allocation, "source": "propagated"}
        elif is_family:
            alloc = default_allocation(CaseType.FAMILY, proc.court_level)
        elif getattr(case, "assume_worst_case", True):
            alloc = {
                "own_court_share": 1.0,
                "own_opposing_share": 1.0,
                "source": "worst_case",
            }
        else:
            alloc = default_allocation(CaseType.CIVIL, proc.court_level)

        own = lawyer_fees(streitwert, proc.court_level, is_family)
        c_fee = court_fees(streitwert, proc.court_level, is_family)
        opp_gross = (
            lawyer_fees(streitwert, proc.court_level, is_family)["gross"]
            if alloc["own_opposing_share"] > 0
            else 0.0
        )

        result.append(
            {
                "proceeding": proc,
                "streitwert": streitwert,
                "allocation_source": alloc.get("source", ""),
                "own_lawyer_gross": own["gross"],
                "own_lawyer_breakdown": own.get("breakdown", {}),
                "court_fee": c_fee,
                "court_fee_share": alloc["own_court_share"],
                "court_fee_charged": c_fee * alloc["own_court_share"],
                "opposing_gross": opp_gross * alloc["own_opposing_share"],
                "subtotal": own["gross"]
                + c_fee * alloc["own_court_share"]
                + opp_gross * alloc["own_opposing_share"],
            }
        )

    return result


class CaseService:
    """Service layer for Case operations."""

    def __init__(self, db: Session):
        self.db = db
        self.case_repo = CaseRepository(db)
        self.doc_repo = DocumentRepository(db)
        self.action_repo = ActionItemRepository(db)
        self.entity_repo = EntityRepository(db)
        self.cost_repo = LegalCostRepository(db)

    def get_case_with_summary(self, case_id: str) -> dict | None:
        """Get case with all related data."""
        from app.services.user_settings_service import count_new_since, get_last_viewed

        case = self.case_repo.get_by_id(case_id)
        if not case:
            return None

        # Eager load proceedings to avoid N+1 in templates
        documents = self.doc_repo.get_by_case(
            case_id, options=[joinedload(Document.proceeding)]
        )
        deadlines = self.action_repo.get_by_case(
            case_id, action_type=ActionItemType.DEADLINE
        )
        hearings = self.action_repo.get_by_case(
            case_id, action_type=ActionItemType.COURT_DATE
        )
        costs = self.cost_repo.get_by_case(case_id)
        entities = self.entity_repo.get_by_case(case_id)

        last_visit = get_last_viewed(case_id, self.db)
        new_docs = count_new_since(case_id, last_visit, self.db)

        now = datetime.now()
        return {
            "case": case,
            "documents": documents,
            "deadlines": deadlines,
            "hearings": hearings,
            "costs": costs,
            "entities": entities,
            "document_count": len(documents),
            "pending_review_count": sum(1 for d in documents if d.needs_review),
            "upcoming_deadlines": sum(
                1 for d in deadlines if d.status == ActionItemStatus.OPEN
            ),
            "upcoming_hearings": sum(1 for h in hearings if h.due_date > now),
            "last_visit": last_visit,
            "new_docs_since_last_visit": new_docs,
        }

    def enrich_case_for_card(
        self, case: Case, now: datetime, last_home_visit: datetime | None = None
    ) -> dict[str, Any]:
        """Enrich a case with metadata needed for the dashboard/directory card."""
        from app.services.user_settings_service import count_new_since

        # Get closest action item
        next_action = (
            self.db.query(ActionItem)
            .filter(
                ActionItem.case_id == case.id,
                ActionItem.status == ActionItemStatus.OPEN,
            )
            .order_by(ActionItem.due_date.asc())
            .first()
        )

        new_docs_count = (
            count_new_since(case.id, last_home_visit, self.db) if last_home_visit else 0
        )

        # Days since last activity
        last_doc = (
            self.db.query(Document)
            .filter(Document.case_id == case.id)
            .order_by(Document.ingest_date.desc())
            .first()
        )
        days_since = (
            (now - last_doc.ingest_date).days
            if last_doc
            else (now - case.ingest_date).days
        )

        # Get active proceeding name
        active_proc = next((p for p in case.proceedings if p.status == "active"), None)
        if not active_proc and case.proceedings:
            active_proc = case.proceedings[0]

        proceeding_name = active_proc.court_name if active_proc else "General"

        # Max significance tier across most recent 20 documents
        _sig_rank = {
            SignificanceTier.CRITICAL: 4,
            SignificanceTier.SIGNIFICANT: 3,
            SignificanceTier.INFORMATIONAL: 2,
            SignificanceTier.ADMINISTRATIVE: 1,
        }
        recent_docs = (
            self.db.query(Document.significance_tier)
            .filter(Document.case_id == case.id, Document.significance_tier.isnot(None))
            .order_by(Document.ingest_date.desc())
            .limit(20)
            .all()
        )
        max_sig = max(
            (row[0] for row in recent_docs),
            key=lambda t: _sig_rank.get(t, 0),
            default=None,
        )

        # Extract client and opposing names from the parties list
        parties = case.parties or []
        client_name = "Unknown"
        opposing_name = "Unknown"

        if isinstance(parties, list):
            for p in parties:
                role = p.get("role") or p.get("key")  # handle both schema versions
                if role in ("own", "klaegerin"):
                    client_name = p.get("name", "Unknown")
                elif role in ("opposing", "beklagter"):
                    opposing_name = p.get("name", "Unknown")

        return {
            "id": case.id,
            "title": case.title,
            "status": case.status,
            "is_draft": case.is_draft,
            "status_line": case.ai_brief.get("status_line", "Active")
            if case.ai_brief and isinstance(case.ai_brief, dict)
            else "Active",
            "next_action": next_action,
            "exposure_eur": case.total_cost_exposure / 100.0
            if case.total_cost_exposure
            else 0.0,
            "new_docs": new_docs_count,
            "days_since_activity": days_since,
            "tier": "delta" if new_docs_count > 0 else "normal",
            "proceeding_name": proceeding_name,
            "max_significance": max_sig,
            "client_name": client_name,
            "opposing_party": opposing_name,
            "is_dormant": days_since > 90,
            "updated_at": last_doc.ingest_date if last_doc else case.ingest_date,
            "active_proceeding": {
                "title": active_proc.court_name,
                "matter_type": active_proc.subject_matter or "",
            }
            if active_proc
            else None,
        }

    def get_all_cases_directory(self) -> dict:
        """Get all cases with counts for directory view."""
        all_cases = self.case_repo.get_all_sorted_by_date(include_drafts=True)
        now = datetime.now()

        # Fetch last_home_visit from user settings for enrichment
        from app.models.database import UserSettings

        settings = self.db.query(UserSettings).first()
        last_home_visit_iso = (
            settings.settings_json.get("last_home_visit")
            if settings and settings.settings_json
            else None
        )
        last_home_visit = (
            datetime.fromisoformat(last_home_visit_iso) if last_home_visit_iso else None
        )

        enriched_cases = [
            self.enrich_case_for_card(c, now, last_home_visit) for c in all_cases
        ]

        draft_cases = [c for c in enriched_cases if c["is_draft"]]
        active_cases = [
            c
            for c in enriched_cases
            if not c["is_draft"] and c["status"] != CaseStatus.CLOSED
        ]
        closed_cases = [
            c
            for c in enriched_cases
            if not c["is_draft"] and c["status"] == CaseStatus.CLOSED
        ]

        stats_by_status = self.case_repo.count_all_by_status()

        doc_counts = self.doc_repo.bulk_count_by_case([c.id for c in all_cases])
        action_counts = self.action_repo.bulk_count_open_by_case(
            [c.id for c in all_cases]
        )

        return {
            "cases": enriched_cases,
            "draft_cases": draft_cases,
            "active_cases": active_cases,
            "closed_cases": closed_cases,
            "stats_by_status": stats_by_status,
            "doc_counts": doc_counts,
            "deadline_counts": action_counts,
            "total": len(all_cases),
        }

    def get_all_cases_directory_paginated(
        self, page: int = 1, per_page: int = 20
    ) -> dict:
        """Get paginated cases with counts for directory view."""
        cases, total = self.case_repo.get_paginated(
            page=page, per_page=per_page, include_drafts=True
        )
        now = datetime.now()

        # Fetch last_home_visit from user settings for enrichment
        from app.models.database import UserSettings

        settings = self.db.query(UserSettings).first()
        last_home_visit_iso = (
            settings.settings_json.get("last_home_visit")
            if settings and settings.settings_json
            else None
        )
        last_home_visit = (
            datetime.fromisoformat(last_home_visit_iso) if last_home_visit_iso else None
        )

        enriched_cases = [
            self.enrich_case_for_card(c, now, last_home_visit) for c in cases
        ]

        draft_cases = [c for c in enriched_cases if c["is_draft"]]
        active_cases = [
            c
            for c in enriched_cases
            if not c["is_draft"] and c["status"] != CaseStatus.CLOSED
        ]
        closed_cases = [
            c
            for c in enriched_cases
            if not c["is_draft"] and c["status"] == CaseStatus.CLOSED
        ]

        stats_by_status = self.case_repo.count_all_by_status()

        case_ids = [c.id for c in cases]
        doc_counts = self.doc_repo.bulk_count_by_case(case_ids)
        action_counts = self.action_repo.bulk_count_open_by_case(case_ids)

        return {
            "cases": enriched_cases,
            "draft_cases": draft_cases,
            "active_cases": active_cases,
            "closed_cases": closed_cases,
            "stats_by_status": stats_by_status,
            "doc_counts": doc_counts,
            "deadline_counts": action_counts,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
        }

    def create_case(
        self,
        case_id: str,
        title: str,
        status: CaseStatus = CaseStatus.INTAKE,
        jurisdiction: Jurisdiction = Jurisdiction.DE,
    ) -> Case:
        """Create a new case."""
        return self.case_repo.create_case(
            case_id=case_id,
            title=title,
            status=status,
            jurisdiction=jurisdiction,
        )

    def update_case_status(self, case_id: str, status: CaseStatus) -> Case | None:
        """Update case status."""
        return self.case_repo.update_status(case_id, status)

    def delete_and_revert(self, case_id: str) -> dict | None:
        """Delete a case, revert its docs/batches to triage, cascade dependent rows.

        Returns ``{"docs": [...], "doc_count": N}`` on success so the caller
        can drive re-enrich + OOB rendering. Returns ``None`` when the case
        does not exist (caller decides whether to 404).

        Raises ``ValueError`` for the Triage singleton — it must never be
        deleted (every triage doc references it).
        """
        from app.models.database import (
            Conversation,
            Entity,
            IngestBatch,
            LegalCost,
        )

        if case_id == "_TRIAGE":
            raise ValueError("Triage Inbox cannot be deleted")

        case = self.db.query(Case).filter(Case.id == case_id).first()
        if not case:
            return None

        # Snapshot affected docs + batches before mutating.
        docs = self.db.query(Document).filter(Document.case_id == case_id).all()
        batch_ids = {d.ingest_batch_id for d in docs if d.ingest_batch_id}

        # Revert documents to the Triage Inbox so they re-enter review.
        for doc in docs:
            doc.case_id = "_TRIAGE"
            doc.proceeding_id = None
            doc.needs_review = True

        # Revert any IngestBatches whose case_id pointed at this case.
        for batch_id in batch_ids:
            batch = (
                self.db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
            )
            if batch and batch.case_id == case_id:
                batch.case_id = None
                batch.proceeding_id = None

        # Hard delete dependent rows scoped to this case.
        self.db.query(Entity).filter(Entity.case_id == case_id).delete(
            synchronize_session=False
        )
        self.db.query(ActionItem).filter(ActionItem.case_id == case_id).delete(
            synchronize_session=False
        )
        self.db.query(LegalCost).filter(LegalCost.case_id == case_id).delete(
            synchronize_session=False
        )
        # Wave 2A: claims are global; their case scope follows their evidence
        # documents. Since docs revert to _TRIAGE rather than being deleted,
        # claims simply re-scope to _TRIAGE alongside their evidence anchors.
        # No explicit claim deletion needed here.

        # Conversations scope_id is a polymorphic string with no FK; clean up
        # case-scoped chats explicitly so they aren't stranded after deletion.
        # Document-scoped chats follow surviving documents back to triage.
        self.db.query(Conversation).filter(
            Conversation.scope_type == "case",
            Conversation.scope_id == case_id,
        ).delete(synchronize_session=False)

        # Case → Proceedings cascade is wired via ORM relationship.
        self.db.delete(case)
        audit_service.record(
            self.db,
            AuditEventType.CASE_DELETED,
            target_type="case",
            target_id=case_id,
        )
        self.db.commit()

        if docs:
            from app.services.triage_service import _reset_and_reenrich

            _reset_and_reenrich(self.db, docs)

        return {"docs": docs, "doc_count": len(docs)}

    def delete_empty_proceeding(self, proceeding_id: int) -> dict:
        """Delete a proceeding that has no attached documents, batches, action items, or costs.

        Refuses to delete the last remaining proceeding of a case.
        Returns {"case_id": str, "was_active": bool} on success.
        Raises ValueError on guard violation (caller maps to 400/404).
        """
        from app.models.database import IngestBatch
        from app.services.user_settings_service import get_active_proceeding

        proceeding = (
            self.db.query(Proceeding).filter(Proceeding.id == proceeding_id).first()
        )
        if not proceeding:
            raise ValueError("Proceeding not found")

        case_id = proceeding.case_id

        doc_count = (
            self.db.query(Document)
            .filter(Document.proceeding_id == proceeding_id)
            .count()
        )
        batch_count = (
            self.db.query(IngestBatch)
            .filter(IngestBatch.proceeding_id == proceeding_id)
            .count()
        )
        action_count = (
            self.db.query(ActionItem)
            .filter(ActionItem.proceeding_id == proceeding_id)
            .count()
        )
        cost_count = (
            self.db.query(LegalCost)
            .filter(LegalCost.proceeding_id == proceeding_id)
            .count()
        )

        if doc_count + batch_count + action_count + cost_count > 0:
            raise ValueError("Proceeding has attached records and cannot be deleted")

        sibling_count = (
            self.db.query(Proceeding)
            .filter(Proceeding.case_id == case_id, Proceeding.id != proceeding_id)
            .count()
        )
        if sibling_count == 0:
            raise ValueError("Cannot delete the only proceeding of a case")

        active_id = get_active_proceeding(case_id, self.db)
        was_active = active_id == proceeding_id

        self.db.delete(proceeding)
        self.db.commit()

        return {"case_id": case_id, "was_active": was_active}

    def get_dashboard_stats(self) -> dict:
        """Get statistics for dashboard."""
        all_cases = self.case_repo.get_all()
        active_cases = [c for c in all_cases if c.status != CaseStatus.CLOSED]

        pending_docs = self.doc_repo.get_pending_review()

        court_doc_count = (
            self.db.query(Document)
            .filter(Document.originator_type.in_(["court"]))
            .count()
        )

        upcoming_deadlines = self.action_repo.get_upcoming(
            days=7, action_type=ActionItemType.DEADLINE
        )
        upcoming_hearings = self.action_repo.get_upcoming(
            days=30, action_type=ActionItemType.COURT_DATE
        )

        return {
            "active_case_count": len(active_cases),
            "pending_review_count": len(pending_docs),
            "court_doc_count": court_doc_count,
            "upcoming_deadlines": upcoming_deadlines,
            "upcoming_hearings": upcoming_hearings,
        }


def _compute_dormancy_alert(case, db) -> str | None:
    """Return a textual alert when an active proceeding has been silent past the threshold."""
    now = datetime.now()
    active_procs = [
        p for p in (case.proceedings or []) if p.status == ProceedingStatus.ACTIVE
    ]
    if not active_procs:
        return None

    oldest_silent_proc = None
    oldest_days = 0

    for proc in active_procs:
        last_activity = (
            db.query(func.max(Document.ingest_date))
            .filter(Document.proceeding_id == proc.id)
            .scalar()
        )
        if last_activity is None:
            last_activity = proc.started_at or proc.ingest_date
        if last_activity is None:
            continue
        days = (now - last_activity).days
        if days > DORMANCY_DAYS and days > oldest_days:
            oldest_silent_proc = proc
            oldest_days = days

    if oldest_silent_proc is None:
        return None

    court = oldest_silent_proc.court_name or "Unknown court"
    az = oldest_silent_proc.az_court or "no docket"
    return f"{court} ({az}) has had no activity for {oldest_days} days."
