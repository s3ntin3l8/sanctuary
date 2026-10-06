"""Pure helpers behind Settings → AI & models and the AI debug-log browser."""

import pytest

from app.services.ai_settings_service import categorize_models, debug_log_path

pytestmark = pytest.mark.unit


def test_categorize_models_by_name_heuristics():
    cats = categorize_models(
        ["qwen3.5:9b", "nomic-embed-text", "qwen-vl", "chandra-ocr"]
    )
    assert cats["embed"] == ["nomic-embed-text"]
    assert cats["ocr"] == ["qwen-vl", "chandra-ocr"]
    # Vision models can drive chat too; embedding models never do.
    assert cats["chat"] == ["qwen3.5:9b", "qwen-vl", "chandra-ocr"]


def test_debug_log_path_mirrors_writer_layout():
    assert (
        debug_log_path({"kind": "doc", "scope_id": 7, "batch_id": 42})
        == "ib-0042/doc_7.md"
    )
    assert (
        debug_log_path({"kind": "batch", "scope_id": 42, "batch_id": 42})
        == "ib-0042/batch_42.md"
    )
    assert debug_log_path({"kind": "case", "scope_id": "ADV-1"}) == "case_ADV-1.md"
    assert debug_log_path({"kind": "doc", "scope_id": 7}) == "unbatched/doc_7.md"
    assert debug_log_path({"scope_id": 7}) == "unbatched/misc_7.md"
    for stage in ("ocr", "embed", "slice"):
        assert debug_log_path({"kind": "doc", "scope_id": 1, "stage": stage}) is None


@pytest.mark.asyncio
async def test_fetch_models_unreachable_endpoint_yields_every_category():
    from unittest.mock import AsyncMock, patch

    from app.services.ai_settings_service import fetch_models

    with patch(
        "app.services.ai_provider.detect_provider",
        AsyncMock(side_effect=RuntimeError("down")),
    ):
        cats = await fetch_models(
            {"base_url": "http://127.0.0.1:9", "provider": "auto"}
        )
    assert cats == {"chat": [], "embed": [], "ocr": []}
