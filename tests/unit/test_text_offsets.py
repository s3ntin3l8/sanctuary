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


@pytest.mark.unit
def test_normalized_span_covers_exactly_the_match_after_whitespace_runs():
    # Whitespace before the match used to shift the mapped start earlier.
    content = "Kopf\n\n  Zeile   eins\n\nDer  Beklagte\tschuldet   Unterhalt. Ende"
    text = "Der Beklagte schuldet Unterhalt."
    start, end = find_text_offsets(content, text)
    assert content[start:end] == "Der  Beklagte\tschuldet   Unterhalt."


@pytest.mark.unit
def test_fuzzy_match_never_ends_inside_a_word():
    content = (
        "kanzlei@sanctuary-counsel.de.\n\n"
        "Der Beklagte schuldet gemäß § 1601 BGB rückständigen Unterhalt für den "
        "Zeitraum Januar bis Oktober 2025."
    )
    # Not verbatim (trailing period), so it falls through to the fuzzy pass.
    text = "Der Beklagte schuldet gemäß § 1601 BGB rückständigen Unterhalt."
    start, end = find_text_offsets(content, text)
    assert content[start:].startswith("Der Beklagte")
    assert not content[end - 1].isalnum() or not content[end].isalnum()
    assert "1601" in content[start:end]
    assert content[start:end] == text.rstrip(".")


@pytest.mark.unit
def test_normalized_match_does_not_end_inside_a_longer_word():
    # The double space defeats the exact pass; the normalized pass must not stop at "Unterhalt|s".
    content = "Er schuldet rückständigen  Unterhaltsanspruch seit Januar."
    start, end = find_text_offsets(content, "rückständigen Unterhalt")
    assert content[start:end] == "rückständigen  Unterhaltsanspruch"


@pytest.mark.unit
def test_snapping_does_not_swallow_a_long_alphanumeric_run():
    # A fuzzy window that begins inside a long hash/id must not widen over all of it.
    junk = "a" * 200
    content = f"{junk}\nDer Beklagte schuldet gemäß § 1601 BGB rückständigen Unterhalt für Januar."
    start, end = find_text_offsets(
        content, "Der Beklagte schuldet gemäß § 1601 BGB rückständigen Unterhalt."
    )
    assert end - start < 80
    assert "Der Beklagte" in content[start:end]
