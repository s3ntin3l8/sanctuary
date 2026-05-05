"""Per-stage AI inference options — centralises all magic numbers in one place."""

_TAIL_CHARS = 2000  # chars to include from document tail in head+tail previews

STAGE_OPTIONS: dict[str, dict] = {
    "metadata": {
        "num_ctx": 32768,
        "temperature": 0.1,
        "num_predict": 10000,
        "max_tokens": 10000,
    },
    "batch_analysis": {
        "num_ctx": 32768,
        "temperature": 0.1,
        "num_predict": 20000,
        "max_tokens": 20000,
    },
    "enrich": {
        "num_ctx": 32768,
        "temperature": 0.2,
        "num_predict": 20000,
        "max_tokens": 20000,
    },
    "relationships": {
        "num_ctx": 32768,
        "temperature": 0.1,
        # 16000 (was 10000) — qwen-3.5-9b's thinking chain on multi-doc
        # relationship reasoning hits 10000+ tokens on hard cases (~21% of
        # historical runs returned empty at the 10000 cap). Same pattern as
        # proceeding stage. See data/ai_debug/runs.jsonl thinking_len.
        "num_predict": 16000,
        "max_tokens": 16000,
    },
    "claims": {
        "num_ctx": 32768,
        "temperature": 0.1,
        "num_predict": 15000,
        "max_tokens": 15000,
    },
    "entities": {
        "num_ctx": 32768,
        "temperature": 0.1,
        "num_predict": 15000,
        "max_tokens": 15000,
    },
    "case_brief": {
        "num_ctx": 32768,
        "temperature": 0.2,
        # 16000 (was 10000) — case-level synthesis thinking can exceed 9500
        # tokens on cases with many docs, saturating the 10000 cap (~24% of
        # historical brief runs returned empty). See runs.jsonl thinking_len.
        "num_predict": 16000,
        "max_tokens": 16000,
    },
    "proceeding": {
        "num_ctx": 16384,
        "temperature": 0.0,
        # 8000 (was 2000) — qwen-3.5-9b's thinking chain alone consumes ~1975
        # tokens on this stage's prompt; the old 2000 cap left zero budget for
        # the ~50-token JSON response and produced empty responses ~50% of the
        # time. See data/ai_debug/runs.jsonl thinking_len pattern.
        "num_predict": 8000,
        "max_tokens": 8000,
    },
}
