"""The committed /api/v1 schema (frontend/src/api/openapi.json) must match the app."""

import pytest
from scripts.export_openapi import OPENAPI_PATH, render_schema

pytestmark = pytest.mark.unit


def test_committed_openapi_schema_is_current():
    committed = OPENAPI_PATH.read_text(encoding="utf-8")
    assert committed == render_schema(), (
        "frontend/src/api/openapi.json is stale — run `make api-types` and commit "
        "the regenerated openapi.json and schema.d.ts."
    )
