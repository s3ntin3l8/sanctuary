"""Per-stage AI inference options — centralises all magic numbers in one place."""

_TAIL_CHARS = 2000  # chars to include from document tail in head+tail previews

# Qwen3.5 official sampling parameters for thinking-mode reasoning models.
# Hybrid of the "precise coding" profile (lower temperature for structured JSON)
# with presence_penalty=1.5 from the "general tasks" profile — the latter is the
# primary mechanism that prevents the literal "Wait, actually..." self-correction
# loops observed in data/ai_debug/doc_22.md (50+ repetitions, 67k thinking
# chars, 0 response chars). Setting temperature=0.0 with reasoning models is
# documented to make these ruts worse, not better.
# Source: https://huggingface.co/Qwen/Qwen3.5-9B
_QWEN_SAMPLING = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repeat_penalty": 1.0,
}


STAGE_OPTIONS: dict[str, dict] = {
    "metadata": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        # Bumped 6000 → 8000: two-pass mode's pass-1 reasoning chain on this
        # stage routinely landed at 105-109% of a 6000 budget. Headroom
        # eliminates truncation even if the JSON-suppression directive is
        # ever bypassed.
        "num_predict": 8000,
        "max_tokens": 8000,
    },
    "batch_analysis": {
        "num_ctx": 65536,
        **_QWEN_SAMPLING,
        # Bumped 10000 → 12000: pass-1 reasoning on batches of 8+ docs hit the
        # 10000 cap (completion_tokens=10000, reasoning_tokens=9999) in two of
        # four batch_33 runs. Secondary fix — the primary cause of bad bundling
        # was the missing enclosed_doc_id schema field, not budget.
        "num_predict": 12000,
        "max_tokens": 12000,
    },
    "enrich": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        # Bumped 8000 → 12000: complex German legal docs with many key_passages
        # and long management_summary fields fill the JSON output alone.
        # Doc 96 (Ladung zum Erörterungstermin) produced a 29 634-char response
        # (~8 000 tokens) that truncated mid-string-value and failed to parse.
        "num_predict": 12000,
        "max_tokens": 12000,
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
        # Bumped 6000 → 8000: every two-pass claims-p1 in the 2026-05-07
        # retry exceeded a 6000 budget (119-127%), truncating pass-1
        # mid-emit and confusing pass 2.
        "num_predict": 8000,
        "max_tokens": 8000,
    },
    "entities": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        # Bumped 6000 → 8000: entities-p1 hit 86-111% of a 6000 budget.
        # One run (doc_3) truncated mid-`"name": "H` and pass 2 emitted
        # `{"entities": []}` instead of the ~20 entities pass 1 had identified.
        "num_predict": 8000,
        "max_tokens": 8000,
    },
    "case_brief": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        "num_predict": 8000,
        "max_tokens": 8000,
    },
}
