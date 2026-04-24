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
        "num_predict": 10000,
        "max_tokens": 10000,
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
        "num_predict": 10000,
        "max_tokens": 10000,
    },
}
