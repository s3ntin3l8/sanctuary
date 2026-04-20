from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.models.database import Entity
from app.models.enums import EntityType
from app.repositories.base import BaseRepository


class EntityRepository(BaseRepository[Entity]):
    """Repository for Entity operations."""

    def __init__(self, db: Session):
        super().__init__(Entity, db)

    def get_by_case(self, case_id: str) -> Sequence[Entity]:
        """Get all entities for a case."""
        return self.db.query(Entity).filter(Entity.case_id == case_id).all()

    def get_paginated(
        self,
        page: int = 1,
        per_page: int = 50,
        case_id: str | None = None,
        entity_type: EntityType | None = None,
    ) -> tuple[Sequence[Entity], int]:
        """Get paginated entities with total count."""
        query = self.db.query(Entity)

        if case_id:
            query = query.filter(Entity.case_id == case_id)

        if entity_type:
            query = query.filter(Entity.type == entity_type)

        total = query.count()

        entities = (
            query.order_by(Entity.name.asc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return entities, total

    def get_by_case_and_type(
        self, case_id: str, entity_type: EntityType
    ) -> Sequence[Entity]:
        """Get entities for case by type."""
        return (
            self.db.query(Entity)
            .filter(Entity.case_id == case_id)
            .filter(Entity.type == entity_type)
            .all()
        )

    def get_by_type(self, entity_type: EntityType) -> Sequence[Entity]:
        """Get entities by type."""
        return self.db.query(Entity).filter(Entity.type == entity_type).all()

    def search_by_name(self, name: str) -> Sequence[Entity]:
        """Search entities by name."""
        return self.db.query(Entity).filter(Entity.name.ilike(f"%{name}%")).all()

    def get_people(self, case_id: str) -> Sequence[Entity]:
        """Get person entities for a case."""
        return self.get_by_case_and_type(case_id, EntityType.PERSON)

    def get_organizations(self, case_id: str) -> Sequence[Entity]:
        """Get organization entities for a case."""
        return self.get_by_case_and_type(case_id, EntityType.ORGANIZATION)

    def get_dates(self, case_id: str) -> Sequence[Entity]:
        """Get date entities for a case."""
        return self.get_by_case_and_type(case_id, EntityType.DATE)

    def get_financial(self, case_id: str) -> Sequence[Entity]:
        """Get financial entities for a case."""
        return self.get_by_case_and_type(case_id, EntityType.FINANCIAL)

    def create_entity(
        self,
        case_id: str,
        entity_type: EntityType,
        name: str,
        source_document_id: int | None = None,
        extra_data: dict | None = None,
    ) -> Entity:
        """Create a new entity."""
        return self.create(
            case_id=case_id,
            type=entity_type,
            name=name,
            source_document_id=source_document_id,
            extra_data=extra_data,
        )

    def delete_by_case(self, case_id: str) -> int:
        """Delete all entities for a case (bulk delete)."""
        result = (
            self.db.query(Entity)
            .filter(Entity.case_id == case_id)
            .delete(synchronize_session=False)
        )
        self.db.flush()
        return result
