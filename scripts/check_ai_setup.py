import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Add app to path to import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

try:
    from app.config import AI_BASE_URL, AI_EMBED_MODEL, AI_SUMMARY_MODEL
except ImportError:
    # Use environment AI_BASE_URL if available
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://ollama.in.s3ntin3l8.de")
    AI_SUMMARY_MODEL = os.getenv("AI_SUMMARY_MODEL", "qwen3.5-9b-16k:latest")
    AI_EMBED_MODEL = os.getenv("AI_EMBED_MODEL", "nomic-embed-text:v1.5")


def check_python():
    print("[*] Checking Python version...")
    print(f"    Version: {sys.version}")
    print("    [✓] Python version OK")


def check_ollama():
    print(f"[*] Checking AI connection at {AI_BASE_URL}...")
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{AI_BASE_URL}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m["name"] for m in data.get("models", [])]
            print("    [✓] AI server is REACHABLE")
            print(
                f"    [✓] Available models: {', '.join(models) if models else 'None'}"
            )

            # Check for specifically required models
            required = [AI_SUMMARY_MODEL, AI_EMBED_MODEL]
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
        print(f"    [!] Connection to {AI_BASE_URL} failed.")
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
                            f"    [✓] AI server is REACHABLE via container IP ({alt_url})"
                        )
                        print(
                            f"    [!] TIP: Use '{alt_url}' in your .env if the hostname is not resolving."
                        )
                        return
        except Exception:
            pass

        print(f"    [✗] AI connection FAILED: {e}")
        print(
            f"    [!] Make sure your AI backend is running and accessible at {AI_BASE_URL}"
        )


def check_pgvector():
    print("[*] Checking Postgres + pgvector...")
    try:
        from sqlalchemy import text

        from app.config import SQLALCHEMY_DATABASE_URL, engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            print(f"    [✓] Connected to {SQLALCHEMY_DATABASE_URL}")
            ext = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).fetchone()
            if ext:
                print(f"    [✓] pgvector extension installed (v{ext[0]})")
            else:
                print(
                    "    [✗] pgvector extension not installed in this database "
                    "(run `make migrate` — the baseline migration creates it)."
                )
    except Exception as e:
        print(f"    [✗] Postgres/pgvector check FAILED: {e}")
        print("    [!] Make sure Postgres is running (`make db-up`) and migrated.")


def main():
    print("=== Sanctuary AI Readiness Check ===\n")
    check_python()
    print()
    check_ollama()
    print()
    check_pgvector()
    print("\n====================================")


if __name__ == "__main__":
    main()
