import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load env
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

AI_BASE_URL = os.getenv("AI_BASE_URL", "http://localhost:11434").rstrip("/")
AI_SUMMARY_MODEL = os.getenv("AI_SUMMARY_MODEL", "qwen3.5:9b")
AI_EMBED_MODEL = os.getenv("AI_EMBED_MODEL", "nomic-embed-text")

print("--- AI Debug Script ---")
print(f"Base URL: {AI_BASE_URL}")
print(f"Summary Model: {AI_SUMMARY_MODEL}")
print(f"Embed Model: {AI_EMBED_MODEL}")
print("-" * 30)


def test_connection():
    print("Testing connection to tags endpoint...")
    start = time.time()
    try:
        r = httpx.get(f"{AI_BASE_URL}/api/tags", timeout=10.0)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        print(f"SUCCESS: Connected in {time.time() - start:.2f}s")
        print(f"Available models: {', '.join(models)}")
        return True
    except Exception as e:
        print(f"FAILED: {e}")
        return False


def test_simple_generate():
    print(f"\nTesting simple generation with {AI_SUMMARY_MODEL}...")
    start = time.time()
    try:
        payload = {
            "model": AI_SUMMARY_MODEL,
            "prompt": "Say hello world in one word.",
            "stream": False,
        }
        r = httpx.post(f"{AI_BASE_URL}/api/generate", json=payload, timeout=30.0)
        r.raise_for_status()
        print(f"SUCCESS: Received response in {time.time() - start:.2f}s")
        print(f"Response: {r.json().get('response')}")
    except Exception as e:
        print(f"FAILED: {e}")


def test_legal_summary():
    print("\nTesting complex legal summary generation (simulating app behavior)...")
    content = """
    VORSCHUSSRECHNUNG
    Gegenstandswert: 5.000,00 EUR

    1,3 Geschäftsgebühr gemäß Nr. 2300 VV RVG ... 434,20 EUR
    Auslagenpauschale Nr. 7002 VV RVG ... 20,00 EUR
    19 % MwSt. Nr. 7008 VV RVG ... 86,30 EUR
    Gesamtbetrag: 540,50 EUR

    Bitte überweisen Sie den Betrag bis zum 24.04.2026.
    """

    system_prompt = """You are a legal document analyst.
    Analyze the provided document and return a JSON object with exactly these three keys:
    - legal_significance: What does this document mean for our legal position?
    - required_action: What needs to be done and by when?
    - financial_impact: Any fees, costs, or financial implications?
    Return ONLY valid JSON."""

    start = time.time()
    try:
        payload = {
            "model": AI_SUMMARY_MODEL,
            "prompt": f"{system_prompt}\n\nDocument:\n{content}",
            "stream": False,
            "format": "json",
        }
        print("Sending request with format='json' (timeout=120s)...")
        r = httpx.post(f"{AI_BASE_URL}/api/generate", json=payload, timeout=120.0)
        r.raise_for_status()
        print(f"SUCCESS: Received response in {time.time() - start:.2f}s")
        print(f"Response: {json.dumps(r.json().get('response'), indent=2)}")
    except Exception as e:
        print(f"FAILED: {e}")


def test_embedding():
    print(f"\nTesting embedding generation with {AI_EMBED_MODEL}...")
    start = time.time()
    try:
        payload = {
            "model": AI_EMBED_MODEL,
            "prompt": "This is a test document for semantic embedding generation.",
        }
        r = httpx.post(f"{AI_BASE_URL}/api/embeddings", json=payload, timeout=60.0)
        r.raise_for_status()
        print(f"SUCCESS: Received embedding in {time.time() - start:.2f}s")
        emb = r.json().get("embedding")
        print(f"Embedding size: {len(emb) if emb else 0}")
    except Exception as e:
        print(f"FAILED: {e}")


def test_optimized_models():
    models_to_test = ["qwen3.5-9b-16k:latest", "qwen3.5-9b-32k:latest"]

    # Actual app prompt and content length
    system_prompt = """You are a legal document analyst.
    Analyze the provided document and return a JSON object with exactly these three keys:
    - legal_significance: What does this document mean for our legal position?
    - required_action: What needs to be done and by when?
    - financial_impact: Any fees, costs, or financial implications?
    Return ONLY valid JSON."""

    content = (
        "RECHNUNG\n" + "Legal service detail text here. " * 200 + "\nTotal: 1.200 EUR"
    )

    for model in models_to_test:
        print(f"\n--- Testing Optimized Model: {model} ---")
        start = time.time()
        try:
            payload = {
                "model": model,
                "prompt": f"{system_prompt}\n\nDocument:\n{content}",
                "stream": False,
                "format": "json",
            }
            # Use 130s to see if we can get past the suspected 60s/120s proxy/server limits
            print("Sending request (timeout=130s)...")
            r = httpx.post(f"{AI_BASE_URL}/api/generate", json=payload, timeout=130.0)

            duration = time.time() - start
            print(f"Response Status: {r.status_code}")
            print(f"Duration: {duration:.2f}s")

            if r.status_code == 200:
                resp = r.json().get("response")
                print("SUCCESS: Received response")
                print(f"Response (JSON): {resp}")
            else:
                print(f"FAILED: Server returned {r.status_code}")
                print(f"Body: {r.text[:500]}")

        except httpx.ReadTimeout:
            print(f"FAILED: Client ReadTimeout after {time.time() - start:.2f}s")
        except Exception as e:
            print(f"FAILED: {e}")


if __name__ == "__main__":
    if test_connection():
        test_optimized_models()
        test_embedding()
