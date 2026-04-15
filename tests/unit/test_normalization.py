import pytest

from app.services.normalization import normalize_hm


@pytest.mark.unit
def test_normalize_hm_basic():
    assert normalize_hm("h&m") == "H&M"
    assert normalize_hm("H&M") == "H&M"
    assert normalize_hm("h & m") == "H&M"


@pytest.mark.unit
def test_normalize_hm_variants():
    assert normalize_hm("h and m") == "H&M"
    assert normalize_hm("H AND M") == "H&M"
    assert normalize_hm("H  and  M") == "H&M"


@pytest.mark.unit
def test_normalize_hm_in_sentence():
    text = "The package from h and m arrived today at h&m store."
    assert normalize_hm(text) == "The package from H&M arrived today at H&M store."


@pytest.mark.unit
def test_normalize_hm_no_match():
    assert normalize_hm("ham") == "ham"
    assert normalize_hm("handm") == "handm"
    assert normalize_hm("h and make") == "h and make"
