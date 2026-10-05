"""Write the ``/api/v1`` OpenAPI schema the SPA's TypeScript types are generated from.

Usage: ``python scripts/export_openapi.py`` (or ``make api-types``, which also
regenerates ``frontend/src/api/schema.d.ts``). Only ``/api/v1`` is exported:
the legacy routes return HTML and have no meaningful schema.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OPENAPI_PATH = (
    Path(__file__).resolve().parent.parent / "frontend" / "src" / "api" / "openapi.json"
)


def _collect_refs(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _collect_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, found)


def build_schema() -> dict:
    """The app's OpenAPI document reduced to /api/v1 and the schemas it uses."""
    from app.api.v1 import API_V1_PREFIX
    from app.main import app

    full = app.openapi()
    paths = {p: v for p, v in full["paths"].items() if p.startswith(API_V1_PREFIX)}
    all_schemas = full.get("components", {}).get("schemas", {})
    needed: set[str] = set()
    _collect_refs(paths, needed)
    while True:
        before = len(needed)
        for name in list(needed):
            _collect_refs(all_schemas.get(name), needed)
        if len(needed) == before:
            break
    return {
        "openapi": full["openapi"],
        "info": {"title": "Sanctuary API", "version": "1"},
        "paths": paths,
        "components": {"schemas": {n: all_schemas[n] for n in sorted(needed)}},
    }


def render_schema() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OPENAPI_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(render_schema(), encoding="utf-8")
    print(f"wrote {OPENAPI_PATH}")
