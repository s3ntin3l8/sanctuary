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


@pytest.mark.unit
def test_render_highlighted_survives_smart_quotes():
    """Markdown typographer rewrites "..." to curly quotes; the mark must
    still wrap the original text. Pre-fix this failed because the regex ran
    against rendered HTML where the quote chars had already been replaced."""
    body = 'The court held: "the appeal is granted in full."'
    passages = [
        {
            "text": '"the appeal is granted in full."',
            "kind": "ruling",
            "id": "smartq111111",
        }
    ]
    result = _rh(body, passages)
    assert 'data-passage-id="smartq111111"' in result
    # Markdown escaped the inner quotes to &quot;, but the <mark> still wraps
    # them — pre-fix the regex would have failed on the substituted chars.
    assert "&quot;the appeal is granted in full.&quot;</mark>" in result
    assert "passage-anchor-unmatched" not in result


@pytest.mark.unit
def test_render_highlighted_survives_inline_formatting():
    """Bold/italic markdown inside a passage should not break the highlight.
    Pre-fix the regex couldn't match across <strong>/<em> tags."""
    body = "The court ruled that **the defendant** is liable."
    passages = [
        {
            "text": "The court ruled that **the defendant** is liable.",
            "kind": "ruling",
            "id": "inlinefmt001",
        }
    ]
    result = _rh(body, passages)
    assert 'data-passage-id="inlinefmt001"' in result
    assert "<strong>the defendant</strong>" in result
    assert "passage-anchor-unmatched" not in result


@pytest.mark.unit
def test_render_highlighted_uses_offsets_when_present():
    """When offsets are stamped, they're authoritative — even when the same
    substring appears multiple times the offset selects the correct one."""
    body = "Apple. Banana. Apple. Cherry."
    passages = [
        {
            "text": "Apple",
            "kind": "neutral",
            "id": "applepidaaaa",
            "start_offset": 14,  # the second "Apple"
            "end_offset": 19,
        }
    ]
    result = _rh(body, passages)
    # First "Apple" stays unwrapped; second one gets the mark.
    assert result.count("<mark") == 1
    # The mark must come after "Banana." in the rendered output.
    banana_idx = result.find("Banana")
    mark_idx = result.find("<mark")
    assert banana_idx < mark_idx


@pytest.mark.unit
def test_render_highlighted_claim_excerpt_via_live_offset():
    """Claim excerpts (no stored offsets) are located at render time via the
    same find_text_offsets cascade — not regex against rendered HTML."""
    from app.main import render_highlighted

    body = 'The defendant said: "we deny all charges."'
    claim_map = {7: '"we deny all charges."'}
    rendered = str(render_highlighted(body, None, None, claim_map))
    assert 'id="claim-7"' in rendered
    assert "claim-anchor-unmatched" not in rendered


@pytest.mark.unit
def test_render_highlighted_offsets_with_paragraph_break():
    """Passage text that spans a paragraph break is anchored on the first line
    (fully marked through to the </p> boundary). The id is present so spine
    clicks resolve."""
    body = "First line of the passage continues here.\n\nAnd this is the second line."
    passages = [
        {
            "text": "First line of the passage continues here.\n\nAnd this is the second line.",
            "kind": "neutral",
            "id": "multipar0001",
            "start_offset": 0,
            "end_offset": len(body),
        }
    ]
    result = _rh(body, passages)
    assert 'id="p-multipar0001"' in result
    assert "passage-anchor-unmatched" not in result
    # Both paragraphs are present (paragraph wrapping survived).
    assert "<p>" in result
    assert result.count("<p>") >= 2
