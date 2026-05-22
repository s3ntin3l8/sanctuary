import logging
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.timezone import to_naive
from app.models.database import CostSignal, Document, LegalCost
from app.models.enums import CostCategory, CostSignalType, CostStatus
from app.repositories.legal_cost import LegalCostRepository

logger = logging.getLogger(__name__)

# Maps cost_delta kind to (CostCategory, vat_rate) — invoice/vorschuss signals
# promote to LegalCost rows.
_KIND_TO_CATEGORY: dict[str, tuple[CostCategory, float]] = {
    "invoice_lawyer": (CostCategory.ANWALTSKOSTEN, 0.19),
    "invoice_court": (CostCategory.GERICHTSKOSTEN, 0.0),
    "vorschuss_lawyer": (CostCategory.VORSCHUSS, 0.19),
    "vorschuss_court": (CostCategory.VORSCHUSS, 0.0),
}

# Non-cost metadata signals — promote to CostSignal rows (sibling table).
_KIND_TO_SIGNAL_TYPE: dict[str, CostSignalType] = {
    "streitwert": CostSignalType.STREITWERT,
    "cost_ruling": CostSignalType.COST_RULING,
    "pkh_grant": CostSignalType.PKH_GRANT,
    "pkh_denied": CostSignalType.PKH_DENIED,
}


def materialize_cost_signal(
    doc: Document, cost_delta: dict, db: Session
) -> LegalCost | CostSignal | None:
    """Dispatch a cost_delta dict to LegalCost (invoice/vorschuss) or CostSignal (others).

    The single materialisation entry point. Returns the created/updated row, or
    None for unknown kinds. Idempotent per (source_document_id, kind).
    """
    kind = cost_delta.get("kind", "")
    if kind in _KIND_TO_CATEGORY:
        return _ensure_ledger_row(doc, cost_delta, db)
    if kind in _KIND_TO_SIGNAL_TYPE:
        return _ensure_cost_signal(doc, cost_delta, db)
    return None


def _ensure_ledger_row(
    doc: Document, cost_delta: dict, db: Session
) -> LegalCost | None:
    """Idempotently materialise a LegalCost row from an invoice/vorschuss signal.

    Keyed on source_document_id — re-enrichment updates the row in place rather
    than creating a duplicate.
    """
    kind = cost_delta.get("kind", "")
    if kind not in _KIND_TO_CATEGORY:
        return None

    category, default_vat = _KIND_TO_CATEGORY[kind]
    amount = cost_delta.get("amount")
    if amount is None:
        return None
    amount = float(amount)

    # Determine net/gross from vat_included flag
    vat_included = cost_delta.get("vat_included")
    if vat_included is True:
        # AI says amount already includes VAT
        amount_gross = amount
        amount_net = round(amount / (1 + default_vat), 2) if default_vat > 0 else amount
    elif vat_included is False:
        # AI says amount is net
        amount_net = amount
        amount_gross = round(amount * (1 + default_vat), 2)
    else:
        # Unknown — treat as gross for court fees (no VAT), net for lawyer fees
        if default_vat > 0:
            amount_net = amount
            amount_gross = round(amount * (1 + default_vat), 2)
        else:
            amount_net = amount
            amount_gross = amount

    # Check for existing row
    existing = (
        db.query(LegalCost).filter(LegalCost.source_document_id == doc.id).first()
    )
    if existing:
        existing.amount_net = amount_net
        existing.amount_gross = amount_gross
        existing.vat_rate = default_vat
        existing.category = category
        _derive_status(existing)
        db.flush()
        return existing

    row = LegalCost(
        case_id=doc.case_id,
        proceeding_id=doc.proceeding_id,
        category=category,
        title=cost_delta.get("description") or doc.title or f"{kind} (auto)",
        rvg_position=None,
        amount_net=amount_net,
        vat_rate=default_vat,
        amount_gross=amount_gross,
        amount_paid=0.0,
        amount_reimbursed=0.0,
        status=CostStatus.OFFEN,
        source_document_id=doc.id,
        auto_created=True,
    )
    db.add(row)
    db.flush()

    # Link to a prior Vorschuss row if the AI identified one
    offsets_signal_id = cost_delta.get("offsets_signal_id")
    if offsets_signal_id and kind.startswith("invoice_"):
        prior = (
            db.query(LegalCost)
            .filter(LegalCost.source_document_id == offsets_signal_id)
            .first()
        )
        if prior:
            prior.offsets_cost_id = row.id
            db.flush()

    return row


def _ensure_cost_signal(
    doc: Document, cost_delta: dict, db: Session
) -> CostSignal | None:
    """Idempotently materialise a CostSignal row from a non-cost signal.

    Keyed on (source_document_id, signal_type) — re-enrichment updates in place.
    Handles the four orphan kinds: streitwert, cost_ruling, pkh_grant, pkh_denied.
    """
    kind = cost_delta.get("kind", "")
    signal_type = _KIND_TO_SIGNAL_TYPE.get(kind)
    if signal_type is None or not doc.case_id:
        return None

    issued_at = doc.issued_date or doc.ingest_date
    if issued_at is not None:
        issued_at = to_naive(issued_at)

    amount = cost_delta.get("amount")
    if amount is not None:
        amount = float(amount)

    existing = (
        db.query(CostSignal)
        .filter(
            CostSignal.source_document_id == doc.id,
            CostSignal.signal_type == signal_type,
        )
        .first()
    )
    if existing:
        existing.case_id = doc.case_id
        existing.proceeding_id = doc.proceeding_id
        existing.amount = amount
        existing.allocation = cost_delta.get("allocation")
        existing.description = cost_delta.get("description") or doc.title
        existing.issued_at = issued_at
        db.flush()
        return existing

    row = CostSignal(
        case_id=doc.case_id,
        proceeding_id=doc.proceeding_id,
        source_document_id=doc.id,
        signal_type=signal_type,
        amount=amount,
        allocation=cost_delta.get("allocation"),
        description=cost_delta.get("description") or doc.title,
        issued_at=issued_at,
    )
    db.add(row)
    db.flush()
    return row


def _derive_status(cost: LegalCost) -> None:
    """Update cost.status based on paid/reimbursed amounts (in place, no flush)."""
    gross = cost.amount_gross or 0.0
    paid = cost.amount_paid or 0.0
    reimbursed = cost.amount_reimbursed or 0.0
    if gross <= 0:
        return
    if reimbursed >= gross:
        cost.status = CostStatus.ERSTATTET
    elif paid >= gross:
        cost.status = CostStatus.BEZAHLT
    elif paid > 0:
        cost.status = CostStatus.TEILWEISE
    else:
        cost.status = CostStatus.OFFEN


class CostSummary:
    def __init__(self, costs: list[LegalCost]):
        self.costs = costs
        self._calculate_totals()

    def _calculate_totals(self):
        self.total_gross = sum(c.amount_gross or 0 for c in self.costs)
        self.total_paid = sum(c.amount_paid or 0 for c in self.costs)
        self.total_reimbursed = sum(c.amount_reimbursed or 0 for c in self.costs)
        self.total_outstanding = sum(
            (c.amount_gross or 0) - (c.amount_paid or 0)
            for c in self.costs
            if c.status not in (CostStatus.BEZAHLT, CostStatus.ERSTATTET)
        )
        self.total_reimbursable = sum(
            (c.amount_gross or 0) - (c.amount_reimbursed or 0)
            for c in self.costs
            if c.is_reimbursable and c.status != CostStatus.ERSTATTET
        )

    def to_dict(self) -> dict:
        return {
            "total_gross": self.total_gross,
            "total_paid": self.total_paid,
            "total_reimbursed": self.total_reimbursed,
            "total_outstanding": self.total_outstanding,
            "total_reimbursable": self.total_reimbursable,
        }


class CostService:
    """Service layer for LegalCost operations."""

    def __init__(self, db: Session):
        self.db = db
        self.cost_repo = LegalCostRepository(db)

    def get_costs_by_case(self, case_id: str) -> Sequence[LegalCost]:
        """Get all costs for a case."""
        return self.cost_repo.get_by_case(case_id)

    def get_cost_summary(self, case_id: str) -> CostSummary:
        """Get cost summary for a case."""
        costs = self.get_costs_by_case(case_id)
        return CostSummary(costs)

    def get_all_costs(self) -> Sequence[LegalCost]:
        """Get all costs."""
        return self.cost_repo.get_all()

    def get_costs_for_page(self) -> dict:
        """Get all costs with grouping for page rendering."""
        from app.models.database import Case

        costs = self.get_all_costs()
        global_summary = self.get_global_cost_summary()

        costs_by_case = {}
        for cost in costs:
            if cost.case_id not in costs_by_case:
                case = self.db.query(Case).filter(Case.id == cost.case_id).first()
                costs_by_case[cost.case_id] = {
                    "case": case,
                    "costs": [],
                    "summary": None,
                    "streitwert": None,
                }
            costs_by_case[cost.case_id]["costs"].append(cost)

        for case_id, data in costs_by_case.items():
            data["summary"] = CostSummary(data["costs"])
            data["streitwert"] = (
                getattr(data["case"], "streitwert", None) if data["case"] else None
            )

        return {
            "all_costs": costs,
            "costs_by_case": costs_by_case,
            "global_summary": global_summary,
        }

    def get_global_cost_summary(self) -> CostSummary:
        """Get cost summary across all cases."""
        costs = self.get_all_costs()
        return CostSummary(costs)

    def get_costs_by_status(self, status: CostStatus) -> Sequence[LegalCost]:
        """Get costs by payment status."""
        return self.cost_repo.get_by_status(status)

    def get_pending_costs(self) -> Sequence[LegalCost]:
        """Get costs pending payment."""
        return self.cost_repo.get_pending()

    def create_cost(
        self,
        case_id: str,
        category: CostCategory,
        title: str,
        amount_net: float,
        amount_gross: float,
        status: CostStatus = CostStatus.OFFEN,
        **kwargs,
    ) -> LegalCost:
        """Create a new cost."""
        return self.cost_repo.create_cost(
            case_id=case_id,
            category=category,
            title=title,
            amount_net=amount_net,
            amount_gross=amount_gross,
            status=status,
            **kwargs,
        )

    def update_cost_status(
        self, cost_id: int, status: CostStatus, paid_at: datetime | None = None
    ) -> LegalCost | None:
        """Update cost payment status."""
        return self.cost_repo.update_status(cost_id, status, paid_at)

    def mark_as_paid(self, cost_id: int) -> LegalCost | None:
        """Mark cost as paid in full and stamp the paid amount on the row.

        Updating the status alone leaves ``amount_paid`` at zero, which
        causes the PAID / OUTSTANDING KPIs to disagree with the row's BEZAHLT
        badge. Setting both fields keeps the headline numbers in sync.
        """
        cost = self.cost_repo.get(cost_id)
        if not cost:
            return None
        cost.amount_paid = cost.amount_gross or 0.0
        cost.status = CostStatus.BEZAHLT
        cost.paid_at = datetime.now()
        self.db.flush()
        self.db.refresh(cost)
        return cost

    def mark_as_reimbursed(self, cost_id: int, amount: float) -> LegalCost | None:
        """Mark cost as reimbursed."""
        cost = self.cost_repo.get(cost_id)
        if cost:
            cost.amount_reimbursed = amount
            cost.status = CostStatus.ERSTATTET
            self.db.flush()
            self.db.refresh(cost)
        return cost

    def mark_as_unpaid(self, cost_id: int) -> LegalCost | None:
        """Reverse a mark-paid: clear amount_paid + paid_at and re-derive status
        from the remaining amounts. If the row was partially reimbursed before
        being paid, status falls back to TEILWEISE / ERSTATTET accordingly."""
        cost = self.cost_repo.get(cost_id)
        if not cost:
            return None
        cost.amount_paid = 0.0
        cost.paid_at = None
        _derive_status(cost)
        self.db.flush()
        self.db.refresh(cost)
        return cost

    def mark_as_unreimbursed(self, cost_id: int) -> LegalCost | None:
        """Reverse a mark-reimbursed: clear amount_reimbursed and re-derive
        status. If the row was also paid, it falls back to BEZAHLT; otherwise
        OFFEN / TEILWEISE as appropriate."""
        cost = self.cost_repo.get(cost_id)
        if not cost:
            return None
        cost.amount_reimbursed = 0.0
        _derive_status(cost)
        self.db.flush()
        self.db.refresh(cost)
        return cost

    def update_amounts_and_derive_status(
        self,
        cost_id: int,
        amount_paid: float | None = None,
        amount_reimbursed: float | None = None,
    ) -> LegalCost | None:
        """Update paid/reimbursed amounts and auto-derive status."""
        cost = self.cost_repo.get(cost_id)
        if not cost:
            return None
        if amount_paid is not None:
            cost.amount_paid = amount_paid
        if amount_reimbursed is not None:
            cost.amount_reimbursed = amount_reimbursed
        _derive_status(cost)
        self.db.flush()
        self.db.refresh(cost)
        return cost
