from sqlalchemy.orm import Session

from app.models.database import AuditLog
from app.models.enums import AuditEventType


def record(
    db: Session,
    event_type: AuditEventType,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict | None = None,
    actor_user_id: int | None = None,
    actor_label: str | None = None,
) -> None:
    db.add(
        AuditLog(
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            actor_user_id=actor_user_id,
            actor_label=actor_label,
        )
    )
    db.flush()
