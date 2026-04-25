"""Unit tests for the render_highlighted Jinja filter (app/main.py)."""

import pytest


def _rh(value, key_passages=None, passage_claim_ids=None):
    from app.main import render_highlighted

    return str(render_highlighted(value, key_passages, passage_claim_ids))


@pytest.mark.unit
def test_render_highlighted_no_passages():
    result = _rh("Hello world")
    assert "Hello world" in result
    assert "<mark" not in result
    assert "title=" not in result


@pytest.mark.unit
def test_render_highlighted_emits_mark_attributes():
    passages = [{"text": "important text", "kind": "critical", "id": "abc123456789"}]
    result = _rh("This is important text in context.", passages)

    assert 'id="p-abc123456789"' in result
    assert 'data-passage-id="abc123456789"' in result
    assert 'data-kind="critical"' in result
    assert "<mark" in result
    assert "title=" not in result


@pytest.mark.unit
def test_render_highlighted_no_title_attribute():
    """The filter must not emit title= (old tooltip pattern)."""
    passages = [{"text": "some phrase", "kind": "neutral", "id": "aaa111222333"}]
    result = _rh("Here is some phrase shown.", passages)
    assert "title=" not in result


@pytest.mark.unit
def test_render_highlighted_claim_chip():
    """When passage_claim_ids maps a passage_id to a claim, the ⚖ chip is emitted."""
    passages = [{"text": "disputed fact", "kind": "contested", "id": "pid111111111"}]
    claim_map = {"pid111111111": 42}
    result = _rh("The disputed fact is here.", passages, claim_map)

    assert "⚖" in result
    assert "claim-42" in result


@pytest.mark.unit
def test_render_highlighted_no_chip_without_map():
    passages = [{"text": "some text", "kind": "neutral", "id": "pid222222222"}]
    result = _rh("Here is some text.", passages)
    assert "⚖" not in result


@pytest.mark.unit
def test_render_highlighted_uses_stable_id_from_passage():
    """Passage id from the dict is used verbatim, not re-computed."""
    passages = [{"text": "phrase here", "kind": "key", "id": "stableid1234"}]
    result = _rh("A phrase here to highlight.", passages)
    assert 'id="p-stableid1234"' in result


@pytest.mark.unit
def test_render_highlighted_fallback_anchor_on_mismatch():
    """When text not found in body, a hidden anchor is injected at the top."""
    passages = [
        {
            "text": "nonexistent passage text XYZ",
            "kind": "neutral",
            "id": "zzz999999999",
        }
    ]
    result = _rh("Body text that does not contain the passage.", passages)
    assert 'id="p-zzz999999999"' in result
    assert "passage-anchor-unmatched" in result


@pytest.mark.unit
def test_render_highlighted_empty_body():
    result = _rh(None)
    assert result == ""

    result = _rh("")
    assert result == ""


@pytest.mark.unit
def test_render_highlighted_multiple_passages():
    passages = [
        {"text": "first key phrase", "kind": "critical", "id": "pid000000001"},
        {"text": "second key phrase", "kind": "neutral", "id": "pid000000002"},
    ]
    result = _rh("Text with first key phrase and second key phrase here.", passages)
    assert 'data-passage-id="pid000000001"' in result
    assert 'data-passage-id="pid000000002"' in result
