"""Pin: extract_cost_candidates handles German prefix-EUR formatting.

German RVG/GKG documents commonly write currency *before* the amount:
    "EUR 583,40 zzgl. MwSt."
    "€ 1.234,56"
The previous regex only matched suffix currency ("583,40 EUR") and silently
dropped prefix-currency amounts from the cost-candidates list — exactly the
data the cost extractor exists to capture.
"""

import pytest

from app.services.ingestion.service import extract_cost_candidates


@pytest.mark.unit
def test_suffix_eur_still_extracted():
    """Don't regress the existing format."""
    candidates = extract_cost_candidates("Verfahrensgebühr beträgt 583,40 EUR")
    amounts = [c["value"] for c in candidates if c["type"] == "amount"]
    assert 583.40 in amounts


@pytest.mark.unit
def test_prefix_eur_extracted():
    """`EUR 583,40` (German lawyer-letter style) is extracted."""
    candidates = extract_cost_candidates("Honorar: EUR 583,40 zzgl. MwSt.")
    amounts = [c["value"] for c in candidates if c["type"] == "amount"]
    assert 583.40 in amounts, f"Expected 583.40 in extracted amounts, got: {amounts}"


@pytest.mark.unit
def test_prefix_euro_symbol_extracted():
    """`€ 1.234,56` (symbol prefix with German thousands sep)."""
    candidates = extract_cost_candidates("Streitwert: € 1.234,56")
    amounts = [c["value"] for c in candidates if c["type"] == "amount"]
    assert 1234.56 in amounts


@pytest.mark.unit
def test_both_prefix_and_suffix_in_same_doc():
    """Prefix and suffix amounts in the same document both extract."""
    candidates = extract_cost_candidates(
        "Rechnung: EUR 583,40 für Termin\nTotal: 1.000,00 EUR"
    )
    amounts = sorted({c["value"] for c in candidates if c["type"] == "amount"})
    assert 583.40 in amounts
    assert 1000.0 in amounts
