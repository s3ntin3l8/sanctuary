"""Unit tests for find_text_offsets — the three-pass passage locator."""

import pytest

from app.services.text_offsets import find_text_offsets


@pytest.mark.unit
@pytest.mark.parametrize("content,text", [("", "x"), ("x", ""), ("", "")])
def test_empty_inputs_return_none(content, text):
    assert find_text_offsets(content, text) is None


@pytest.mark.unit
def test_exact_unique_match():
    content = "hello world foo"
    assert find_text_offsets(content, "world") == (6, 11)


@pytest.mark.unit
def test_exact_non_unique_falls_through_to_none():
    # "ab" appears twice → pass 1 (must be unique) rejects it; no other pass
    # can confidently place a 2-char string → None.
    assert find_text_offsets("ab cd ab", "ab") is None


@pytest.mark.unit
def test_normalized_collapses_whitespace():
    # Exact fails (double space), normalized pass collapses it and matches at 0.
    start, end = find_text_offsets("hello   world foo", "hello world")
    assert start == 0


@pytest.mark.unit
def test_normalized_folds_curly_quotes():
    content = 'Court said "the claim is denied" today'
    text = "the claim is denied"  # straight quotes vs source
    result = find_text_offsets(content, text)
    assert result is not None
    start, _ = result
    assert content[start : start + len(text)] == text


@pytest.mark.unit
def test_fuzzy_match_for_punctuation_shifted_quote():
    content = "The quick brown fox jumps over the lazy dog every morning"
    # Trailing period defeats exact + normalized; fuzzy (ratio >= 0.85) recovers it.
    text = "The quick brown fox jumps over the lazy dog."
    result = find_text_offsets(content, text)
    assert result is not None
    assert result[0] == 0


@pytest.mark.unit
def test_unrelated_text_returns_none():
    assert find_text_offsets("alpha beta gamma delta epsilon", "xyz123 qrs789") is None
