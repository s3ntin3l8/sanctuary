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
        # 1. Check for OpenAI-compatible (LM Studio / vLLM / etc)
        try:
            resp = await client.get(f"{base_url}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                # Standard OpenAI format for model list
                if isinstance(data, dict) and (
                    data.get("object") == "list" or "data" in data
                ):
                    return ProviderType.LMSTUDIO
        except Exception as e:
            logger.debug(f"OpenAI probe failed at {base_url}: {e}")

        # 2. Check for Ollama
        try:
            resp = await client.get(f"{base_url}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "models" in data:
                    return ProviderType.OLLAMA
        except Exception as e:
            logger.debug(f"Ollama probe failed at {base_url}: {e}")

    logger.warning(
        f"AI provider auto-detection failed for {base_url}, defaulting to ollama"
    )
    return ProviderType.OLLAMA  # Default fallback


class AIProvider:
    def __init__(self):
        self.base_url = AI_BASE_URL
        self.provider = AI_PROVIDER
        self.api_key = AI_API_KEY
        self._detected_type: ProviderType | None = None
        self._user_context: str = ""

    def reload_from_db(self, db) -> None:
        """Refresh connection config and user context from UserSettings."""
        from app.services.ai_config import get_effective_config

        cfg = get_effective_config(db)
        changed = (
            cfg.base_url != self.base_url
            or cfg.provider != self.provider
            or cfg.api_key != self.api_key
        )
        self.base_url = cfg.base_url
        self.provider = cfg.provider
        self.api_key = cfg.api_key
        self._user_context = cfg.user_context
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
            # LM Studio / OpenAI compatible
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

    async def probe_health(self) -> dict:
        """Check if the configured AI provider endpoint is reachable."""
        ptype = await self.get_type()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                if ptype == ProviderType.OLLAMA:
                    resp = await client.get(f"{self.base_url}/api/tags")
                    if resp.status_code == 200:
                        count = len(resp.json().get("models", []))
                        return {
                            "ok": True,
                            "provider": str(ptype),
                            "detail": f"{count} models available",
                        }
                else:
                    resp = await client.get(f"{self.base_url}/v1/models")
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
            # OpenAI format: "data: {...}"
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


# Global instance
ai_provider = AIProvider()


def call_llm(
    prompt: str,
    model: str,
    system_prompt: str | None = None,
    temperature: float = 0.0,
    response_format: dict | None = None,
) -> str:
    """Synchronous non-streaming LLM call."""
    from app.core.async_utils import run_async

    options = {"temperature": temperature}
    # Note: response_format is handled by provider-specific logic if supported

    params = run_async(
        ai_provider.get_generate_params(
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            stream=False,
            options=options,
        )
    )

    with httpx.Client(timeout=httpx.Timeout(120.0)) as client:
        resp = client.post(
            params["url"], json=params["json"], headers=params["headers"]
        )
        resp.raise_for_status()
        data = resp.json()

        # Handle Ollama vs OpenAI format
        if "response" in data:  # Ollama
            return data["response"]
        elif "choices" in data:  # OpenAI
            return data["choices"][0]["message"]["content"]
        return ""
