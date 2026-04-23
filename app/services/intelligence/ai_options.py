"""Per-stage AI inference options — centralises all magic numbers in one place."""

_TAIL_CHARS = 2000  # chars to include from document tail in head+tail previews

STAGE_OPTIONS: dict[str, dict] = {
    "metadata": {
        "num_ctx": 16384,
        "temperature": 0.1,
        "num_predict": 1000,
        "max_tokens": 1000,
    },
    "batch_analysis": {
        "num_ctx": 8192,
        "temperature": 0.1,
        "num_predict": 2000,
        "max_tokens": 2000,
    },
    "enrich": {
        "num_ctx": 16384,
        "temperature": 0.2,
        "num_predict": 2000,
        "max_tokens": 2000,
    },
    "relationships": {
        "num_ctx": 8192,
        "temperature": 0.1,
        "num_predict": 1000,
        "max_tokens": 1000,
    },
    "claims": {
        "num_ctx": 8192,
        "temperature": 0.1,
        "num_predict": 1500,
        "max_tokens": 1500,
    },
    "entities": {
        "num_ctx": 8192,
        "temperature": 0.1,
        "num_predict": 1500,
        "max_tokens": 1500,
    },
    "case_brief": {
        "num_ctx": 8192,
        "temperature": 0.2,
        "num_predict": 1000,
        "max_tokens": 1000,
    },
}
