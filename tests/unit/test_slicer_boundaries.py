"""Slicer boundary signals and the every-boundary AI judgment."""

import asyncio

import pytest

from app.services.ingestion import slicer
from app.services.ingestion.slicer import _Boundary, _boundary_signals


def test_page_reset_matches_seite_n_von_m():
    signals = _boundary_signals("", "weiter auf Seite 2 von 2", "Seite 1 von 3 Betreff")
    assert "page_reset" in signals


def test_bare_folio_numbers_are_no_signal():
    assert (
        _boundary_signals("", "Hans 12", "13 Daher bestand für den Antragsgegner") == []
    )


def test_enclosure_marker_only_counts_at_the_top_of_the_page():
    top = _boundary_signals("", "x", "Anlage K 3 Kontoauszug")
    buried = _boundary_signals("", "x", "y" * 200 + " Anlagen: 3")
    assert "enclosure_marker" in top
    assert "enclosure_marker" not in buried


def test_transmittal_sheet_is_a_signal_even_without_umlaut():
    assert "transmittal_page" in _boundary_signals(
        "", "x", "Zu 17 Ubertragungsnachweis"
    )


def test_no_signal_on_a_plain_continuation():
    assert (
        _boundary_signals(
            "", "und deshalb", "beantragen wir daher ausdrücklich die Aufhebung"
        )
        == []
    )


def test_judgment_prompt_carries_context_and_signals():
    b = _Boundary(5, "tail", "head", "prev start", ("az_change", "blank_page"))
    prompt = slicer._judgment_prompt(b)
    assert "Page 4 starts:\nprev start" in prompt
    assert "Page 4 ends:\ntail" in prompt
    assert "Page 5 (the page in question) starts:\nhead" in prompt
    assert prompt.endswith("Heuristic signals: az_change, blank_page")
    assert slicer._judgment_prompt(_Boundary(2, "t", "h")).endswith("signals: none")


@pytest.mark.asyncio
async def test_every_boundary_is_judged_within_the_concurrency_limit(monkeypatch):
    monkeypatch.setattr(slicer, "_AI_CONCURRENCY", 2)
    running = peak = 0

    async def fake(b, model, client):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {"is_new_document": True, "confidence": "high"}

    monkeypatch.setattr(slicer, "_ai_cut_judgment", fake)
    boundaries = [_Boundary(p, "t", "h") for p in range(2, 12)]

    out = await slicer._ai_cut_judgments(boundaries, "m")

    assert sorted(out) == list(range(2, 12))
    assert peak == 2


@pytest.mark.asyncio
async def test_a_crashing_judgment_becomes_no_cut(monkeypatch):
    async def fake(b, model, client):
        raise RuntimeError("model gone")

    monkeypatch.setattr(slicer, "_ai_cut_judgment", fake)
    out = await slicer._ai_cut_judgments([_Boundary(2, "t", "h")], "m")
    assert out[2]["is_new_document"] is False


@pytest.mark.asyncio
async def test_judgment_is_schema_constrained_so_a_reasoning_model_cannot_stall(
    monkeypatch,
):
    sent = {}

    class FakeProvider:
        async def get_generate_params(self, **kwargs):
            sent.update(kwargs)
            return {"url": "http://x", "json": {}, "headers": {}}

        async def get_type(self):
            return "ollama"

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "response": '{"is_new_document": true, "confidence": "high", '
                '"kind": "letter", "notes": "new"}'
            }

    class FakeClient:
        async def post(self, url, json, headers):
            return FakeResponse()

    monkeypatch.setattr(slicer, "chat_provider", FakeProvider())

    result = await slicer._ai_cut_judgment(_Boundary(2, "t", "h"), "m", FakeClient())

    assert result["is_new_document"] is True
    options = sent["options"]
    assert options["_schema_name"] == "CutJudgment"
    assert "is_new_document" in options["_response_schema"]["properties"]
    assert options["num_ctx"] == 4096
    assert options["max_tokens"] == slicer._AI_MAX_TOKENS
    assert options["_include_user_context"] is False
