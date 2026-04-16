import json

import httpx

# Use IPv4 and specific model
URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3.5-9b-16k:latest"


def ask_complex_streaming():
    # A logic puzzle that requires multi-step reasoning
    complex_prompt = (
        "There are three boxes. One contains only apples, one contains only oranges, "
        "and one contains both apples and oranges. All three boxes are labeled incorrectly. "
        "You can pick one fruit from one box. How can you label all the boxes correctly? "
        "Think step by step and explain your logic."
    )

    payload = {
        "model": MODEL,
        "prompt": complex_prompt,
        "stream": True,
        "options": {
            "temperature": 0.4,  # Lower for logic puzzles
            "num_ctx": 4096,  # Conservative memory footprint
            "top_k": 40,
            "top_p": 0.9,
        },
    }

    print(f"Connecting to {MODEL}...")
    print("-" * 30)

    try:
        # 120s timeout allows the Docker host to swap the model into VRAM if needed
        with httpx.Client(timeout=120.0, trust_env=False) as client:
            with client.stream("POST", URL, json=payload) as r:
                r.raise_for_status()

                thinking_started = False
                answer_started = False

                for line in r.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)

                    # 1. Handle "Thinking" tokens (if the model uses the thinking field)
                    thought = data.get("thinking", "")
                    if thought:
                        if not thinking_started:
                            print("\n[THINKING]\n", end="", flush=True)
                            thinking_started = True
                        print(f"\033[3m{thought}\033[0m", end="", flush=True)

                    # 2. Handle "Response" tokens
                    chunk = data.get("response", "")
                    if chunk:
                        if not answer_started:
                            print("\n\n[FINAL ANSWER]\n", end="", flush=True)
                            answer_started = True
                        print(chunk, end="", flush=True)

                    if data.get("done"):
                        print("\n" + "-" * 30)
                        print(
                            f"Total Duration: {data.get('total_duration', 0) / 1e9:.2f}s"
                        )
                        print(
                            f"Context used: {data.get('prompt_eval_count', 0)} tokens"
                        )
                        break

    except httpx.ReadTimeout:
        print(
            "\n\nERROR: ReadTimeout. The model is taking too long to generate the next token."
        )
        print("Tip: Check 'docker stats' to see if your CPU/GPU is pegged at 100%.")
    except Exception as e:
        print(f"\n\nERROR: {type(e).__name__} - {e}")


if __name__ == "__main__":
    ask_complex_streaming()
