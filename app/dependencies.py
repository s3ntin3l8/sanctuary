from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from app.config import SessionLocal


def get_db_session() -> Session:
    """Return a new database session from SessionLocal."""
    return SessionLocal()


def get_db() -> Generator[Session, None, None]:
    db = get_db_session()
    try:
        yield db
    finally:
        db.close()


def get_document_repo(db: Session = Depends(get_db)):
    from app.repositories.document import DocumentRepository

    return DocumentRepository(db)


def get_ingest_batch_repo(db: Session = Depends(get_db)):
    from app.repositories.ingest_batch import IngestBatchRepository

    return IngestBatchRepository(db)


def get_user_reaction_repo(db: Session = Depends(get_db)):
    from app.repositories.user_reaction import UserReactionRepository

    return UserReactionRepository(db)


def get_action_item_repo(db: Session = Depends(get_db)):
    from app.repositories.action_item import ActionItemRepository

    return ActionItemRepository(db)


def get_triage_service(db: Session = Depends(get_db)):
    from app.services.triage_service import TriageService

    return TriageService(db)
