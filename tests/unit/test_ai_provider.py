"""Unit tests for ai_provider.py — provider auto-detection, param shaping, stream parsing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.ai_provider import (
    AIProvider,
    ProviderType,
    _make_anthropic_strict,
    _make_openai_strict,
    anthropic_headers,
    detect_provider,
    get_embedding_params_for,
    models_in,
    models_request,
    normalize_base_url,
    openai_compat_headers,
    sanitize_provider_error,
)


@pytest.mark.unit
def test_sanitize_runtime_error_is_clear_unreachable_message():
    """An unreachable endpoint (detect_provider RuntimeError) gets a readable
    message, not the opaque 'Unexpected error: RuntimeError' fallback."""
    msg = sanitize_provider_error(RuntimeError("AI provider unreachable: …"))
    assert "not reachable" in msg.lower()
    assert "RuntimeError" not in msg


@pytest.mark.unit
def test_sanitize_connect_error_names_the_target():
    msg = sanitize_provider_error(httpx.ConnectError("refused"), "http://10.0.0.9:7000")
    assert "10.0.0.9:7000" in msg or "Cannot reach" in msg


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


# ---------------------------------------------------------------------------
# Hosted endpoints: base-URL normalisation and payload hygiene
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.anthropic.com/v1", "https://api.anthropic.com"),
        ("https://api.openai.com/v1/", "https://api.openai.com"),
        ("  http://localhost:11434/ ", "http://localhost:11434"),
        ("http://localhost:1234", "http://localhost:1234"),
        ("", ""),
        ("   ", ""),
        ("https://example.com/v1beta", "https://example.com/v1beta"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_hosted_endpoint_omits_local_only_fields():
    p = AIProvider()
    p.provider = "lmstudio"
    p.base_url = "https://api.openai.com"

    params = await p.get_generate_params(
        "gpt-4o",
        "Hello",
        options={"top_p": 0.9, "top_k": 20, "min_p": 0.0, "_enable_thinking": False},
    )

    body = params["json"]
    assert "max_tokens" not in body
    assert "top_k" not in body
    assert "min_p" not in body
    assert "chat_template_kwargs" not in body
    assert body["top_p"] == 0.9


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_local_endpoint_keeps_sampler_fields():
    p = AIProvider()
    p.provider = "lmstudio"
    p.base_url = "http://localhost:1234"

    params = await p.get_generate_params(
        "qwen",
        "Hello",
        options={
            "top_k": 20,
            "min_p": 0.0,
            "max_tokens": 500,
            "_enable_thinking": False,
        },
    )

    body = params["json"]
    assert body["top_k"] == 20
    assert body["min_p"] == 0.0
    assert body["max_tokens"] == 500
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_ollama_wins_over_its_openai_compat_surface():
    """Ollama also answers /v1/models in OpenAI shape; it must still be Ollama."""
    tags = MagicMock(status_code=200)
    tags.json.return_value = {"models": [{"name": "llama3"}]}
    models = MagicMock(status_code=200)
    models.json.return_value = {
        "object": "list",
        "data": [{"id": "llama3", "owned_by": "library"}],
    }

    async def fake_get(url, **_):
        return tags if url.endswith("/api/tags") else models

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        result = await detect_provider("http://docker-01:11434")

    assert result == ProviderType.OLLAMA


@pytest.mark.asyncio
@pytest.mark.unit
async def test_generate_params_hosted_endpoint_without_options_has_no_max_tokens():
    p = AIProvider()
    p.provider = "lmstudio"
    p.base_url = "https://api.openai.com"

    params = await p.get_generate_params("gpt-4o", "Hello")

    assert set(params["json"]) == {"model", "messages", "stream", "temperature"} | {
        "stream_options"
    }
    assert "max_tokens" not in params["json"]


@pytest.mark.unit
def test_openai_compat_headers():
    assert openai_compat_headers("sk-1") == {
        "anthropic-version": "2023-06-01",
        "Authorization": "Bearer sk-1",
    }
    for key in ("not-needed", "", None):
        assert openai_compat_headers(key) == {"anthropic-version": "2023-06-01"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_anthropic_requires_version_header():
    """Anthropic's /v1/models answers 400 without `anthropic-version`; with it,
    a Bearer key works and the body is `{"data": [...]}` (no `object: list`)."""
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"data": [{"id": "claude-sonnet-5-5"}], "has_more": False}
    bad = MagicMock(status_code=400)
    not_found = MagicMock(status_code=404)

    async def fake_get(url, headers=None, **_):
        if url.endswith("/api/tags"):
            return not_found
        return ok if (headers or {}).get("anthropic-version") else bad

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        result = await detect_provider(
            "https://api.anthropic.com",
            api_key="sk-ant-x",  # pragma: allowlist secret
        )

    assert result == ProviderType.LMSTUDIO


# ---------------------------------------------------------------------------
# Native Anthropic Messages API
# ---------------------------------------------------------------------------


def _anthropic_provider(**kw) -> AIProvider:
    p = AIProvider()
    p.provider = "anthropic"
    p.base_url = "https://api.anthropic.com"
    p.api_key = "sk-ant-x"  # pragma: allowlist secret
    p._user_context = kw.get("user_context", "")
    return p


@pytest.mark.unit
def test_anthropic_headers_use_x_api_key_not_bearer():
    h = anthropic_headers("sk-ant-x")  # pragma: allowlist secret
    assert h == {
        "anthropic-version": "2023-06-01",
        "x-api-key": "sk-ant-x",  # pragma: allowlist secret
    }
    assert "x-api-key" not in anthropic_headers("not-needed")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_anthropic_by_model_entry_type():
    ok = MagicMock(status_code=200)
    ok.json.return_value = {
        "data": [{"type": "model", "id": "claude-haiku-5-5"}],
        "has_more": False,
    }
    nf = MagicMock(status_code=404)

    async def fake_get(url, **_):
        return nf if url.endswith("/api/tags") else ok

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        result = await detect_provider("https://api.anthropic.com", api_key="k")

    assert result == ProviderType.ANTHROPIC


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detect_provider_anthropic_by_error_envelope_on_bad_key():
    bad = MagicMock(status_code=401)
    bad.json.return_value = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    nf = MagicMock(status_code=404)

    async def fake_get(url, **_):
        return nf if url.endswith("/api/tags") else bad

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        result = await detect_provider(
            "https://api.anthropic.com",
            api_key="wrong",  # pragma: allowlist secret
        )

    assert result == ProviderType.ANTHROPIC


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_generate_params_shape():
    p = _anthropic_provider(user_context="Case context")

    params = await p.get_generate_params(
        "claude-sonnet-5-5",
        "Hello",
        system_prompt="Be brief",
        options={
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
            "num_ctx": 32768,
            "stop": ["\nWait, actually", "  ", "\n"],
            "_enable_thinking": False,
        },
    )

    body = params["json"]
    assert params["url"] == "https://api.anthropic.com/v1/messages"
    assert params["headers"]["x-api-key"] == "sk-ant-x"  # pragma: allowlist secret
    assert "Authorization" not in params["headers"]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "Case context\n\nBe brief"
    assert body["messages"] == [{"role": "user", "content": "Hello"}]
    assert body["max_tokens"] == 8192  # required by the API; stage set none
    assert body["stop_sequences"] == ["\nWait, actually"]  # blanks are a 400
    assert body["stream"] is True
    for local_only in (
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "options",
        "think",
        "chat_template_kwargs",
    ):
        assert local_only not in body


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_generate_params_max_tokens_and_schema():
    p = _anthropic_provider()
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "default": "x"}},
    }

    params = await p.get_generate_params(
        "m",
        "Hi",
        options={"max_tokens": 1234, "_response_schema": schema},
    )

    body = params["json"]
    assert body["max_tokens"] == 1234
    assert "system" not in body
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["required"] == ["a"]


@pytest.mark.unit
def test_make_anthropic_strict_drops_free_form_object_fields():
    from app.services.intelligence.schemas import DocumentEnrichment

    out = _make_anthropic_strict(DocumentEnrichment.model_json_schema())

    def walk(node):
        if isinstance(node, dict):
            assert node.get("additionalProperties") is not True
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out)
    assert "allocation" not in out["$defs"]["_CostDelta"]["properties"]
    assert "allocation" not in out["$defs"]["_CostDelta"]["required"]


_ANTHROPIC_STREAM = [
    "event: message_start",
    'data: {"type":"message_start","message":{"usage":{"input_tokens":12,"output_tokens":4}}}',
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    "event: ping",
    'data: {"type": "ping"}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"hmm"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}',
    'data: {"type":"message_stop"}',
]


@pytest.mark.unit
def test_anthropic_stream_parsing_end_to_end():
    p = _anthropic_provider()
    text = thinking = ""
    usage: dict = {}
    done = False
    for line in _ANTHROPIC_STREAM:
        chunk = p.parse_stream_line(line, ProviderType.ANTHROPIC)
        if not chunk:
            continue
        usage = {**usage, **chunk.get("usage", {})}
        text += chunk.get("response", "")
        thinking += chunk.get("thinking", "")
        done = done or chunk.get("done", False)

    assert (text, thinking, done) == ("OK", "hmm", True)
    assert usage == {"prompt_tokens": 12, "completion_tokens": 9}


@pytest.mark.unit
def test_anthropic_stream_error_event_raises():
    p = _anthropic_provider()
    line = (
        'data: {"type":"error","error":{"type":"overloaded_error",'
        '"message":"Overloaded"}}'
    )
    with pytest.raises(RuntimeError, match="overloaded_error"):
        p.parse_stream_line(line, ProviderType.ANTHROPIC)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_anthropic_has_no_embeddings():
    p = _anthropic_provider()
    with pytest.raises(RuntimeError, match="no embeddings API"):
        await p.get_embedding_params("m", "text")
    with pytest.raises(RuntimeError, match="no embeddings API"):
        await get_embedding_params_for(
            {"base_url": "https://api.anthropic.com", "provider": "anthropic"},
            "m",
            "text",
        )


@pytest.mark.unit
def test_models_request_and_models_in_per_provider():
    url, headers = models_request(
        ProviderType.ANTHROPIC, "https://api.anthropic.com", "k"
    )
    assert url == "https://api.anthropic.com/v1/models?limit=1000"
    assert headers["x-api-key"] == "k"
    assert models_request(ProviderType.OLLAMA, "http://h:1", "k") == (
        "http://h:1/api/tags",
        {},
    )
    assert models_in(ProviderType.ANTHROPIC, {"data": [{"id": "claude-x"}]}) == [
        "claude-x"
    ]
    assert models_in(ProviderType.OLLAMA, {"models": [{"name": "llama3"}]}) == [
        "llama3"
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_probe_health_anthropic_counts_models_with_native_headers():
    seen = {}
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"data": [{"id": "a"}, {"id": "b"}]}

    async def fake_get(url, headers=None, **_):
        seen.update(url=url, headers=headers)
        return ok

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        result = await AIProvider().probe_health(
            config={
                "base_url": "https://api.anthropic.com/v1",
                "provider": "anthropic",
                "api_key": "sk-ant-x",  # pragma: allowlist secret
            }
        )

    assert result == {
        "ok": True,
        "provider": "anthropic",
        "detail": "2 models available",
    }
    assert seen["url"] == "https://api.anthropic.com/v1/models?limit=1000"
    assert seen["headers"]["x-api-key"] == "sk-ant-x"  # pragma: allowlist secret
