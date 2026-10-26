"""Run a Python module or script with the clock fixed to DEMO_NOW.

    python -m scripts.demo.freeze_time alembic upgrade head
    python -m scripts.demo.freeze_time scripts/seed_dummy_data.py
    python -m scripts.demo.freeze_time uvicorn app.main:app --port 3985

The demo's relative dates ("due in 3 days", overdue deadlines, "arrived 2 hours ago") are computed
from the clock, so two runs on different days would render different text. With DEMO_NOW set, the
seed and the server run under `time-machine` starting at that instant (it keeps ticking, so
timeouts, sessions and ordering still behave). Nothing in the app is touched: every timestamp is
produced in Python (no DB-side `now()` defaults), so one patch covers it.

The browser needs the same instant: `clock` in `visual/scenes.json` (visual-kit installs it with
`context.clock.setFixedTime`). Without DEMO_NOW this just runs the target.

Never import `app.*` from here (scripts/demo must not create `data/` or an engine at import).
"""

from __future__ import annotations

import os
import re
import runpy
import sys
from datetime import UTC, datetime

import time_machine


def parse_demo_now(value: str) -> datetime:
    """`DEMO_NOW` as an aware datetime; a value without an offset is UTC. A bare date is refused: it
    would read as 00:00 UTC, a day off whatever `clock` the scenes pair it with."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(
            f"DEMO_NOW={value!r} is not an ISO 8601 date-time (e.g. 2026-10-01T09:00:00Z)"
        ) from None
    # A time of day follows a `T` (or a space) separator; ISO basic format (20261001T090000) has no colon.
    if not re.search(r"[T ]", value.strip(), re.IGNORECASE):
        raise SystemExit(
            f"DEMO_NOW={value!r} has no time of day (e.g. 2026-10-01T09:00:00Z)"
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: list[str]) -> None:
    if not argv:
        raise SystemExit(
            "usage: python -m scripts.demo.freeze_time <module|script.py> [args...]"
        )
    target, *rest = argv
    raw = os.environ.get("DEMO_NOW", "").strip()
    if raw:
        time_machine.travel(parse_demo_now(raw), tick=True).start()
    sys.argv = [target, *rest]
    if target.endswith(".py"):
        runpy.run_path(target, run_name="__main__")
    else:
        runpy.run_module(target, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main(sys.argv[1:])
