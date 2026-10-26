"""The demo's fixed clock (scripts/demo/freeze_time.py)."""

import subprocess
import sys
from pathlib import Path

import pytest
from scripts.demo import instance
from scripts.demo.freeze_time import parse_demo_now

ROOT = Path(__file__).resolve().parents[2]


def test_demo_now_without_an_offset_is_utc() -> None:
    assert (
        parse_demo_now("2026-10-01T09:00:00").isoformat() == "2026-10-01T09:00:00+00:00"
    )
    assert (
        parse_demo_now("2026-10-01T09:00:00Z").isoformat()
        == "2026-10-01T09:00:00+00:00"
    )
    assert (
        parse_demo_now("2026-10-01T11:00:00+02:00").utcoffset().total_seconds() == 7200
    )


def test_a_bare_date_is_refused() -> None:
    with pytest.raises(SystemExit, match="no time of day"):
        parse_demo_now("2026-10-01")


def test_an_unparsable_demo_now_stops_the_launcher() -> None:
    with pytest.raises(SystemExit, match="not an ISO 8601"):
        parse_demo_now("yesterday")


def test_python_command_goes_through_the_clock_only_when_frozen() -> None:
    plain = instance._python(["-m", "alembic", "upgrade", "head"], frozen=False)
    assert plain[1:] == ["-m", "alembic", "upgrade", "head"]
    frozen = instance._python(["-m", "alembic", "upgrade", "head"], frozen=True)
    assert frozen[1:] == [
        "-m",
        "scripts.demo.freeze_time",
        "alembic",
        "upgrade",
        "head",
    ]
    script = instance._python(["scripts/seed_dummy_data.py"], frozen=True)
    assert script[1:] == [
        "-m",
        "scripts.demo.freeze_time",
        "scripts/seed_dummy_data.py",
    ]


def _run(tmp_path: Path, demo_now: str | None) -> str:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\nfrom datetime import UTC, datetime\n"
        "print(datetime.now(UTC).isoformat(), sys.argv[1:])\n"
    )
    env = {"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}
    if demo_now:
        env["DEMO_NOW"] = demo_now
    done = subprocess.run(
        [sys.executable, "-m", "scripts.demo.freeze_time", str(probe), "a", "b"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return done.stdout.strip()


def test_a_script_runs_with_the_clock_fixed_and_its_arguments(tmp_path: Path) -> None:
    out = _run(tmp_path, "2026-10-01T09:00:00Z")
    assert out.startswith("2026-10-01T09:00:0")
    assert out.endswith("['a', 'b']")


def test_without_demo_now_the_clock_is_untouched(tmp_path: Path) -> None:
    assert not _run(tmp_path, None).startswith("2026-10-01")
