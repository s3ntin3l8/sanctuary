from collections.abc import Generator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app import config
from app.config import SessionLocal
from app.models.database import User


def get_db_session() -> Session:
    """Return a new database session from SessionLocal."""
    return SessionLocal()


def get_db() -> Generator[Session, None, None]:
    db = get_db_session()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Return the authenticated user for the request.

    The auth gate middleware enforces authentication before routes run and
    stashes the resolved user on ``request.state.current_user``; we reuse it
    when present to avoid a second DB round-trip. When AUTH_ENABLED is false we
    auto-bind the bootstrap admin so there is exactly one current-user code path.
    Raises 401 only as a defensive fallback (the gate should have redirected).
    """
    from app.services import auth_service

    cached = getattr(request.state, "current_user", None)
    if isinstance(cached, User):
        return cached

    # The gate validated the session and stashed the uid; load the ORM object
    # with this request's db so it stays attached for the response lifetime.
    uid = getattr(request.state, "auth_user_id", None)
    if isinstance(uid, int):
        user = db.get(User, uid)
        if user is not None:
            request.state.current_user = user
            return user

    # Fallbacks for code paths the gate didn't cover (e.g. tests hitting routes
    # directly, or AUTH disabled without the gate having run).
    if not config.AUTH_ENABLED:
        user = auth_service.get_or_create_bootstrap_admin(db)
        db.commit()
        if user is None:
            # Fresh DB, no admin yet: send to the one-time create-admin screen.
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/signup"}
            )
        request.state.current_user = user
        return user

    user = auth_service.resolve_session_user(db, getattr(request, "session", None))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    request.state.current_user = user
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """Require the current user to be an admin (403 otherwise)."""
    from app.models.enums import UserRole

    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )
    return user


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
