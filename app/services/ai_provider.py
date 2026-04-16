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
                    "max_tokens": -1,  # LM Studio convention for unlimited
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
            if line.startswith("data: "):
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    return {"done": True}
                try:
                    chunk = json.loads(data_str)
                    content = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    return {"response": content, "done": False}
                except json.JSONDecodeError:
                    return None
        return None


# Global instance
ai_provider = AIProvider()
