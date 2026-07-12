import copy
import json
import logging
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx

from app.config import AI_API_KEY, AI_BASE_URL, AI_PROVIDER

if TYPE_CHECKING:
    from app.services.ai_config import ChatConfig, EmbedConfig, OcrConfig

logger = logging.getLogger(__name__)


def sanitize_provider_error(exc: Exception, provider_url: str = "") -> str:
    """Translate provider-side exceptions into actionable user-facing messages.

    Examples:
        ConnectError on :11434 → "Ollama is not running on localhost:11434…"
        HTTPStatusError(401)  → "Authentication failed — check the API key."
        TimeoutException      → "Provider is taking too long to respond…"

    Falls through to `Unexpected error: <ExcClass>` so unknown failures still
    surface a non-leaky message; the raw traceback stays in the server log
    (callers should log before calling this).
    """
    if isinstance(exc, ConnectionRefusedError | httpx.ConnectError):
        if "11434" in provider_url:
            return (
                "Ollama is not running on localhost:11434. "
                "Start it with `ollama serve`."
            )
        if "1234" in provider_url:
            return (
                "LM Studio's local server isn't reachable. "
                "Start the server in LM Studio's Developer tab."
            )
        target = provider_url or "the configured URL"
        return f"Cannot reach provider at {target}."
    if isinstance(exc, httpx.TimeoutException):
        return (
            "Provider is taking too long to respond. "
            "The model may be loading or overloaded."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        s = exc.response.status_code
        if s == 401:
            return "Authentication failed — check the API key."
        if s == 403:
            return "Provider denied access — check key permissions."
        if s == 404:
            return "Model not found on the provider."
        if s == 429:
            return "Rate limited by the provider — wait and retry."
        if s >= 500:
            return f"Provider returned HTTP {s}. May be transient."
        return f"Provider returned HTTP {s}."
    if isinstance(exc, json.JSONDecodeError):
        return "Provider returned non-JSON response — the URL is likely wrong."
    if isinstance(exc, RuntimeError):
        # detect_provider / get_embedding_params_for raise RuntimeError when no
        # endpoint responded on /v1/models or /api/tags — i.e. unreachable.
        return (
            "Endpoint not reachable — check the URL and that the AI server is running."
        )
    return f"Unexpected error: {type(exc).__name__}"


def _make_openai_strict(schema: dict) -> dict:
    """Rewrite a Pydantic-emitted JSON schema to satisfy OpenAI strict mode.

    OpenAI structured-output strict mode requires:
    - Every property of every object listed in `required`.
    - `additionalProperties: false` on every object.

    Pydantic v2 emits optional fields with `default` and an `anyOf [<type>, null]`
    union — which already encodes nullability the way OpenAI wants — but leaves
    them out of `required` and skips `additionalProperties`. We add both,
    walking through `$defs` and nested `properties` as well. The `default` key
    is dropped because OpenAI rejects it under strict mode.

    LMStudio / vLLM / llama.cpp accept the rewrite too — it's a strict superset
    of what they require. Caller passes the result to the OpenAI-compat branch
    so all four backends get the same grammar-enforced shape.
    """
    rewritten = copy.deepcopy(schema)

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"].keys())
                node["additionalProperties"] = False
            node.pop("default", None)
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(rewritten)
    return rewritten


class ProviderType(StrEnum):
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"
    OPENAI = "openai"
    LLAMACPP = "llamacpp"


async def detect_provider(base_url: str, api_key: str | None = None) -> ProviderType:
    """Detect the AI provider by probing endpoints and checking content.

    LM Studio and llama.cpp's `llama-server` both expose `/v1/models` in the
    OpenAI list shape; llama.cpp distinguishes itself with `owned_by="llamacpp"`
    on each entry (server-models.cpp:1184). The split matters because
    llama.cpp's `response_format` shape is sibling-`schema`, not nested.

    `api_key` lets us probe auth-protected OpenAI-compatible servers (LiteLLM,
    OpenAI, hosted gateways). When unset and we hit a 401 on `/v1/models`, we
    still classify as LMSTUDIO — the server exists and speaks the OpenAI
    surface; auth will be supplied at request time.
    """
    headers = (
        {"Authorization": f"Bearer {api_key}"}
        if api_key and api_key != "not-needed"
        else {}
    )
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{base_url}/v1/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and (
                    data.get("object") == "list" or "data" in data
                ):
                    entries = data.get("data") or []
                    if any(
                        isinstance(e, dict) and e.get("owned_by") == "llamacpp"
                        for e in entries
                    ):
                        return ProviderType.LLAMACPP
                    return ProviderType.LMSTUDIO
            # 401/403: server exists, just needs auth — still OpenAI-compatible.
            if resp.status_code in (401, 403):
                return ProviderType.LMSTUDIO
        except Exception as e:
            logger.debug(f"OpenAI probe failed at {base_url}: {e}")

        try:
            resp = await client.get(f"{base_url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "models" in data:
                    return ProviderType.OLLAMA
        except Exception as e:
            logger.debug(f"Ollama probe failed at {base_url}: {e}")

    raise RuntimeError(
        f"AI provider unreachable: no endpoint responded at {base_url} "
        "(tried /v1/models for LM Studio / llama.cpp and /api/tags for Ollama "
        "— check that your AI server is running)"
    )


class AIProvider:
    def __init__(self, role: str = "chat"):
        self._role = role
        self.base_url = AI_BASE_URL
        self.provider = AI_PROVIDER
        self.api_key = AI_API_KEY
        self._detected_type: ProviderType | None = None
        self._user_context: str = ""

    def reload_from_db(self, db) -> None:
        """Refresh connection config from the active instance for this role."""
        cfg: ChatConfig | EmbedConfig | OcrConfig
        if self._role == "chat":
            from app.services.ai_config import get_chat_config

            cfg = get_chat_config(db)
            self._user_context = cfg.user_context
        elif self._role == "embed":
            from app.services.ai_config import get_embed_config

            cfg = get_embed_config(db)
        elif self._role == "ocr":
            from app.services.ai_config import get_ocr_config

            cfg = get_ocr_config(db)
        else:
            raise ValueError(f"Unknown role {self._role!r}")

        changed = (
            cfg.base_url != self.base_url
            or cfg.provider != self.provider
            or cfg.api_key != self.api_key
        )
        self.base_url = cfg.base_url
        self.provider = cfg.provider
        self.api_key = cfg.api_key
        if changed:
            self._detected_type = None

    async def get_type(self) -> ProviderType:
        if self.provider != "auto":
            return ProviderType(self.provider)
        if not self._detected_type:
            self._detected_type = await detect_provider(self.base_url, self.api_key)
            logger.info(f"Auto-detected AI provider: {self._detected_type}")
        return self._detected_type

    async def get_generate_params(
        self,
        model: str,
        prompt: str,
        system_prompt: str | None = None,
        stream: bool = True,
        options: dict | None = None,
    ) -> dict:
        """Get provider-specific request parameters for text generation."""
        # User-context preamble (e.g. "I'm Björn fighting for custody…") is
        # prepended to the system prompt by default, which is what case-level
        # synthesis (case brief) needs. Per-doc stages opt out via
        # `_include_user_context=False` because the preamble leaks the case
        # narrative into individual document summaries — e.g. an ICBC bank
        # certificate ended up with `required_action="File as supporting
        # evidence in custody proceedings"` because the preamble framed
        # everything as a custody case. The per-doc prompts get the party
        # identities they need from the explicit "Known Party Identity" block
        # in the user prompt, not from this preamble.
        include_user_context = (
            options.get("_include_user_context", True) if options else True
        )
        ctx = self._user_context
        if ctx and include_user_context:
            system_prompt = f"{ctx}\n\n{system_prompt}" if system_prompt else ctx

        ptype = await self.get_type()

        # Meta flags set by call_json_ai. Both are translated below into
        # provider-specific fields and stripped from forwarded options so they
        # don't leak as unknown keys to the server.
        #   _enable_thinking         — Qwen thinking-disable
        #   _response_schema         — JSON schema for grammar-constrained output
        #   _schema_name             — human-readable name for the OpenAI strict-mode envelope
        #   _include_user_context    — handled above; stripped before forwarding
        enable_thinking = options.get("_enable_thinking", True) if options else True
        response_schema = options.get("_response_schema") if options else None
        schema_name = options.get("_schema_name", "response") if options else "response"

        if ptype == ProviderType.OLLAMA:
            full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
            ollama_options = {
                k: v for k, v in (options or {}).items() if not k.startswith("_")
            }
            body: dict = {
                "model": model,
                "prompt": full_prompt,
                "stream": stream,
                "options": ollama_options,
            }
            if not enable_thinking:
                body["think"] = False
            if response_schema is not None:
                # Ollama: schema dict goes directly into top-level `format`.
                body["format"] = response_schema
            return {
                "url": f"{self.base_url}/api/generate",
                "json": body,
                "headers": {},
            }
        else:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            payload: dict = {
                "model": model,
                "messages": messages,
                "stream": stream,
                "temperature": options.get("temperature", 0.1) if options else 0.1,
                "max_tokens": options.get("max_tokens", -1) if options else -1,
            }
            if stream:
                # Many OpenAI-compatible gateways (LiteLLM, vLLM) require this
                # to send the final usage chunk at the end of the stream.
                payload["stream_options"] = {"include_usage": True}

            if options:
                # Forward Qwen sampling params that LMStudio / OpenAI-compat accept.
                for key in (
                    "top_p",
                    "top_k",
                    "min_p",
                    "presence_penalty",
                    "frequency_penalty",
                ):
                    if key in options:
                        payload[key] = options[key]
                if options.get("stop"):
                    payload["stop"] = options["stop"]
            if not enable_thinking:
                # Qwen3.5 vLLM convention — top-level chat_template_kwargs.
                # Servers that don't support it ignore unknown fields.
                payload["chat_template_kwargs"] = {"enable_thinking": False}
            if response_schema is not None:
                if ptype == ProviderType.LLAMACPP:
                    # llama.cpp: schema is a sibling of `type`, no `name`,
                    # no `strict` (server-models.cpp + chat.cpp parsing).
                    payload["response_format"] = {
                        "type": "json_schema",
                        "schema": response_schema,
                    }
                else:
                    # OpenAI / LM Studio / vLLM: canonical nested envelope with
                    # strict-mode-compatible rewrite. Pydantic v2's default
                    # schema leaves optionals out of `required` and skips
                    # `additionalProperties: false`, both of which OpenAI strict
                    # mode requires; _make_openai_strict adds them recursively.
                    # LM Studio / vLLM accept the rewrite too — it's a superset
                    # of what they require — so all four OpenAI-compat backends
                    # get the same grammar-enforced shape.
                    payload["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "schema": _make_openai_strict(response_schema),
                            "strict": True,
                        },
                    }

            return {
                "url": f"{self.base_url}/v1/chat/completions",
                "json": payload,
                "headers": {"Authorization": f"Bearer {self.api_key}"}
                if self.api_key != "not-needed"
                else {},
            }

    async def get_embedding_params(self, model: str, prompt: str) -> dict:
        """Get provider-specific request parameters for embeddings."""
        ptype = await self.get_type()

        if ptype == ProviderType.OLLAMA:
            return {
                "url": f"{self.base_url}/api/embeddings",
                "json": {"model": model, "prompt": prompt},
                "headers": {},
            }
        else:
            return {
                "url": f"{self.base_url}/v1/embeddings",
                "json": {"model": model, "input": prompt},
                "headers": {"Authorization": f"Bearer {self.api_key}"}
                if self.api_key != "not-needed"
                else {},
            }

    async def probe_health(self, config: dict | None = None) -> dict:
        """Check if an AI provider endpoint is reachable.

        Pass config dict with base_url/provider/api_key to probe a specific instance
        without mutating this provider's state.
        """
        if config:
            base_url = config.get("base_url", self.base_url).strip().rstrip("/")
            provider = config.get("provider", self.provider)
            api_key = config.get("api_key", self.api_key)
            try:
                ptype = (
                    ProviderType(provider)
                    if provider != "auto"
                    else await detect_provider(base_url, api_key)
                )
            except Exception as e:
                logger.debug(f"probe_health: detect_provider failed at {base_url}: {e}")
                return {
                    "ok": False,
                    "provider": "unknown",
                    "detail": sanitize_provider_error(e, base_url),
                }
        else:
            base_url = self.base_url
            api_key = self.api_key
            ptype = await self.get_type()

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                if ptype == ProviderType.OLLAMA:
                    resp = await client.get(f"{base_url}/api/tags")
                    if resp.status_code == 200:
                        count = len(resp.json().get("models", []))
                        return {
                            "ok": True,
                            "provider": str(ptype),
                            "detail": f"{count} models available",
                        }
                else:
                    headers = (
                        {"Authorization": f"Bearer {api_key}"}
                        if api_key != "not-needed"
                        else {}
                    )
                    resp = await client.get(f"{base_url}/v1/models", headers=headers)
                    if resp.status_code == 200:
                        count = len(resp.json().get("data", []))
                        return {
                            "ok": True,
                            "provider": str(ptype),
                            "detail": f"{count} models available",
                        }
            return {
                "ok": False,
                "provider": str(ptype),
                "detail": f"HTTP {resp.status_code}",
            }
        except Exception as e:
            logger.debug(f"probe_health: {base_url} {ptype} failed: {e}")
            return {
                "ok": False,
                "provider": str(ptype),
                "detail": sanitize_provider_error(e, base_url),
            }

    def parse_stream_line(self, line: str, ptype: ProviderType) -> dict | None:
        """Parse a single line from a streaming response."""
        if not line:
            return None

        if ptype == ProviderType.OLLAMA:
            try:
                data = json.loads(line)
                result = {
                    "response": data.get("response", ""),
                    "thinking": data.get("thinking", ""),
                    "done": data.get("done", False),
                }
                # Ollama token usage
                if "prompt_eval_count" in data or "eval_count" in data:
                    result["usage"] = {
                        "prompt_tokens": data.get("prompt_eval_count", 0),
                        "completion_tokens": data.get("eval_count", 0),
                        "total_tokens": data.get("prompt_eval_count", 0)
                        + data.get("eval_count", 0),
                    }
                return result
            except json.JSONDecodeError:
                return None
        else:
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    return {"done": True}
                try:
                    chunk = json.loads(data_str)

                    # OpenAI-compatible token usage (usually in final chunk)
                    usage = chunk.get("usage")

                    choices = chunk.get("choices", [])
                    if not choices:
                        return {
                            "response": "",
                            "thinking": "",
                            "done": False,
                            "usage": usage,
                        }

                    delta = choices[0].get("delta", {})
                    content = delta.get("content") or ""
                    thinking = (
                        delta.get("reasoning_content") or delta.get("reasoning") or ""
                    )

                    return {
                        "response": content,
                        "thinking": thinking,
                        "done": False,
                        "usage": usage,
                    }
                except json.JSONDecodeError:
                    return None
        return None


# Role-specific singletons: chat_provider for generation, embed_provider for
# embeddings, ocr_provider for image-to-markdown extraction (Chandra-class).
chat_provider = AIProvider(role="chat")
embed_provider = AIProvider(role="embed")
ocr_provider = AIProvider(role="ocr")


async def get_embedding_params_for(config: dict, model: str, prompt: str) -> dict:
    """Build embedding request params from a config dict without touching singletons.

    Raises RuntimeError if the provider endpoint is unreachable.
    """
    base_url = config.get("base_url", "").strip().rstrip("/")
    api_key = config.get("api_key", "not-needed")
    provider = config.get("provider", "auto")

    ptype = (
        await detect_provider(base_url, api_key)
        if provider == "auto"
        else ProviderType(provider)
    )

    if ptype == ProviderType.OLLAMA:
        return {
            "url": f"{base_url}/api/embeddings",
            "json": {"model": model, "prompt": prompt},
            "headers": {},
        }
    else:
        return {
            "url": f"{base_url}/v1/embeddings",
            "json": {"model": model, "input": prompt},
            "headers": {"Authorization": f"Bearer {api_key}"}
            if api_key != "not-needed"
            else {},
        }
