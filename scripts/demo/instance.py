"""A throwaway, isolated Sanctuary with seeded sample data, for UI audits.

    make demo          # serve it until Ctrl-C / SIGTERM, then delete everything
    make demo KEEP=1   # keep the scratch directory on exit
    DEMO_AUTH=1 make demo   # login screen on, for /login and /signup

Everything lives in one mkdtemp directory: a copy of the code (so `app/config.py`
resolves PROJECT_ROOT, `.env`, `data/` and the log folder inside it), HOME and
TMPDIR. The database is a throwaway pgvector container on a loopback port. The
children get an environment built from scratch (scripts/demo/env.py refuses
anything that points at the dev stack). This module and env.py never import
`app.*`: importing `app.config` would create `<repo>/data` and a database engine.

Prints `DEMO_READY url=...` only after migration, seeding and the first answer
from the server, then runs until SIGINT/SIGTERM (the capture script sends TERM).
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from cryptography.fernet import Fernet

from scripts.demo.env import (
    DEMO_DB_NAME,
    UNREACHABLE_AI,
    UNREACHABLE_REDIS,
    assert_demo_env,
    assert_ports_free,
    demo_env,
)

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
DEMO_PORT = int(os.environ.get("DEMO_PORT", "3985"))
DEMO_DB_PORT = int(os.environ.get("DEMO_DB_PORT", "3986"))
DB_IMAGE = "pgvector/pgvector:pg17"

# What the app needs at run time; the rest of the repo (tests, frontend sources,
# the repo's .env and data/) stays out of the scratch tree.
TREE_ENTRIES = (
    "app",
    "alembic",
    "alembic.ini",
    "scripts",
    "static",
    ".release-please-manifest.json",
)


def _wait_for_http(url: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as res:  # noqa: S310 - loopback URL we built
                if res.status < 500:
                    return
        except urllib.error.HTTPError as e:  # urlopen raises for every 4xx/5xx
            if e.code < 500:
                return
            last = e
        except OSError as e:  # URLError, ConnectionError and slow-accept TimeoutError
            last = e
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}: {last}")


# Everything `npm run build` reads: a change to any of these makes dist stale.
SPA_INPUT_DIRS = ("src",)
SPA_INPUT_GLOBS = (
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.*",
    "tsconfig*.json",
)


def _mtime(path: Path) -> float:
    # A dangling or looping symlink has no target to stat; it can't be a build input.
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def spa_is_stale(frontend: Path) -> bool:
    """True when frontend/dist is missing or older than any of its build inputs.

    Directories count too: deleting a file only bumps its parent's mtime. The
    check is mtime-based, so an mtime-preserving restore goes unnoticed.
    """
    index = frontend / "dist" / "index.html"
    if not index.exists():
        return True
    built = index.stat().st_mtime
    paths = [frontend]
    for d in SPA_INPUT_DIRS:
        if (frontend / d).is_dir():
            paths += [frontend / d, *(frontend / d).rglob("*")]
    paths += [
        p for pattern in SPA_INPUT_GLOBS for p in frontend.glob(pattern) if p.is_file()
    ]
    return any(_mtime(p) > built for p in paths)


def ensure_spa_built() -> None:
    """The server serves frontend/dist; (re)build it when missing or stale."""
    frontend = ROOT / "frontend"
    if not spa_is_stale(frontend):
        return
    print("[demo] building the SPA (frontend/dist)...", flush=True)
    subprocess.run(["npm", "--prefix", str(frontend), "run", "build"], check=True)


@dataclass
class Demo:
    demo_dir: Path
    keep: bool = False
    container: str | None = None
    server: subprocess.Popen[bytes] | None = None
    url: str = ""
    _stopped: bool = field(default=False, repr=False)

    def stop(self) -> None:
        """Idempotent teardown: server group, container, scratch directory."""
        if self._stopped:
            return
        self._stopped = True
        if self.server is not None:
            if self.server.poll() is None:
                # Own session (start_new_session), so the whole group goes. It can
                # exit between poll() and killpg; that must not mask the real error.
                try:
                    os.killpg(self.server.pid, signal.SIGTERM)
                    try:
                        self.server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.server.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.server.wait()
        if self.container is not None:
            subprocess.run(
                ["docker", "rm", "-f", self.container],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if self.keep:
            print(f"[demo] kept {self.demo_dir}", flush=True)
            return
        shutil.rmtree(self.demo_dir, ignore_errors=True)


def _start_database(demo: Demo, password: str) -> None:
    name = f"sanctuary-demo-{uuid.uuid4().hex[:8]}"
    # Set before `docker run` returns so a failure between create and start still
    # gets the container removed by teardown.
    demo.container = name
    print(f"[demo] starting the throwaway database ({name})...", flush=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", f"127.0.0.1:{DEMO_DB_PORT}:5432",
            "-e", "POSTGRES_USER=sanctuary",
            "-e", f"POSTGRES_PASSWORD={password}",
            "-e", f"POSTGRES_DB={DEMO_DB_NAME}",
            DB_IMAGE,
        ],
        stdout=subprocess.DEVNULL,
        check=True,
    )  # fmt: skip
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = subprocess.run(
            [
                "docker",
                "exec",
                name,
                "pg_isready",
                "-U",
                "sanctuary",
                "-d",
                DEMO_DB_NAME,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("the throwaway database did not become ready")


def _copy_tree(tree: Path) -> None:
    tree.mkdir()
    for entry in TREE_ENTRIES:
        src = ROOT / entry
        if src.is_dir():
            shutil.copytree(
                src, tree / entry, ignore=shutil.ignore_patterns("__pycache__")
            )
        elif src.exists():
            shutil.copy2(src, tree / entry)


def start_demo(*, keep: bool = False, auth: bool = False) -> Demo:
    assert_ports_free([DEMO_PORT, DEMO_DB_PORT])
    if DEMO_PORT == DEMO_DB_PORT:
        raise RuntimeError("DEMO_PORT and DEMO_DB_PORT must differ")
    ensure_spa_built()

    demo_dir = Path(tempfile.mkdtemp(prefix="sanctuary-demo-"))
    demo = Demo(demo_dir=demo_dir, keep=keep)
    try:
        tree = demo_dir / "tree"  # child cwd and PROJECT_ROOT: no .env here
        for sub in ("home", "tmp"):
            (demo_dir / sub).mkdir()
        _copy_tree(tree)

        db_password = secrets.token_urlsafe(16)
        env = demo_env(
            demo_dir,
            {
                "PYTHONPATH": str(tree),
                "HOST": "127.0.0.1",
                "PORT": str(DEMO_PORT),
                "DATABASE_URL": (
                    f"postgresql+psycopg://sanctuary:{db_password}"
                    f"@127.0.0.1:{DEMO_DB_PORT}/{DEMO_DB_NAME}"
                ),
                "CELERY_TASK_ALWAYS_EAGER": "true",
                "AUTH_ENABLED": "true" if auth else "false",
                # The login screen needs somewhere to go: /signup renders when open.
                **({"AUTH_SIGNUP_ENABLED": "true"} if auth else {}),
                "SESSION_SECRET": secrets.token_urlsafe(32),
                "SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "REDIS_URL": UNREACHABLE_REDIS,
                "RATE_LIMIT_STORAGE_URI": "memory://",
                "AI_BASE_URL": UNREACHABLE_AI,
                "TIMEZONE": "Europe/Berlin",
                "LOG_LEVEL": "ERROR",
                "FRONTEND_DIST": str(ROOT / "frontend" / "dist"),
            },
        )
        assert_demo_env(env, demo_dir, cwd=tree, db_port=DEMO_DB_PORT, auth=auth)
        print(f"[demo] scratch dir {demo_dir}", flush=True)

        _start_database(demo, db_password)

        print("[demo] migrating...", flush=True)
        subprocess.run(
            [PYTHON, "-m", "alembic", "upgrade", "head"], cwd=tree, env=env, check=True
        )
        print("[demo] seeding demo data...", flush=True)
        subprocess.run(
            [PYTHON, "scripts/seed_dummy_data.py"], cwd=tree, env=env, check=True
        )

        print("[demo] starting the server...", flush=True)
        demo.server = subprocess.Popen(
            [
                PYTHON, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1",
                "--port", str(DEMO_PORT),
                "--no-proxy-headers",
            ],
            cwd=tree,
            env=env,
            start_new_session=True,
        )  # fmt: skip
        demo.url = f"http://127.0.0.1:{DEMO_PORT}"
        _wait_for_http(f"{demo.url}/")
        return demo
    except BaseException:
        demo.stop()
        raise


class _Terminated(BaseException):
    """Raised from the signal handler so teardown runs through the normal path."""


def _raise_terminated(signum: int, frame: FrameType | None) -> None:
    raise _Terminated(signal.Signals(signum).name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep", action="store_true", help="keep the scratch dir on exit"
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        default=os.environ.get("DEMO_AUTH") == "1",
        help="login screen on (also DEMO_AUTH=1), to capture /login and /signup",
    )
    args = parser.parse_args()
    # Before anything is started: the capture script stops us with SIGTERM, and
    # Python's default would die without running the teardown.
    signal.signal(signal.SIGTERM, _raise_terminated)
    signal.signal(signal.SIGINT, _raise_terminated)

    demo: Demo | None = None
    try:
        demo = start_demo(keep=args.keep, auth=args.auth)
        print(f"DEMO_READY url={demo.url}", flush=True)
        print(
            "[demo] Loopback only, "
            + (
                "login on (admin@localhost / the seed's dev password)."
                if args.auth
                else "no login."
            )
            + " Ctrl-C stops it and deletes all demo data.",
            flush=True,
        )
        assert demo.server is not None
        demo.server.wait()
    except _Terminated:
        pass  # SIGINT/SIGTERM are the documented way to stop; teardown runs below.
    finally:
        # A second signal during teardown must not abort it halfway.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if demo is not None:
            demo.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
