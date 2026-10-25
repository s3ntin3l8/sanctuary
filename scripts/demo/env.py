"""Environment isolation for the throwaway demo instance (`make demo`).

The demo runs on a host that also runs the real Sanctuary stack (compose, with
`./data` bind-mounted into the containers), from a developer shell full of
secrets. The concrete hazards:

- `app/config.py` loads `PROJECT_ROOT/.env` (the code's location, not the cwd),
  and the repo `.env` carries the live instance's SECRETS_ENCRYPTION_KEY and
  Postgres password. The Makefile also `-include`s and `export`s it into every
  recipe, so the child environment is the only place this can be stopped.
- With DATABASE_URL unset the app builds one from POSTGRES_* on localhost:5432,
  i.e. the dev database; REDIS_URL defaults to the dev Redis.
- `DATA_DIR` is `PROJECT_ROOT/data`, which the live containers write to.
- `AI_BASE_URL` defaults to a real local Ollama.

So every demo child gets an environment built from scratch (only PATH and
locale are inherited), the launcher runs a copy of the code from inside the
scratch directory (so `PROJECT_ROOT` is scratch too), and `assert_demo_env`
refuses to start if anything still points elsewhere.

This module must not import `app.*`: importing `app.config` creates `data/`
and the database engine.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TERM", "TZ")

DEMO_DB_NAME = "sanctuary_demo"
# Refuses instantly (nothing listens on port 1), so kombu/redis clients never stall.
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNREACHABLE_AI = "http://127.0.0.1:9"

# Keys the demo sets itself with values it generates for this run.
GENERATED_SECRETS: frozenset[str] = frozenset(
    {"SESSION_SECRET", "SECRETS_ENCRYPTION_KEY"}
)
SECRET_KEY = re.compile(r"(SECRET|PASSWORD|_KEY|_TOKEN)$|ENCRYPTION")
PATH_KEY = re.compile(r"(_DIR|_PATH|_HOME|_DIST)$")
# Path-ish keys that legitimately point outside the scratch dir: the built SPA
# (read-only) and the Playwright browser cache.
PATH_ALLOW = frozenset({"FRONTEND_DIST", "PLAYWRIGHT_BROWSERS_PATH"})
# Where Python looks for `app`: the seed runs as a script, so PYTHONPATH alone
# decides whether it imports the scratch copy or the live repo (and its .env).
PYTHON_PATH_KEYS = ("PYTHONPATH", "PYTHONHOME")
MUST_BE_INSIDE = ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
# Integrations the demo leaves unconfigured.
FORBIDDEN_PREFIXES = ("POSTGRES_", "GMAIL_", "OIDC_", "BOOTSTRAP_ADMIN_", "MCP_")
# Ports of the real dev stack (Postgres, Redis, app); a demo must never take them.
RESERVED_PORTS = frozenset({"5432", "6379", "8000"})
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def demo_env(
    demo_dir: Path,
    extra: Mapping[str, str] | None = None,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A clean environment for a demo child process."""
    parent = os.environ if parent is None else parent
    env = {k: parent[k] for k in PASSTHROUGH if k in parent}
    env.update(
        {
            "HOME": str(demo_dir / "home"),
            "TMPDIR": str(demo_dir / "tmp"),
            "XDG_CONFIG_HOME": str(demo_dir / "home" / ".config"),
            "XDG_DATA_HOME": str(demo_dir / "home" / ".local" / "share"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.update(extra or {})
    return env


def _inside(base: Path, value: str) -> bool:
    p = Path(value)
    if not p.is_absolute():
        return False
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _database_problems(url: str | None, db_port: int | None) -> list[str]:
    if not url:
        return ["DATABASE_URL must be set (unset it falls back to the dev database)"]
    parts = urlsplit(url)
    problems: list[str] = []
    if parts.hostname != "127.0.0.1":
        problems.append("DATABASE_URL host must be 127.0.0.1")
    port = str(parts.port) if parts.port else ""
    if not port or port in RESERVED_PORTS:
        problems.append(
            f"DATABASE_URL port {port or '(default)'} is reserved for a real instance"
        )
    elif db_port is not None and port != str(db_port):
        problems.append(f"DATABASE_URL port must be the scratch database's {db_port}")
    if parts.path.lstrip("/") != DEMO_DB_NAME:
        problems.append(f"DATABASE_URL database must be {DEMO_DB_NAME}")
    return problems


def demo_env_problems(
    env: Mapping[str, str],
    demo_dir: Path,
    cwd: Path | None = None,
    *,
    db_port: int | None = None,
) -> list[str]:
    """Everything that would let the demo reach outside `demo_dir`."""
    problems: list[str] = []
    for key, value in env.items():
        if key.startswith(FORBIDDEN_PREFIXES):
            problems.append(f"{key} must not be set in the demo")
        if SECRET_KEY.search(key) and key not in GENERATED_SECRETS:
            problems.append(f"{key} looks like a real secret; the demo never sets it")
        if (
            PATH_KEY.search(key)
            and key not in PATH_ALLOW
            and os.path.isabs(value)
            and not _inside(demo_dir, value)
        ):
            problems.append(f"{key}={value} is outside the demo dir")
    for key in PYTHON_PATH_KEYS:
        if key in env and not all(
            _inside(demo_dir, part) for part in env[key].split(os.pathsep)
        ):
            problems.append(f"{key}={env[key]} must point inside the demo dir")
    if "PYTHONPATH" not in env:
        problems.append("PYTHONPATH must put the scratch tree on sys.path")
    for key in MUST_BE_INSIDE:
        if key not in env or not _inside(demo_dir, env[key]):
            problems.append(f"{key} must be set inside the demo dir")
    problems += _database_problems(env.get("DATABASE_URL"), db_port)
    if env.get("REDIS_URL") != UNREACHABLE_REDIS:
        problems.append(f"REDIS_URL must be {UNREACHABLE_REDIS} (never the dev Redis)")
    if env.get("RATE_LIMIT_STORAGE_URI") != "memory://":
        problems.append("RATE_LIMIT_STORAGE_URI must be memory://")
    ai = urlsplit(env.get("AI_BASE_URL", ""))
    if ai.hostname not in LOOPBACK or ai.port == 11434:
        problems.append(
            "AI_BASE_URL must be an unreachable loopback address (not Ollama's)"
        )
    if env.get("HOST") != "127.0.0.1":
        problems.append("HOST must be 127.0.0.1")
    port = env.get("PORT")
    if not port:
        problems.append("PORT must be set")
    elif port in RESERVED_PORTS:
        problems.append(f"PORT {port} is reserved for a real instance")
    if cwd is not None and (cwd / ".env").exists():
        problems.append(f"{cwd} contains a .env that the app would load")
    return problems


def assert_demo_env(
    env: Mapping[str, str],
    demo_dir: Path,
    cwd: Path | None = None,
    *,
    db_port: int | None = None,
) -> None:
    problems = demo_env_problems(env, demo_dir, cwd, db_port=db_port)
    if problems:
        raise RuntimeError("demo environment refused:\n  - " + "\n  - ".join(problems))


def assert_ports_free(
    ports: Iterable[int], hosts: Iterable[str] = ("127.0.0.1", "::1")
) -> None:
    """Raise if anything already listens on one of the ports (likely a real instance)."""
    for port in ports:
        if str(port) in RESERVED_PORTS:
            raise RuntimeError(
                f"port {port} is reserved for a real instance; refusing to start"
            )
        for host in hosts:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
            except OSError:  # no IPv6 on this host: nothing can listen there
                continue
            with sock:
                sock.settimeout(0.5)
                if sock.connect_ex((host, int(port))) == 0:
                    raise RuntimeError(
                        f"port {port} is already in use on {host}; refusing to start the demo"
                    )
