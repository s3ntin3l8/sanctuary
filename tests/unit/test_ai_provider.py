"""Unit tests for ai_provider.py — provider auto-detection, param shaping, stream parsing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_provider import AIProvider, ProviderType, detect_provider

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
    assert result == {"response": "Hello", "done": False}


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
    assert result == {"response": "Hi", "thinking": "", "done": False}


def test_parse_stream_line_openai_done():
    p = AIProvider()
    result = p.parse_stream_line("data: [DONE]", ProviderType.LMSTUDIO)
    assert result == {"done": True}


def test_parse_stream_line_empty():
    p = AIProvider()
    assert p.parse_stream_line("", ProviderType.OLLAMA) is None
