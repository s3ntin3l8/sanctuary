"""A reply or forward keeps the lawyer's internal reference."""

import pytest

from app.services.case_service import _derive_case_title_from_subject
from app.services.ingestion.extractors import extract_internal_id_from_subject


@pytest.mark.unit
@pytest.mark.parametrize(
    "subject",
    [
        "8372/25 - Hansen ./. Liu",
        "AW: 8372/25 - Hansen ./. Liu",
        "WG: AW: 8372/25 - 003 F 426/25",
        "Fwd: RE: 8372/25",
    ],
)
def test_internal_id_survives_reply_prefixes(subject):
    assert extract_internal_id_from_subject(subject) == "8372-25"


@pytest.mark.unit
def test_internal_id_still_requires_leading_position():
    assert extract_internal_id_from_subject("Termin 8372/25") is None


@pytest.mark.unit
def test_case_title_drops_reply_prefix():
    assert (
        _derive_case_title_from_subject("AW: 8372/25 - Hansen ./. Liu", "8372/25")
        == "Hansen ./. Liu"
    )
