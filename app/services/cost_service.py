from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import LegalCost
from app.models.enums import CostCategory, CostStatus
from app.repositories.legal_cost import LegalCostRepository


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
        """Mark cost as paid."""
        return self.update_cost_status(cost_id, CostStatus.BEZAHLT, datetime.now())

    def mark_as_reimbursed(self, cost_id: int, amount: float) -> LegalCost | None:
        """Mark cost as reimbursed."""
        cost = self.cost_repo.get(cost_id)
        if cost:
            cost.amount_reimbursed = amount
            cost.status = CostStatus.ERSTATTET
            self.db.flush()
            self.db.refresh(cost)
        return cost
