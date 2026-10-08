"""Postgres advisory locks for read-then-write sections the schema can't enforce.

Use these when uniqueness depends on application logic (normalised names, party
snapping, payload-wide collapsing) and so can't be a DB constraint. The lock is
transaction-scoped: it is released automatically on commit or rollback.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

# Namespaces (the first int4 of the two-key advisory lock), one per protected
# section, so unrelated sections never contend on the same hashed key.
ENTITIES_NS = 0x454E54  # "ENT"


def advisory_xact_lock(db: Session, namespace: int, key: str) -> None:
    """Block until this transaction holds the lock for (namespace, key)."""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
        {"ns": namespace, "key": key},
    )
