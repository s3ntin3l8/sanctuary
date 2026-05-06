import json
import logging
from enum import StrEnum

import httpx

from app.config import AI_API_KEY, AI_BASE_URL, AI_PROVIDER

logger = logging.getLogger(__name__)


class ProviderType(StrEnum):
    OLLAMA = "ollama"
    LMSTUDIO = "lmstudio"
    OPENAI = "openai"


async def detect_provider(base_url: str) -> ProviderType:
    """Detect the AI provider by probing endpoints and checking content."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{base_url}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and (
                    data.get("object") == "list" or "data" in data
                ):
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
        "(tried /v1/models for LM Studio and /api/tags for Ollama — "
        "check that your AI server is running)"
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
        if self._role == "chat":
            from app.services.ai_config import get_chat_config

            cfg = get_chat_config(db)
            changed = (
                cfg.base_url != self.base_url
                or cfg.provider != self.provider
                or cfg.api_key != self.api_key
            )
            self.base_url = cfg.base_url
            self.provider = cfg.provider
            self.api_key = cfg.api_key
            self._user_context = cfg.user_context
        else:
            from app.services.ai_config import get_embed_config

            cfg = get_embed_config(db)
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
            self._detected_type = await detect_provider(self.base_url)
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
        ctx = self._user_context
        if ctx:
            system_prompt = f"{ctx}\n\n{system_prompt}" if system_prompt else ctx

        ptype = await self.get_type()

        if ptype == ProviderType.OLLAMA:
            full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
            return {
                "url": f"{self.base_url}/api/generate",
                "json": {
                    "model": model,
                    "prompt": full_prompt,
                    "stream": stream,
                    "options": options or {},
                },
                "headers": {},
            }
        else:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            return {
                "url": f"{self.base_url}/v1/chat/completions",
                "json": {
                    "model": model,
                    "messages": messages,
                    "stream": stream,
                    "temperature": options.get("temperature", 0.1) if options else 0.1,
                    "max_tokens": options.get("max_tokens", -1) if options else -1,
                },
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
                    else await detect_provider(base_url)
                )
            except (RuntimeError, Exception) as e:
                return {"ok": False, "provider": "unknown", "detail": str(e)}
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
            return {"ok": False, "provider": str(ptype), "detail": str(e)}

    def parse_stream_line(self, line: str, ptype: ProviderType) -> dict | None:
        """Parse a single line from a streaming response."""
        if not line:
            return None

        if ptype == ProviderType.OLLAMA:
            try:
                return json.loads(line)
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
                    choices = chunk.get("choices", [])
                    if not choices:
                        return {"response": "", "done": False}

                    delta = choices[0].get("delta", {})
                    content = delta.get("content") or ""
                    thinking = (
                        delta.get("reasoning_content") or delta.get("reasoning") or ""
                    )

                    return {
                        "response": content,
                        "thinking": thinking,
                        "done": False,
                    }
                except json.JSONDecodeError:
                    return None
        return None


# Role-specific singletons: chat_provider for generation, embed_provider for embeddings
chat_provider = AIProvider(role="chat")
embed_provider = AIProvider(role="embed")
