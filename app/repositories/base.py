from typing import Any, TypeVar

from sqlalchemy.orm import Session

from app.models.database import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository[ModelType: Base]:
    """Base repository with common CRUD operations."""

    def __init__(self, model: type[ModelType], db: Session):
        self.model = model
        self.db = db

    def get(self, id: int, options: list | None = None) -> ModelType | None:
        """Get single record by ID."""
        query = self.db.query(self.model)
        if options:
            query = query.options(*options)
        return query.filter(self.model.id == id).first()

    def get_by(self, options: list | None = None, **filters) -> ModelType | None:
        """Get single record by arbitrary filters."""
        query = self.db.query(self.model)
        if options:
            query = query.options(*options)
        return query.filter_by(**filters).first()

    def get_all(self, options: list | None = None, **filters) -> list[ModelType]:
        """Get all records matching filters."""
        query = self.db.query(self.model)
        if options:
            query = query.options(*options)
        if filters:
            query = query.filter_by(**filters)
        return query.all()

    def get_paginated(
        self, offset: int = 0, limit: int = 50, options: list | None = None, **filters
    ) -> list[ModelType]:
        """Get paginated records."""
        query = self.db.query(self.model)
        if options:
            query = query.options(*options)
        if filters:
            query = query.filter_by(**filters)
        return query.offset(offset).limit(limit).all()

    def count(self, **filters) -> int:
        """Count records matching filters."""
        query = self.db.query(self.model)
        if filters:
            query = query.filter_by(**filters)
        return query.count()

    def exists(self, **filters) -> bool:
        """Check if record exists."""
        return self.count(**filters) > 0

    def create(self, **kwargs) -> ModelType:
        """Create new record."""
        instance = self.model(**kwargs)
        self.db.add(instance)
        self.db.flush()
        self.db.refresh(instance)
        return instance

    def update(self, id: int, **kwargs) -> ModelType | None:
        """Update record by ID."""
        instance = self.get(id)
        if instance:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            self.db.flush()
            self.db.refresh(instance)
        return instance

    def delete(self, id: int) -> bool:
        """Delete record by ID."""
        instance = self.get(id)
        if instance:
            self.db.delete(instance)
            self.db.flush()
            return True
        return False

    def bulk_create(self, instances: list[dict]) -> list[ModelType]:
        """Create multiple records."""
        created = []
        for kwargs in instances:
            instance = self.model(**kwargs)
            self.db.add(instance)
            created.append(instance)
        self.db.flush()
        return created

    def execute(self, statement) -> Any:
        """Execute raw SQL statement."""
        return self.db.execute(statement)
