import pytest

from app.helpers import format_eur


@pytest.mark.unit
def test_format_eur():
    assert format_eur(1234.56) == "€\u00a01.234,56"
    assert format_eur(0) == "€\u00a00,00"
    assert format_eur(None) == "—"
