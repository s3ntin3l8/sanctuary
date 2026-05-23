"""Tests for the two-pass dispatch path in call_json_ai.

The single-pass path is exercised by other tests; here we focus on:
- two_pass=True issues two _stream_response calls
- pass 1 has no schema; pass 2 has schema
- pass 2's user prompt embeds pass 1's output between the two `---` markers
- pass 1 reasoning_content promotion: if pass-1 response is empty but
  thinking has content, the thinking becomes the analysis fed to pass 2
- empty pass 2 still triggers the auto-retry; empty pass 1 does NOT
"""

from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict

from app.services.intelligence import _ai_call


class _TestSchema(BaseModel):
    """Minimal schema used as a stand-in to test the two-pass call_json_ai mechanism."""

    model_config = ConfigDict(extra="ignore")

    is_court_document: bool = False
    court_level: str | None = None
    court_name: str | None = None
    az_court: str | None = None
    subject_matter: str | None = None
    appeal_deadline_days: int | None = None


# Alias used throughout this file
ProceedingExtraction = _TestSchema


@pytest.fixture
def patched_provider():
    """Patch get_chat_config + chat_provider so call_json_ai runs without DB."""

    class FakeCfg:
        summary_model = "test-model"

    def fake_get_chat_config(_db):
        return FakeCfg()

    async def fake_get_type():
        return "lmstudio"

    async def fake_get_generate_params(
        *, model, prompt, system_prompt, stream, options
    ):
        # Echo enough to verify what the orchestrator built
        return {
            "url": "http://stub",
            "json": {
                "model": model,
                "prompt": prompt,
                "system": system_prompt,
                "options": options,
                "schema_in_options": "_response_schema" in (options or {}),
            },
            "headers": {},
        }

    with (
        patch.object(_ai_call, "get_chat_config", fake_get_chat_config),
        patch.object(_ai_call.chat_provider, "get_type", fake_get_type, create=True),
        patch.object(
            _ai_call.chat_provider,
            "get_generate_params",
            fake_get_generate_params,
            create=True,
        ),
        patch.object(
            _ai_call.chat_provider,
            "reload_from_db",
            lambda *_a, **_k: None,
            create=True,
        ),
        # call_json_ai's no-db branch reads user_settings via
        # get_ai_debug_redact. Without patching it the test hits the
        # production DB (no user_settings table → OperationalError).
        patch(
            "app.services.user_settings_service.get_ai_debug_redact",
            return_value=False,
        ),
    ):
        yield


@pytest.mark.unit
def test_two_pass_makes_two_stream_calls_with_correct_schema_split(patched_provider):
    """Pass 1 must omit the schema; pass 2 must include it."""
    calls: list[dict] = []

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        calls.append(
            {
                "label": debug_label,
                "schema_in_options": params["json"]["schema_in_options"],
                "user_prompt": params["json"]["prompt"],
            }
        )
        if debug_label.endswith("-p1"):
            return ("Analysis: this is a court letter from AG Hamburg.", "")
        return (
            '{"is_court_document": true, "court_level": "ag", "court_name": null, '
            '"az_court": null, "subject_matter": null, "appeal_deadline_days": null}',
            "",
        )

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        result = _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="USER_ORIGINAL",
            options={},
            debug_label="doc_1_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
        )

    assert len(calls) == 2, "two_pass must issue exactly two stream calls"
    assert calls[0]["label"] == "doc_1_proceeding-p1"
    assert calls[1]["label"] == "doc_1_proceeding-p2"
    assert calls[0]["schema_in_options"] is False, "pass 1 must NOT carry the schema"
    assert calls[1]["schema_in_options"] is True, "pass 2 MUST carry the schema"
    assert isinstance(result, ProceedingExtraction)
    assert result.is_court_document is True


@pytest.mark.unit
def test_two_pass_embeds_pass1_output_in_pass2_user_prompt(patched_provider):
    """Pass 2 user prompt must contain the original prompt + pass-1 analysis."""
    calls: list[dict] = []

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        calls.append({"label": debug_label, "user_prompt": params["json"]["prompt"]})
        if debug_label.endswith("-p1"):
            return ("DEEP_ANALYSIS_TOKEN", "")
        return ('{"is_court_document": false}', "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="ORIGINAL_PROMPT_TOKEN",
            options={},
            debug_label="doc_2_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
        )

    p1, p2 = calls
    assert "ORIGINAL_PROMPT_TOKEN" in p1["user_prompt"]
    assert "DEEP_ANALYSIS_TOKEN" not in p1["user_prompt"]  # not yet
    assert "ORIGINAL_PROMPT_TOKEN" in p2["user_prompt"]
    assert "DEEP_ANALYSIS_TOKEN" in p2["user_prompt"]
    assert "Your prior analysis" in p2["user_prompt"]
    assert "Now output ONLY the JSON matching the schema" in p2["user_prompt"]


@pytest.mark.unit
def test_two_pass_promotes_pass1_thinking_when_response_empty(patched_provider):
    """If pass 1 returns empty content but populated thinking, the thinking
    must be used as the analysis fed into pass 2."""
    calls: list[dict] = []

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        calls.append({"label": debug_label, "user_prompt": params["json"]["prompt"]})
        if debug_label.endswith("-p1"):
            return ("", "REASONING_FROM_THINKING_CHANNEL")
        return ('{"is_court_document": true}', "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="orig",
            options={},
            debug_label="doc_3_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
        )

    p2_prompt = calls[1]["user_prompt"]
    assert "REASONING_FROM_THINKING_CHANNEL" in p2_prompt, (
        "pass 1's thinking channel must be promoted into pass 2's analysis context"
    )


@pytest.mark.unit
def test_two_pass_skips_analysis_block_when_pass1_truly_empty(patched_provider):
    """When pass 1 returns nothing on either channel, pass 2 should run with
    the original prompt unaugmented (no `--- Your prior analysis ---` block)."""
    calls: list[dict] = []

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        calls.append({"label": debug_label, "user_prompt": params["json"]["prompt"]})
        if debug_label.endswith("-p1"):
            return ("", "")
        return ('{"is_court_document": true}', "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="ORIGINAL_PROMPT",
            options={},
            debug_label="doc_4_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
        )

    p2_prompt = calls[1]["user_prompt"]
    assert p2_prompt.startswith("ORIGINAL_PROMPT")
    assert "Your prior analysis" not in p2_prompt


@pytest.mark.unit
def test_two_pass_pass2_empty_triggers_retry(patched_provider):
    """Empty pass 2 must trigger the suppress_thinking auto-retry, not bubble."""
    call_count = {"p1": 0, "p2": 0}
    second_p2_response = (
        '{"is_court_document": true, "court_level": null, "court_name": null, '
        '"az_court": null, "subject_matter": null, "appeal_deadline_days": null}'
    )

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        if debug_label.endswith("-p1"):
            call_count["p1"] += 1
            return ("Some analysis", "")
        # pass 2: first call returns empty; second (the retry) succeeds
        call_count["p2"] += 1
        if call_count["p2"] == 1:
            return ("", "")
        return (second_p2_response, "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        result = _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="orig",
            options={},
            debug_label="doc_5_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
        )

    assert isinstance(result, ProceedingExtraction)
    # The first call_json_ai invocation runs pass1+pass2; the recursive retry
    # runs pass1+pass2 again (suppress_thinking=True). So we expect 2 of each.
    assert call_count["p1"] == 2, (
        f"expected 2 pass-1 calls (initial + retry), got {call_count['p1']}"
    )
    assert call_count["p2"] == 2, (
        f"expected 2 pass-2 calls (initial empty + retry), got {call_count['p2']}"
    )


@pytest.mark.unit
def test_runs_jsonl_records_watchdog_drain_event(
    patched_provider, tmp_path, monkeypatch
):
    """When the thinking-loop watchdog drains a stream, the runs.jsonl entry
    must carry `watchdog: "think_drain"` so log scans can find silently-
    degraded calls (status=ok via channel promotion). Without this signal,
    bursts of watchdog-drained completions look healthy in the index."""
    import json

    from app.services.intelligence import _ai_call as ai_call_mod

    # Redirect ai_debug to a tmp dir so we can inspect the runs.jsonl entry.
    monkeypatch.setattr(ai_call_mod, "DATA_DIR", tmp_path)

    # Patch httpx to return a stream where thinking accumulates past the
    # watchdog threshold without ever producing response tokens. The drain
    # then fires; we want to see the resulting runs.jsonl entry.
    import time

    big_chunk = "x" * (ai_call_mod._THINK_WATCHDOG_CHARS // 4 + 100)

    # The watchdog also requires elapsed time > _THINK_WATCHDOG_SECS — lower
    # it to a value the test can actually exceed without sleeping.
    monkeypatch.setattr(ai_call_mod, "_THINK_WATCHDOG_SECS", 0.0)

    class _FakeResponse:
        is_success = True
        request = None

        def iter_lines(self):
            # Emit four thinking chunks that exceed the watchdog threshold.
            for _ in range(5):
                time.sleep(0.001)
                yield "data: irrelevant"

    class _FakeStream:
        def __enter__(self_inner):
            return _FakeResponse()

        def __exit__(self_inner, *a):
            return False

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *args, **kwargs):
            return _FakeStream()

    monkeypatch.setattr(ai_call_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(ai_call_mod.httpx, "Timeout", lambda **kwargs: None)

    # parse_stream_line returns thinking-only chunks so full_response stays "".
    def _fake_parse(line, _ptype):
        return {"thinking": big_chunk}

    monkeypatch.setattr(ai_call_mod.chat_provider, "parse_stream_line", _fake_parse)
    monkeypatch.setattr(
        ai_call_mod,
        "track_ai_call",
        lambda _: __import__("contextlib").nullcontext(),
    )

    ai_call_mod._stream_response(
        params={"url": "http://x", "json": {}, "headers": {}},
        ptype="openai",
        debug_label="doc_99_enricher",
        resolved_model="test-model",
        ingest_batch_id=None,
        doc_case_id=None,
        redact=False,
    )

    runs_jsonl = tmp_path / "ai_debug" / "runs.jsonl"
    assert runs_jsonl.exists(), "runs.jsonl was not written"
    entries = [json.loads(line) for line in runs_jsonl.read_text().splitlines()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["watchdog"] == "think_drain", (
        f"expected watchdog=think_drain, got {entry.get('watchdog')}"
    )
    # status remains ok because no exception fired — the watchdog is a soft
    # signal, not an error. The combination (status=ok, watchdog=think_drain)
    # is what makes this call "silently degraded".
    assert entry["status"] == "ok"


@pytest.mark.unit
def test_runs_jsonl_watchdog_field_null_on_normal_completion(
    patched_provider, tmp_path, monkeypatch
):
    """The watchdog field is null/None when no drain fired — distinguishes
    healthy calls from ones that landed in the thinking channel via drain."""
    import json

    from app.services.intelligence import _ai_call as ai_call_mod

    monkeypatch.setattr(ai_call_mod, "DATA_DIR", tmp_path)

    class _FakeResponse:
        is_success = True
        request = None

        def iter_lines(self):
            yield "data: ok"

    class _FakeStream:
        def __enter__(self_inner):
            return _FakeResponse()

        def __exit__(self_inner, *a):
            return False

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *args, **kwargs):
            return _FakeStream()

    monkeypatch.setattr(ai_call_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(ai_call_mod.httpx, "Timeout", lambda **kwargs: None)

    def _fake_parse(line, _ptype):
        return {"response": "ok", "done": True}

    monkeypatch.setattr(ai_call_mod.chat_provider, "parse_stream_line", _fake_parse)
    monkeypatch.setattr(
        ai_call_mod,
        "track_ai_call",
        lambda _: __import__("contextlib").nullcontext(),
    )

    ai_call_mod._stream_response(
        params={"url": "http://x", "json": {}, "headers": {}},
        ptype="openai",
        debug_label="doc_99_enricher",
        resolved_model="test-model",
        ingest_batch_id=None,
        doc_case_id=None,
        redact=False,
    )

    runs_jsonl = tmp_path / "ai_debug" / "runs.jsonl"
    entries = [json.loads(line) for line in runs_jsonl.read_text().splitlines()]
    assert entries[0]["watchdog"] is None


@pytest.mark.unit
def test_two_pass_watchdog_drain_short_circuits_retry(patched_provider):
    """When pass-2's thinking exceeds _THINK_WATCHDOG_CHARS with empty
    response, the model has spun in a reasoning loop. Retrying the same
    prompt is denial — the next attempt almost always reproduces the loop.
    The inner retry MUST be skipped; ValueError is raised so outer callers
    handle fallback (e.g. batch_analyzer's own outer retry).

    Regression for the IB-0033 enricher storm: doc 95 had 34 attempts and
    doc 98 had 31, driven by this inner-retry compounding with Celery's
    outer max_retries.
    """
    call_count = {"p1": 0, "p2": 0}

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        if debug_label.endswith("-p1"):
            call_count["p1"] += 1
            return ("Some analysis", "")
        # Pass 2: empty response, but massive thinking — the watchdog-drain
        # signature. Must NOT trigger the suppress_thinking retry.
        call_count["p2"] += 1
        # Long-thinking with NO json brace, so the channel-promotion at
        # _ai_call.py:769-782 doesn't kick in either.
        return ("", "x" * (_ai_call._THINK_WATCHDOG_CHARS + 1000))

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        with pytest.raises(ValueError, match="Thinking-loop trap"):
            _ai_call.call_json_ai(
                system_prompt="sys",
                user_prompt="orig",
                options={},
                debug_label="doc_99_enricher",
                schema=ProceedingExtraction,
                two_pass=True,
            )

    assert call_count["p1"] == 1, (
        f"watchdog-drain must not retry pass-1; got {call_count['p1']} calls"
    )
    assert call_count["p2"] == 1, (
        f"watchdog-drain must not retry pass-2; got {call_count['p2']} calls"
    )


@pytest.mark.unit
def test_single_pass_unchanged(patched_provider):
    """two_pass=False (default) must still produce exactly one stream call."""
    calls: list[str] = []

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        calls.append(debug_label)
        return ('{"is_court_document": true}', "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        result = _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="orig",
            options={},
            debug_label="doc_6_proceeding",
            schema=ProceedingExtraction,
            # two_pass omitted — defaults to False
        )

    assert calls == ["doc_6_proceeding"], (
        "single-pass must use the bare debug_label and issue one call"
    )
    assert isinstance(result, ProceedingExtraction)


@pytest.mark.unit
def test_pass1_user_prompt_carries_no_json_directive(patched_provider):
    """Pass 1 must augment the user prompt with a 'don't output JSON yet'
    directive so the model spends its budget on reasoning instead of
    re-emitting the structured output that pass 2 will produce under
    grammar enforcement. Regression: without this, pass 1 routinely hit
    the stage's max_tokens cap mid-JSON-emit on claims/entities/metadata,
    leaving pass 2 with truncated analysis context."""
    seen_prompts: dict[str, str] = {}

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        seen_prompts[debug_label] = params["json"]["prompt"]
        if debug_label.endswith("-p1"):
            return ("Analysis prose only.", "")
        return ('{"is_court_document": true}', "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="ORIGINAL_PROMPT",
            options={},
            debug_label="doc_9_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
        )

    p1_prompt = seen_prompts["doc_9_proceeding-p1"]
    assert "ORIGINAL_PROMPT" in p1_prompt
    assert "Analysis pass" in p1_prompt
    assert "Do NOT output JSON" in p1_prompt
    # Pass 2 should NOT carry the same directive (it needs JSON output)
    p2_prompt = seen_prompts["doc_9_proceeding-p2"]
    assert "Do NOT output JSON" not in p2_prompt
    assert "Now output ONLY the JSON" in p2_prompt


@pytest.mark.unit
def test_pass1_inherits_caller_max_tokens_by_default(patched_provider):
    """Without an explicit `pass1_max_tokens=`, pass 1 must use whatever the
    caller's options.max_tokens specifies — no surprise cap."""
    seen_max_tokens: dict[str, int | None] = {}

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        seen_max_tokens[debug_label] = params["json"]["options"].get("max_tokens")
        if debug_label.endswith("-p1"):
            return ("Analysis", "")
        return ('{"is_court_document": true}', "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="orig",
            options={"max_tokens": 8000},
            debug_label="doc_8_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
            # pass1_max_tokens omitted — should default to None
        )

    assert seen_max_tokens["doc_8_proceeding-p1"] == 8000, (
        "default pass1_max_tokens=None must inherit caller's max_tokens, not cap"
    )
    assert seen_max_tokens["doc_8_proceeding-p2"] == 8000


@pytest.mark.unit
def test_pass1_max_tokens_caps_max_tokens(patched_provider):
    """Pass 1 max_tokens must be capped to pass1_max_tokens, regardless of
    what the caller put in options."""
    seen_max_tokens: dict[str, int | None] = {}

    def fake_stream(
        *,
        params,
        ptype,
        debug_label,
        resolved_model,
        ingest_batch_id,
        doc_case_id=None,
        redact=False,
    ):
        seen_max_tokens[debug_label] = params["json"]["options"].get("max_tokens")
        if debug_label.endswith("-p1"):
            return ("Analysis", "")
        return ('{"is_court_document": true}', "")

    with patch.object(_ai_call, "_stream_response", side_effect=fake_stream):
        _ai_call.call_json_ai(
            system_prompt="sys",
            user_prompt="orig",
            options={"max_tokens": 9999},  # caller wants huge cap
            debug_label="doc_7_proceeding",
            schema=ProceedingExtraction,
            two_pass=True,
            pass1_max_tokens=500,
        )

    p1_max = seen_max_tokens["doc_7_proceeding-p1"]
    p2_max = seen_max_tokens["doc_7_proceeding-p2"]
    assert p1_max == 500, f"pass 1 should cap max_tokens to 500, got {p1_max}"
    assert p2_max == 9999, f"pass 2 should preserve caller's max_tokens, got {p2_max}"


@pytest.mark.unit
def test_runs_jsonl_entry_carries_doc_case_and_batch_for_doc_scoped_calls(
    tmp_path, monkeypatch
):
    """A doc-scoped call must report the doc's case_id and ingest_batch_id in
    the runs.jsonl entry. Previously batch_id and case_id were null on doc-
    scoped entries, making it impossible to filter the index by case/batch
    without joining against the documents table."""
    import json

    monkeypatch.setattr(_ai_call, "DATA_DIR", tmp_path)
    (tmp_path / "ai_debug").mkdir(parents=True, exist_ok=True)
    runs_jsonl = tmp_path / "ai_debug" / "runs.jsonl"

    _ai_call._append_index(
        runs_jsonl,
        started_at="2026-05-07T20:00:00Z",
        kind="doc",
        scope_id="42",
        stage="proceeding-p2",
        ingest_batch_id=4,
        doc_case_id="ADV-099-Z",
        model="qwen/qwen3.5-9b",
        provider="openai",
        duration_ms=1234,
        ttfb_ms=100,
        response_len=200,
        thinking_len=0,
        status="ok",
        error=None,
    )

    assert runs_jsonl.exists()
    line = runs_jsonl.read_text().strip()
    entry = json.loads(line)
    assert entry["doc_id"] == 42
    assert entry["batch_id"] == 4, (
        "doc-scoped entry must surface the doc's batch (was null before)"
    )
    assert entry["case_id"] == "ADV-099-Z", (
        "doc-scoped entry must surface the doc's case (was null before)"
    )
    # The legacy `ingest_batch_id` field is gone — `batch_id` is the single
    # source of truth now.
    assert "ingest_batch_id" not in entry


@pytest.mark.unit
def test_runs_jsonl_entry_carries_case_for_batch_scoped_calls(tmp_path, monkeypatch):
    """A batch-scoped call (kind=batch) must report the batch's case_id when
    it's known at call time."""
    import json

    monkeypatch.setattr(_ai_call, "DATA_DIR", tmp_path)
    (tmp_path / "ai_debug").mkdir(parents=True, exist_ok=True)
    runs_jsonl = tmp_path / "ai_debug" / "runs.jsonl"

    _ai_call._append_index(
        runs_jsonl,
        started_at="2026-05-07T20:00:00Z",
        kind="batch",
        scope_id="4",
        stage="analyzer-p2",
        ingest_batch_id=4,
        doc_case_id="ADV-100-Z",
        model="qwen/qwen3.5-9b",
        provider="openai",
        duration_ms=2000,
        ttfb_ms=200,
        response_len=300,
        thinking_len=0,
        status="ok",
        error=None,
    )

    entry = json.loads(runs_jsonl.read_text().strip())
    assert entry["batch_id"] == 4
    assert entry["doc_id"] is None
    assert entry["case_id"] == "ADV-100-Z"


@pytest.mark.unit
def test_runs_jsonl_entry_for_case_scoped_calls(tmp_path, monkeypatch):
    """A case-scoped call (kind=case) puts the case ID in case_id."""
    import json

    monkeypatch.setattr(_ai_call, "DATA_DIR", tmp_path)
    (tmp_path / "ai_debug").mkdir(parents=True, exist_ok=True)
    runs_jsonl = tmp_path / "ai_debug" / "runs.jsonl"

    _ai_call._append_index(
        runs_jsonl,
        started_at="2026-05-07T20:00:00Z",
        kind="case",
        scope_id="ADV-101-Z",
        stage="brief-p2",
        ingest_batch_id=None,
        doc_case_id=None,
        model="qwen/qwen3.5-9b",
        provider="openai",
        duration_ms=3000,
        ttfb_ms=300,
        response_len=400,
        thinking_len=0,
        status="ok",
        error=None,
    )

    entry = json.loads(runs_jsonl.read_text().strip())
    assert entry["case_id"] == "ADV-101-Z"
    assert entry["doc_id"] is None
    assert entry["batch_id"] is None
