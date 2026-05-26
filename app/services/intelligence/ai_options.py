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
        # Bumped 8000 → 12000: metadata pass-1 was hitting the cap on ~25% of
        # multi-page docs (2026-05-25 re-enrichment audit: 27/114 sync-p1 calls
        # had completion_tokens == 8000 with response_len=0 — model exhausted
        # the budget mid-thinking and emitted no JSON). Same rationale as the
        # earlier enricher bump. The remaining empty-response cases (Mode B —
        # model terminates after `</think>` without writing the JSON despite
        # having budget left) are a qwen3.5-9b behavior the bump does not
        # address — see _ai_call.py:792-799 for the thinking-as-analysis
        # fallback that handles those.
        "num_predict": 12000,
        "max_tokens": 12000,
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
        # Bumped 6000 → 10000: post-R4 audit (2026-05-26) shows 7/10
        # relationships-p1 calls (70%) cap-hit at 6000 — every empty
        # relationships-p1 in the window was Mode A. Smaller bump than
        # claims/metadata because relationship reasoning is structurally
        # simpler, but 6000 left no headroom for thinking-heavy docs.
        "num_predict": 10000,
        "max_tokens": 10000,
    },
    "claims": {
        "num_ctx": 32768,
        **_QWEN_SAMPLING,
        # Bumped 8000 → 12000: post-R4 audit (2026-05-26) shows 14/41
        # claims-p1 calls (34%) cap-hit at 8000 with response_len=0 — same
        # Mode A pattern as the prior metadata and enricher bumps. Claims
        # pass-1 reasons against an existing-claims context list and needs
        # the headroom to commit to JSON. (Earlier 6000→8000 bump on
        # 2026-05-07 was insufficient.)
        "num_predict": 12000,
        "max_tokens": 12000,
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
