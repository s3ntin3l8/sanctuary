import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Add app to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from app.config import AI_EMBED_MODEL, AI_SUMMARY_MODEL
from app.services.ai_provider import ai_provider


async def test_backend():
    print("--- AI Backend Test ---")
    ptype = await ai_provider.get_type()
    print(f"Provider: {ptype}")
    print(f"Base URL: {ai_provider.base_url}")
    print("-" * 30)

    # 1. Test Generation
    print("\n1. Testing Text Generation...")
    params = await ai_provider.get_generate_params(
        model=AI_SUMMARY_MODEL,
        prompt="Say 'Backend test successful' in one sentence.",
        stream=True,
    )

    full_response = ""
    try:
        async with (
            httpx.AsyncClient(timeout=30.0) as client,
            client.stream(
                "POST", params["url"], json=params["json"], headers=params["headers"]
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                chunk = ai_provider.parse_stream_line(line, ptype)
                if chunk and "response" in chunk:
                    full_response += chunk["response"]
                    print(chunk["response"], end="", flush=True)
                if chunk and chunk.get("done"):
                    break
        print("\n[✓] Generation SUCCESS")
    except Exception as e:
        print(f"\n[✗] Generation FAILED: {e}")

    # 2. Test Embedding
    print("\n2. Testing Embeddings...")
    params = await ai_provider.get_embedding_params(
        model=AI_EMBED_MODEL, prompt="Test embedding"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                params["url"], json=params["json"], headers=params["headers"]
            )
            resp.raise_for_status()
            data = resp.json()

            embedding = None
            if "embedding" in data:
                embedding = data["embedding"]
            elif "data" in data:
                embedding = data["data"][0].get("embedding")

            if embedding:
                print(f"[✓] Embedding SUCCESS (Size: {len(embedding)})")
            else:
                print("[✗] Embedding FAILED: No embedding found in response")
    except Exception as e:
        print(f"[✗] Embedding FAILED: {e}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(test_backend())
