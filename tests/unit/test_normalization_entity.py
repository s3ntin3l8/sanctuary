"""Unit tests for normalize_entity_name."""

import pytest

from app.models.enums import EntityType
from app.services.normalization import normalize_entity_name


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Björn Hansen", "bjorn hansen"),
        ("Bjoern Hansen", "bjorn hansen"),
        ("Hansen, Björn", "bjorn hansen"),
        ("Herr Björn Hansen", "bjorn hansen"),
        ("Dr. Björn Hansen", "bjorn hansen"),
        ("Yingying Liu", "yingying liu"),
        ("Liu, Yingying", "yingying liu"),
        # Ambiguous order without a comma is kept as-is — no fuzzy matching.
        ("Liu Yingying", "liu yingying"),
    ],
)
def test_normalize_person_collapses_variants(raw, expected):
    assert normalize_entity_name(raw, EntityType.PERSON) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Amtsgericht Ingolstadt", "amtsgericht ingolstadt"),
        ("amtsgericht  ingolstadt ", "amtsgericht ingolstadt"),
        ("Amtsgericht Köln", "amtsgericht koln"),
    ],
)
def test_normalize_court_strips_diacritics_and_spaces(raw, expected):
    assert normalize_entity_name(raw, EntityType.COURT) == expected


@pytest.mark.unit
def test_normalize_org_collapses_sub_unit_when_known():
    """Sub-unit collapse mirrors _normalize_originator: parent must be canonical."""
    canonical = {"landratsamt eichstatt"}
    assert (
        normalize_entity_name(
            "Landratsamt Eichstätt, Amt für Familie",
            EntityType.ORGANIZATION,
            canonical_names=canonical,
        )
        == "landratsamt eichstatt"
    )


@pytest.mark.unit
def test_normalize_empty_returns_empty_string():
    assert normalize_entity_name("", EntityType.PERSON) == ""
    assert normalize_entity_name("   ", EntityType.PERSON) == ""
