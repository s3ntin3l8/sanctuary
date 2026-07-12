from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.database import LegalCost
from app.models.enums import CostCategory, CostStatus
from app.repositories.base import BaseRepository


class LegalCostRepository(BaseRepository[LegalCost]):
    """Repository for LegalCost operations."""

    def __init__(self, db: Session):
        super().__init__(LegalCost, db)

    def get_by_case(
        self, case_id: str, proceeding_id: int | None = None
    ) -> Sequence[LegalCost]:
        """Get all costs for a case, optionally filtered by proceeding."""
        query = self.db.query(LegalCost).filter(LegalCost.case_id == case_id)
        if proceeding_id:
            query = query.filter(LegalCost.proceeding_id == proceeding_id)
        return query.order_by(LegalCost.issued_at.asc()).all()

    def get_by_category(self, category: CostCategory) -> Sequence[LegalCost]:
        """Get costs by category."""
        return self.db.query(LegalCost).filter(LegalCost.category == category).all()

    def get_by_status(self, status: CostStatus) -> Sequence[LegalCost]:
        """Get costs by status."""
        return self.db.query(LegalCost).filter(LegalCost.status == status).all()

    def get_pending(self) -> Sequence[LegalCost]:
        """Get pending (unpaid) costs."""
        return (
            self.db.query(LegalCost).filter(LegalCost.status == CostStatus.OFFEN).all()
        )

    def get_by_case_and_status(
        self, case_id: str, status: CostStatus
    ) -> Sequence[LegalCost]:
        """Get costs for case by status."""
        return (
            self.db.query(LegalCost)
            .filter(LegalCost.case_id == case_id)
            .filter(LegalCost.status == status)
            .all()
        )

    def get_by_case_and_category(
        self, case_id: str, category: CostCategory
    ) -> Sequence[LegalCost]:
        """Get costs for case by category."""
        return (
            self.db.query(LegalCost)
            .filter(LegalCost.case_id == case_id)
            .filter(LegalCost.category == category)
            .all()
        )

    def sum_amounts_by_case(self, case_id: str) -> dict:
        """Sum all amounts for a case using SQL aggregation."""
        result = (
            self.db.query(
                func.sum(LegalCost.amount_net).label("net"),
                func.sum(LegalCost.amount_gross).label("gross"),
                func.sum(LegalCost.amount_paid).label("paid"),
                func.sum(LegalCost.amount_reimbursed).label("reimbursed"),
            )
            .filter(LegalCost.case_id == case_id)
            .first()
        )
        if result is None:
            return {"net": 0, "gross": 0, "paid": 0, "reimbursed": 0}
        return {
            "net": result.net or 0,
            "gross": result.gross or 0,
            "paid": result.paid or 0,
            "reimbursed": result.reimbursed or 0,
        }

    def bulk_sum_by_cases(self, case_ids: list[str]) -> dict[str, dict]:
        """Bulk sum amounts for multiple cases (avoids N+1)."""
        results = (
            self.db.query(
                LegalCost.case_id,
                func.sum(LegalCost.amount_net).label("net"),
                func.sum(LegalCost.amount_gross).label("gross"),
                func.sum(LegalCost.amount_paid).label("paid"),
                func.sum(LegalCost.amount_reimbursed).label("reimbursed"),
            )
            .filter(LegalCost.case_id.in_(case_ids))
            .group_by(LegalCost.case_id)
            .all()
        )
        return {
            r.case_id: {
                "net": r.net or 0,
                "gross": r.gross or 0,
                "paid": r.paid or 0,
                "reimbursed": r.reimbursed or 0,
            }
            for r in results
        }

    def count_by_case(self, case_id: str) -> int:
        """Count costs for a case."""
        return self.db.query(LegalCost).filter(LegalCost.case_id == case_id).count()

    def create_cost(
        self,
        case_id: str,
        category: CostCategory,
        title: str,
        amount_net: float,
        amount_gross: float,
        status: CostStatus = CostStatus.OFFEN,
        vat_rate: float = 0.0,
        rvg_position: str | None = None,
        streitwert: float | None = None,
        gebuehren_faktor: float | None = None,
        notes: str | None = None,
        is_reimbursable: bool = True,
        issued_at: datetime | None = None,
        due_at: datetime | None = None,
        source_document_id: int | None = None,
        proceeding_id: int | None = None,
    ) -> LegalCost:
        """Create a new cost."""
        return self.create(
            case_id=case_id,
            category=category,
            title=title,
            amount_net=amount_net,
            amount_gross=amount_gross,
            status=status,
            vat_rate=vat_rate,
            rvg_position=rvg_position,
            streitwert=streitwert,
            gebuehren_faktor=gebuehren_faktor,
            notes=notes,
            is_reimbursable=is_reimbursable,
            issued_at=issued_at,
            due_at=due_at,
            source_document_id=source_document_id,
            proceeding_id=proceeding_id,
            ingest_date=datetime.now(),
        )

    def update_status(
        self, cost_id: int, status: CostStatus, paid_at: datetime | None = None
    ) -> LegalCost | None:
        """Update cost status."""
        updates: dict[str, CostStatus | datetime] = {"status": status}
        if paid_at:
            updates["paid_at"] = paid_at
        return self.update(cost_id, **updates)

    def get_paginated(  # type: ignore[override]  # LegalCostRepository intentionally specializes the generic base signature for LegalCost-specific filters
        self,
        page: int = 1,
        per_page: int = 20,
        case_id: str | None = None,  # type: ignore[override]  # LegalCostRepository intentionally specializes the generic base signature for LegalCost-specific filters
        status: CostStatus | None = None,
    ) -> tuple[Sequence[LegalCost], int]:
        """Get paginated costs with total count."""
        query = self.db.query(LegalCost)

        if case_id:
            query = query.filter(LegalCost.case_id == case_id)

        if status:
            query = query.filter(LegalCost.status == status)

        total = query.count()

        costs = (
            query.order_by(LegalCost.due_at.asc().nullsfirst())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return costs, total
