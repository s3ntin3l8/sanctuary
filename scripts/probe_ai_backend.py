import asyncio
import logging

import httpx

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("probe")


async def probe(url):
    print(f"\n--- Probing AI Backend at {url} ---")
    async with httpx.AsyncClient(timeout=5.0) as client:
        # 1. Probe v1/models (LM Studio / OpenAI)
        print("\n1. Probing OpenAI-compatible endpoint (/v1/models)...")
        try:
            resp = await client.get(f"{url}/v1/models")
            print(f"   Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"   Response Object: {data.get('object')}")
                if "data" in data:
                    models = [m.get("id") for m in data["data"]]
                    print(f"   Found {len(models)} models: {', '.join(models[:3])}...")
                    if data.get("object") == "list" or "data" in data:
                        print("   [✓] Matches OpenAI-compatible signature (LM Studio)")
                else:
                    print("   [!] Missing 'data' key in response")
        except Exception as e:
            print(f"   Error: {e}")

        # 2. Probe tags (Ollama)
        print("\n2. Probing Ollama-specific endpoint (/api/tags)...")
        try:
            resp = await client.get(f"{url}/api/tags")
            print(f"   Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                if "models" in data:
                    models = [m.get("name") for m in data["models"]]
                    print(f"   Found {len(models)} models: {', '.join(models[:3])}...")
                    print("   [✓] Matches Ollama signature")
                else:
                    print("   [!] Missing 'models' key in response")
                    print(f"   Raw Body: {resp.text[:100]}")
        except Exception as e:
            print(f"   Error: {e}")


if __name__ == "__main__":
    import sys

    # Use user's specific LM Studio URL from logs
    target_url = sys.argv[1] if len(sys.argv) > 1 else "http://192.168.2.106:1234"
    asyncio.run(probe(target_url))
