import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# 1. Load environment variables
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

AI_BASE_URL = os.getenv("AI_BASE_URL", "http://localhost:11434").rstrip("/")
MODEL = os.getenv("AI_SUMMARY_MODEL", "qwen3.5-9b-16k:latest")

print("--- Direct AI Backend Test ---")
print(f"Target URL: {AI_BASE_URL}")
print(f"Target Model: {MODEL}")
print("-" * 30)


def test_ping():
    print(f"1. Pinging {AI_BASE_URL}/api/tags...")
    try:
        start = time.time()
        r = httpx.get(f"{AI_BASE_URL}/api/tags", timeout=10.0)
        print(f"   Status: {r.status_code}")
        print(f"   Time: {time.time() - start:.2f}s")
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            print(f"   Models found: {', '.join(models)}")
            return True
    except Exception as e:
        print(f"   ERROR: {e}")
    return False


def test_simple_chat():
    print("\n2. Sending simple non-streaming prompt 'Why is the sky blue?'...")
    payload = {"model": MODEL, "prompt": "Hello", "stream": False}
    try:
        start = time.time()
        # Using a 60s timeout to see if the proxy cuts us off
        # with httpx.Client(timeout=60.0) as client:
        with httpx.Client(timeout=120.0, trust_env=False) as client:
            r = client.post(f"{AI_BASE_URL}/api/generate", json=payload)
            print(f"   Status: {r.status_code}")
            print(f"   Time: {time.time() - start:.2f}s")
            if r.status_code == 200:
                print(f"   Response: {r.json().get('response')}")
            else:
                print(f"   Full Body: {r.text}")
    except httpx.ReadTimeout:
        print(
            "   TIMEOUT: Request timed out after 60s. This suggests a proxy or model loading issue."
        )
    except Exception as e:
        print(f"   ERROR: {e}")


def test_streaming():
    print("\n3. Testing STREAMING prompt...")
    payload = {
        "model": MODEL,
        "prompt": "Tell me a short story about a robot.",
        "stream": True,
    }
    try:
        start = time.time()
        first_token_time = None
        full_text = ""

        with (
            httpx.Client(timeout=60.0) as client,
            client.stream("POST", f"{AI_BASE_URL}/api/generate", json=payload) as r,
        ):
            print(f"   Status: {r.status_code}")
            for line in r.iter_lines():
                if not line:
                    continue
                if first_token_time is None:
                    first_token_time = time.time() - start
                    print(f"   Time to first token: {first_token_time:.2f}s")

                data = json.loads(line)
                full_text += data.get("response", "")
                if data.get("done"):
                    break

        print(f"   Total Time: {time.time() - start:.2f}s")
        print(f"   Text Length: {len(full_text)} characters")
        if not full_text:
            print("   WARNING: Received EMPTY text from stream.")
    except Exception as e:
        print(f"   ERROR: {e}")


if __name__ == "__main__":
    if test_ping():
        test_simple_chat()
        test_streaming()
    else:
        print("\nCould not connect to AI server. Please check AI_BASE_URL in .env.")
