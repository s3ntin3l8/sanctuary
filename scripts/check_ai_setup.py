import os
import sqlite3
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Add app to path to import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

try:
    from app.config import OLLAMA_BASE_URL, OLLAMA_EMBED_MODEL, OLLAMA_SUMMARY_MODEL
except ImportError:
    # Use environment OLLAMA_BASE_URL if available
    OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "https://ollama.in.s3ntin3l8.de")
    OLLAMA_SUMMARY_MODEL = os.getenv("OLLAMA_SUMMARY_MODEL", "qwen3.5:9b")
    OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")


def check_python():
    print("[*] Checking Python version...")
    print(f"    Version: {sys.version}")
    if sys.version_info < (3, 12):
        print("    [!] Warning: Recommended Python version is 3.12+")
    else:
        print("    [✓] Python version OK")


def check_ollama():
    print(f"[*] Checking Ollama connection at {OLLAMA_BASE_URL}...")
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            print("    [✓] Ollama is REACHABLE")
            print(
                f"    [✓] Available models: {', '.join(models) if models else 'None'}"
            )

            # Check for specifically required models
            required = [OLLAMA_SUMMARY_MODEL, OLLAMA_EMBED_MODEL]
            for model in required:
                # Ollama often appends :latest or other tags, check for partial match if needed
                if any(model in m for m in models):
                    print(f"    [✓] Model '{model}' is INSTALLED")
                else:
                    print(
                        f"    [✗] Model '{model}' is MISSING (Run 'ollama pull {model}')"
                    )
    except Exception as e:
        # Smart Probe: If connection fails, try to find a local docker container
        print(f"    [!] Connection to {OLLAMA_BASE_URL} failed.")
        print("    [*] Attempting smart discovery of local Ollama container...")

        try:
            import subprocess

            res = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "ollama",
                    "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                ],
                capture_output=True,
                text=True,
            )
            container_ip = res.stdout.strip()
            if container_ip:
                alt_url = f"http://{container_ip}:11434"
                print(f"    [*] Found Ollama container at {alt_url}. Testing...")
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(f"{alt_url}/api/tags")
                    if resp.status_code == 200:
                        print(
                            f"    [✓] Ollama is REACHABLE via container IP ({alt_url})"
                        )
                        print(
                            f"    [!] TIP: Use '{alt_url}' in your .env if the hostname is not resolving."
                        )
                        return
        except Exception:
            pass

        print(f"    [✗] Ollama connection FAILED: {e}")
        print(
            f"    [!] Make sure Ollama is running and accessible at {OLLAMA_BASE_URL}"
        )


def check_sqlite_vec():
    print("[*] Checking sqlite-vec support...")
    try:
        import sqlite_vec

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        print("    [✓] sqlite-vec extension LOADED successfully")
        print(f"    [✓] Extension path: {sqlite_vec.loadable_path()}")
    except (ImportError, sqlite3.OperationalError, Exception) as e:
        print(f"    [✗] sqlite-vec check FAILED: {e}")
        print("    [!] Ensure 'sqlite-vec' is installed (pip install sqlite-vec).")


def main():
    print("=== Sanctuary AI Readiness Check ===\n")
    check_python()
    print()
    check_ollama()
    print()
    check_sqlite_vec()
    print("\n====================================")


if __name__ == "__main__":
    main()
