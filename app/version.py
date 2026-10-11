"""Build identity: release version, git commit and build date.

The Docker image gets these as ``SANCTUARY_*`` env vars (build args in the
publish workflows). From a source checkout they fall back to the release-please
manifest and ``git``; anything still unknown reports ``"unknown"``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from app.config import PROJECT_ROOT

UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildInfo:
    version: str
    git_sha: str
    build_date: str


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _manifest_version() -> str | None:
    try:
        manifest = json.loads(
            (PROJECT_ROOT / ".release-please-manifest.json").read_text()
        )
    except (OSError, ValueError):
        return None
    version = manifest.get(".") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) and version else None


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


@lru_cache(maxsize=1)
def get_build_info() -> BuildInfo:
    """Resolved once per process; env vars win over the checkout."""
    return BuildInfo(
        version=_env("SANCTUARY_VERSION") or _manifest_version() or UNKNOWN,
        git_sha=_env("SANCTUARY_GIT_SHA")
        or _git("rev-parse", "--short=9", "HEAD")
        or UNKNOWN,
        build_date=_env("SANCTUARY_BUILD_DATE")
        or _git("log", "-1", "--format=%cI")
        or UNKNOWN,
    )
