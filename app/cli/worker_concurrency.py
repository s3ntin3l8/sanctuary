"""Print the saved `ai`-worker concurrency to stdout.

Run by docker/worker-ai-entrypoint.sh at container boot to resolve the
`--concurrency` value from the DB (the single source of truth, set via the
Settings UI). Prints nothing useful and exits non-zero if the DB can't be read
yet (e.g. pre-migration) — the entrypoint falls back to a default in that case.

    python -m app.cli.worker_concurrency
"""

from __future__ import annotations

from app.config import SessionLocal
from app.services.user_settings_service import get_worker_concurrency


def main() -> None:
    db = SessionLocal()
    try:
        print(get_worker_concurrency(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
