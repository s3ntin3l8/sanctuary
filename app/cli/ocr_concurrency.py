"""Print the saved OCR-slot concurrency to stdout, seeding the Redis limit key.

Run by docker/worker-ingest-entrypoint.sh at container boot to resolve the
`--concurrency` value from the DB (the single source of truth, set via the
Settings UI). Also publishes the value to the ocr_slots Redis limit key so
the per-page semaphore has a live limit even before any Settings save — a
Redis restart (e.g. plain `make run`'s `docker compose up redis`) would
otherwise leave that key absent until someone opens Settings. Prints nothing
useful and exits non-zero if the DB can't be read yet (e.g. pre-migration) —
the entrypoint falls back to a default in that case.

    python -m app.cli.ocr_concurrency
"""

from __future__ import annotations

from app.config import SessionLocal
from app.services.ocr_slots import set_limit
from app.services.user_settings_service import get_ocr_concurrency


def main() -> None:
    db = SessionLocal()
    try:
        n = get_ocr_concurrency(db)
    finally:
        db.close()
    set_limit(n)
    print(n)


if __name__ == "__main__":
    main()
