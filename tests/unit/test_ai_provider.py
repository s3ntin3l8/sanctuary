"""Unit tests for ai_provider.py — provider auto-detection, param shaping, stream parsing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_provider import (
    AIProvider,
    ProviderType,
    _make_openai_strict,
    detect_provider,
)

# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_openai_format():
    """Responds with OpenAI list format → LM Studio."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"object": "list", "data": []}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await detect_provider("http://localhost:1234")

    assert result == ProviderType.LMSTUDIO


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_ollama_format():
    """OpenAI probe fails; Ollama probe succeeds."""
    openai_err = Exception("connection refused")
    ollama_resp = MagicMock(status_code=200)
    ollama_resp.json.return_value = {"models": [{"name": "llama3"}]}

    call_count = [0]

    async def side_effect(url, **_):
        call_count[0] += 1
        if "/v1/models" in url:
            raise openai_err
        return ollama_resp

    with patch("httpx.AsyncClient.get", side_effect=side_effect):
        result = await detect_provider("http://localhost:11434")

    assert result == ProviderType.OLLAMA


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_both_fail_raises():
    """Both probes fail → RuntimeError (loud failure beats silent miscategorisation)."""
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=Exception("offline"),
    ):
        with pytest.raises(RuntimeError, match="AI provider unreachable"):
            await detect_provider("http://localhost:9999")


# ---------------------------------------------------------------------------
# AIProvider.get_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_type_explicit_ollama():
    p = AIProvider()
    p.provider = "ollama"
    assert await p.get_type() == ProviderType.OLLAMA


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_type_explicit_openai():
    p = AIProvider()
    p.provider = "openai"
    assert await p.get_type() == ProviderType.OPENAI


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_type_auto_caches_result():
    """Auto mode calls detect_provider once and caches."""
    p = AIProvider()
    p.provider = "auto"

    with patch(
        "app.services.ai_provider.detect_provider",
        new_callable=AsyncMock,
        return_value=ProviderType.OLLAMA,
    ) as mock_detect:
        t1 = await p.get_type()
        t2 = await p.get_type()

    assert t1 == ProviderType.OLLAMA
    assert t2 == ProviderType.OLLAMA
    mock_detect.assert_called_once()


# ---------------------------------------------------------------------------
# AIProvider.get_generate_params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_ollama_shape():
    p = AIProvider()
    p.provider = "ollama"
    p.base_url = "http://localhost:11434"

    params = await p.get_generate_params("llama3", "Hello", system_prompt="Be brief")

    assert params["url"].endswith("/api/generate")
    assert "model" in params["json"]
    assert "Be brief" in params["json"]["prompt"]
    assert params["json"]["stream"] is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_openai_shape():
    p = AIProvider()
    p.provider = "lmstudio"
    p.base_url = "http://localhost:1234"

    params = await p.get_generate_params("gpt-4", "Hello", system_prompt="Be brief")

    assert params["url"].endswith("/v1/chat/completions")
    messages = params["json"]["messages"]
    roles = [m["role"] for m in messages]
    assert "system" in roles
    assert "user" in roles


# ---------------------------------------------------------------------------
# AIProvider.get_embedding_params
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_embedding_params_ollama():
    p = AIProvider()
    p.provider = "ollama"
    p.base_url = "http://localhost:11434"

    params = await p.get_embedding_params("nomic-embed-text", "test content")

    assert params["url"].endswith("/api/embeddings")
    assert params["json"]["model"] == "nomic-embed-text"
    assert params["json"]["prompt"] == "test content"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_embedding_params_openai():
    p = AIProvider()
    p.provider = "openai"
    p.base_url = "http://localhost:1234"

    params = await p.get_embedding_params("text-embedding-3-small", "test content")

    assert params["url"].endswith("/v1/embeddings")
    assert params["json"]["input"] == "test content"


# ---------------------------------------------------------------------------
# AIProvider.parse_stream_line
# ---------------------------------------------------------------------------


def test_parse_stream_line_ollama_normal():
    p = AIProvider()
    result = p.parse_stream_line(
        '{"response": "Hello", "done": false}', ProviderType.OLLAMA
    )
    assert result == {"response": "Hello", "done": False, "thinking": ""}


def test_parse_stream_line_ollama_done():
    p = AIProvider()
    result = p.parse_stream_line('{"done": true}', ProviderType.OLLAMA)
    assert result["done"] is True


def test_parse_stream_line_ollama_invalid_json():
    p = AIProvider()
    result = p.parse_stream_line("not json", ProviderType.OLLAMA)
    assert result is None


def test_parse_stream_line_openai_data():
    p = AIProvider()
    line = 'data: {"choices": [{"delta": {"content": "Hi"}}]}'
    result = p.parse_stream_line(line, ProviderType.LMSTUDIO)
    assert result == {"response": "Hi", "thinking": "", "done": False, "usage": None}


def test_parse_stream_line_openai_done():
    p = AIProvider()
    result = p.parse_stream_line("data: [DONE]", ProviderType.LMSTUDIO)
    assert result == {"done": True}


def test_parse_stream_line_empty():
    p = AIProvider()
    assert p.parse_stream_line("", ProviderType.OLLAMA) is None


# ---------------------------------------------------------------------------
# Structured output: schema injection across provider branches
# ---------------------------------------------------------------------------


_SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {"foo": {"type": "string"}},
    "required": ["foo"],
}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_llamacpp_signature():
    """llama.cpp's /v1/models entries carry owned_by='llamacpp'."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "object": "list",
        "data": [{"id": "qwen", "owned_by": "llamacpp", "object": "model"}],
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await detect_provider("http://localhost:8080")

    assert result == ProviderType.LLAMACPP


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_authenticated_endpoint_classified_as_lmstudio():
    """LiteLLM / OpenAI / hosted gateways return 401 without auth — the server
    exists and speaks the OpenAI surface, so we still classify as LMSTUDIO so
    the request-time auth header gets the chance to succeed."""
    mock_resp = MagicMock(status_code=401)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await detect_provider("http://gateway:4000")

    assert result == ProviderType.LMSTUDIO


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_passes_api_key_through_authorization_header():
    """When an api_key is supplied, detect_provider must forward it as Bearer
    so authenticated gateways respond instead of returning 401."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"object": "list", "data": []}

    captured_headers = {}

    async def capture(url, **kwargs):
        captured_headers.update(kwargs.get("headers") or {})
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=capture):
        await detect_provider("http://gateway:4000", api_key="sk-1234")

    assert captured_headers.get("Authorization") == "Bearer sk-1234"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_skips_auth_header_for_not_needed_sentinel():
    """`api_key="not-needed"` (the unauthenticated-server sentinel) must NOT
    leak as a Bearer token — local LMStudio/Ollama setups would fail on it."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"object": "list", "data": []}

    captured_headers = {}

    async def capture(url, **kwargs):
        captured_headers.update(kwargs.get("headers") or {})
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=capture):
        await detect_provider("http://localhost:1234", api_key="not-needed")

    assert "Authorization" not in captured_headers


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_lmstudio_when_no_llamacpp_marker():
    """LM Studio entries don't carry the llamacpp marker — falls through to LMSTUDIO."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "object": "list",
        "data": [{"id": "qwen", "owned_by": "organization_owner", "object": "model"}],
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        result = await detect_provider("http://localhost:1234")

    assert result == ProviderType.LMSTUDIO


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_ollama_with_schema():
    """Ollama: schema dict goes directly into top-level `format`."""
    p = AIProvider()
    p.provider = "ollama"
    p.base_url = "http://localhost:11434"

    params = await p.get_generate_params(
        "llama3",
        "Hello",
        system_prompt="Be brief",
        options={"_response_schema": _SAMPLE_SCHEMA, "_schema_name": "test"},
    )

    assert params["json"]["format"] == _SAMPLE_SCHEMA
    # Meta-flags should not leak through into ollama options
    assert "_response_schema" not in params["json"]["options"]
    assert "_schema_name" not in params["json"]["options"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_lmstudio_with_schema():
    """LM Studio / OpenAI / vLLM: canonical nested envelope with name + strict."""
    p = AIProvider()
    p.provider = "lmstudio"
    p.base_url = "http://localhost:1234"

    params = await p.get_generate_params(
        "qwen",
        "Hello",
        system_prompt="Be brief",
        options={"_response_schema": _SAMPLE_SCHEMA, "_schema_name": "MyModel"},
    )

    rf = params["json"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "MyModel"
    assert rf["json_schema"]["strict"] is True
    # Schema is rewritten to strict-compatible form: required+additionalProperties
    sent = rf["json_schema"]["schema"]
    assert sent["required"] == ["foo"]
    assert sent["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_openai_with_schema():
    """OpenAI uses the same canonical envelope as LM Studio."""
    p = AIProvider()
    p.provider = "openai"
    p.base_url = "https://api.openai.com"
    p.api_key = "sk-fake"

    params = await p.get_generate_params(
        "gpt-4",
        "Hello",
        options={"_response_schema": _SAMPLE_SCHEMA, "_schema_name": "MyModel"},
    )

    assert params["json"]["response_format"]["type"] == "json_schema"
    sent = params["json"]["response_format"]["json_schema"]["schema"]
    # Rewritten to strict form — original `required` was set, additionalProperties added
    assert sent["required"] == ["foo"]
    assert sent["additionalProperties"] is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_llamacpp_with_schema():
    """llama.cpp: schema is sibling of `type`, no nested `json_schema` wrapper."""
    p = AIProvider()
    p.provider = "llamacpp"
    p.base_url = "http://localhost:8080"

    params = await p.get_generate_params(
        "qwen",
        "Hello",
        options={"_response_schema": _SAMPLE_SCHEMA, "_schema_name": "MyModel"},
    )

    rf = params["json"]["response_format"]
    assert rf == {"type": "json_schema", "schema": _SAMPLE_SCHEMA}
    # llama.cpp shape must NOT carry the OpenAI nested envelope keys
    assert "json_schema" not in rf


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_no_schema_omits_response_format():
    """Without a schema, response_format / format must not be present."""
    p = AIProvider()
    p.provider = "lmstudio"
    p.base_url = "http://localhost:1234"

    params = await p.get_generate_params("qwen", "Hello")

    assert "response_format" not in params["json"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_meta_flags_dont_leak_to_openai():
    """The `_`-prefixed meta keys must not appear at top level of the OpenAI payload."""
    p = AIProvider()
    p.provider = "lmstudio"
    p.base_url = "http://localhost:1234"

    params = await p.get_generate_params(
        "qwen",
        "Hello",
        options={
            "_response_schema": _SAMPLE_SCHEMA,
            "_schema_name": "X",
            "_enable_thinking": False,
            "temperature": 0.2,
        },
    )

    payload = params["json"]
    assert "_response_schema" not in payload
    assert "_schema_name" not in payload
    assert "_enable_thinking" not in payload
    # But the translated form should be there
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


# ---------------------------------------------------------------------------
# Schema round-trip: Pydantic models produce valid JSON Schema
# ---------------------------------------------------------------------------


def test_intelligence_schemas_emit_valid_json_schema():
    """Each intelligence stage's Pydantic model produces a structurally valid
    JSON schema dict — guards against accidental field-shape regressions."""
    from app.services.intelligence.schemas import (
        BatchAnalysis,
        CaseBrief,
        ClaimExtraction,
        DocumentEnrichment,
        EntityList,
        Phase1Metadata,
        RelationshipDetection,
    )

    for model_cls in [
        Phase1Metadata,
        EntityList,
        ClaimExtraction,
        DocumentEnrichment,
        BatchAnalysis,
        RelationshipDetection,
        CaseBrief,
    ]:
        schema = model_cls.model_json_schema()
        assert isinstance(schema, dict)
        assert schema.get("type") == "object" or "$ref" in schema
        assert "properties" in schema or "$defs" in schema


def test_phase1_metadata_accepts_proceeding_fields():
    """Verifies Phase1Metadata accepts the merged proceeding fields."""
    from app.services.intelligence.schemas import Phase1Metadata

    m = Phase1Metadata.model_validate(
        {
            "is_court_document": True,
            "court_level": "ag",
            "az_court": "003 F 426/25",
        }
    )
    assert m.is_court_document is True
    assert m.court_level == "ag"  # use_enum_values=True returns string
    assert m.subject_matter is None


def test_entity_list_rejects_unknown_entity_type():
    """Literal-typed entity types reject values outside the allowed set."""
    from pydantic import ValidationError

    from app.services.intelligence.schemas import EntityList

    with pytest.raises(ValidationError):
        EntityList.model_validate({"entities": [{"type": "alien", "name": "X"}]})


def test_relationship_detection_rejects_invalid_type():
    from pydantic import ValidationError

    from app.services.intelligence.schemas import RelationshipDetection

    with pytest.raises(ValidationError):
        RelationshipDetection.model_validate(
            {
                "relationships": [
                    {"to_document_id": 1, "relationship_type": "invented_type"}
                ]
            }
        )


# ---------------------------------------------------------------------------
# _make_openai_strict — schema rewriter for OpenAI strict mode compliance
# ---------------------------------------------------------------------------


def test_make_openai_strict_adds_required_and_additional_properties():
    """Top-level object gets every property in `required` + additionalProperties:false."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }
    out = _make_openai_strict(schema)
    assert out["required"] == ["a", "b"]
    assert out["additionalProperties"] is False


def test_make_openai_strict_strips_default_keys():
    """OpenAI strict mode rejects `default` — must be removed."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "default": "hi"}},
    }
    out = _make_openai_strict(schema)
    assert "default" not in out["properties"]["a"]


def test_make_openai_strict_walks_nested_objects_and_defs():
    """Nested objects in $defs are also strict-rewritten."""
    schema = {
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "integer", "default": 0},
                },
            }
        },
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Item"}}},
    }
    out = _make_openai_strict(schema)
    item = out["$defs"]["Item"]
    assert item["required"] == ["name", "qty"]
    assert item["additionalProperties"] is False
    assert "default" not in item["properties"]["qty"]


def test_make_openai_strict_does_not_mutate_input():
    """Pure function — input dict must be unchanged after the call."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "default": "x"}},
    }
    snapshot = json.loads(json.dumps(schema))
    _make_openai_strict(schema)
    assert schema == snapshot


def test_make_openai_strict_real_pydantic_schema():
    """End-to-end: one of our intelligence schemas survives the rewrite without
    structural damage and ends up with required + additionalProperties at every
    object level."""
    from app.services.intelligence.schemas import DocumentEnrichment

    raw = DocumentEnrichment.model_json_schema()
    out = _make_openai_strict(raw)

    # Top-level object: every property required, additionalProperties false
    assert out["additionalProperties"] is False
    assert sorted(out["required"]) == sorted(out["properties"].keys())

    # Each $defs entry that is an object should be similarly tightened
    for name, definition in out.get("$defs", {}).items():
        if definition.get("type") == "object" and "properties" in definition:
            assert definition["additionalProperties"] is False, (
                f"$defs.{name} missing additionalProperties:false"
            )
            assert sorted(definition["required"]) == sorted(
                definition["properties"].keys()
            ), f"$defs.{name} required mismatch"
