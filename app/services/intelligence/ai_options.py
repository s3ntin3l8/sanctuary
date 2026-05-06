"""Per-stage AI inference options — centralises all magic numbers in one place."""

_TAIL_CHARS = 2000  # chars to include from document tail in head+tail previews

# Qwen3.5 official sampling parameters for thinking-mode reasoning models.
# Hybrid of the "precise coding" profile (lower temperature for structured JSON)
# with presence_penalty=1.5 from the "general tasks" profile — the latter is the
# primary mechanism that prevents the literal "Wait, actually..." self-correction
# loops observed in data/ai_debug/doc_22.log (50+ repetitions, 67k thinking
# chars, 0 response chars). Setting temperature=0.0 with reasoning models is
# documented to make these ruts worse, not better.
# Source: https://huggingface.co/Qwen/Qwen3.5-9B
_QWEN_SAMPLING = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
}


STAGE_OPTIONS: dict[str, dict] = {
    "metadata": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 6000,
        "max_tokens": 6000,
    },
    "batch_analysis": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 10000,
        "max_tokens": 10000,
    },
    "enrich": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 8000,
        "max_tokens": 8000,
    },
    "relationships": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 6000,
        "max_tokens": 6000,
    },
    "claims": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 6000,
        "max_tokens": 6000,
    },
    "entities": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 6000,
        "max_tokens": 6000,
    },
    "case_brief": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 8000,
        "max_tokens": 8000,
    },
    "proceeding": {
        "num_ctx": 16384,
        **_QWEN_SAMPLING,
        # 8000 (was 2000) — qwen-3.5-9b's thinking chain alone consumes ~1975
        # tokens on this stage's prompt; the old 2000 cap left zero budget for
        # the ~50-token JSON response and produced empty responses ~50% of the
        # time. See data/ai_debug/runs.jsonl thinking_len pattern.
        "num_predict": 8000,
        "max_tokens": 8000,
    },
}
