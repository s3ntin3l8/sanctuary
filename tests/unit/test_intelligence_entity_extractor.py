"""Tests for entity_extractor._save_entities validation and dedup logic."""

import pytest

from app.models.database import Entity
from app.services.intelligence.entity_extractor import _save_entities


@pytest.mark.unit
def test_save_entities_valid(db_session, sample_document):
    """Valid entities are persisted."""
    sample_document.case_id = "TEST-001"
    db_session.flush()

    result = {
        "entities": [
            {
                "type": "COURT",
                "name": "Amtsgericht Hamburg",
                "context_quote": "before the court",
            },
            {"type": "PERSON", "name": "Dr. Müller", "context_quote": "represented by"},
        ]
    }
    count = _save_entities(sample_document, result, db_session)

    assert count == 2
    entities = db_session.query(Entity).filter(Entity.case_id == "TEST-001").all()
    assert len(entities) == 2
    names = {e.name for e in entities}
    assert "Amtsgericht Hamburg" in names
    assert "Dr. Müller" in names


@pytest.mark.unit
def test_save_entities_dedup(db_session, sample_document):
    """Same case+type+name is not duplicated."""
    # `sample_document` is on `sample_case` (id="TEST-001"); use that for FK validity.
    case_id = sample_document.case_id

    result = {
        "entities": [
            {"type": "COURT", "name": "Amtsgericht Hamburg", "context_quote": "text"},
        ]
    }
    _save_entities(sample_document, result, db_session)
    count2 = _save_entities(sample_document, result, db_session)  # second call
    assert count2 == 0

    total = db_session.query(Entity).filter(Entity.case_id == case_id).count()
    assert total == 1


@pytest.mark.unit
def test_save_entities_collapses_name_variants(db_session, sample_document):
    """Diacritic / honorific / order variants of the same name produce one Entity."""
    case_id = sample_document.case_id

    result = {
        "entities": [
            {"type": "PERSON", "name": "Björn Hansen", "context_quote": "v1"},
            {"type": "PERSON", "name": "Bjoern Hansen", "context_quote": "v2"},
            {"type": "PERSON", "name": "Hansen, Björn", "context_quote": "v3"},
            {"type": "PERSON", "name": "Herr Björn Hansen", "context_quote": "v4"},
        ]
    }
    count = _save_entities(sample_document, result, db_session)
    assert count == 1

    rows = db_session.query(Entity).filter(Entity.case_id == case_id).all()
    assert len(rows) == 1
    # The first variant's original spelling is preserved.
    assert rows[0].name == "Björn Hansen"


@pytest.mark.unit
def test_save_entities_court_diacritic_variants_dedup(db_session, sample_document):
    """'Amtsgericht Köln' and 'Amtsgericht Koeln' collapse to one row."""
    case_id = sample_document.case_id

    result = {
        "entities": [
            {"type": "COURT", "name": "Amtsgericht Köln", "context_quote": ""},
            {"type": "COURT", "name": "Amtsgericht Koeln", "context_quote": ""},
        ]
    }
    count = _save_entities(sample_document, result, db_session)
    assert count == 1
    assert (
        db_session.query(Entity)
        .filter(Entity.case_id == case_id, Entity.type == "COURT")
        .count()
        == 1
    )


@pytest.mark.unit
def test_save_entities_unknown_type_skipped(db_session, sample_document):
    """Entities with invalid type strings are silently skipped."""
    result = {
        "entities": [
            {"type": "INVALID_TYPE", "name": "Something", "context_quote": ""},
            {"type": "COURT", "name": "Valid Court", "context_quote": ""},
        ]
    }
    count = _save_entities(sample_document, result, db_session)
    assert count == 1


@pytest.mark.unit
def test_save_entities_empty_name_skipped(db_session, sample_document):
    """Entities with empty names are silently skipped."""
    result = {
        "entities": [
            {"type": "PERSON", "name": "", "context_quote": ""},
            {"type": "PERSON", "name": "Valid Name", "context_quote": ""},
        ]
    }
    count = _save_entities(sample_document, result, db_session)
    assert count == 1


@pytest.mark.unit
def test_save_entities_no_entities(db_session, sample_document):
    """Empty list returns 0."""
    count = _save_entities(sample_document, {"entities": []}, db_session)
    assert count == 0


# ---------------------------------------------------------------------------
# Issue #6: entity quality — court-type override, case-title rejection,
# party-name canonicalization snap.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_save_entities_overrides_court_misclassified_as_law_firm(
    db_session, sample_document
):
    """Regression for entity 95: AI emitted `LAW_FIRM Amtsgericht Ingolstadt`.
    A court name MUST be stored as COURT regardless of the AI's type call."""
    result = {
        "entities": [
            {
                "type": "LAW_FIRM",
                "name": "Amtsgericht Ingolstadt",
                "context_quote": "x",
            },
        ]
    }
    _save_entities(sample_document, result, db_session)
    rows = (
        db_session.query(Entity).filter(Entity.case_id == sample_document.case_id).all()
    )
    assert len(rows) == 1
    assert rows[0].type.name == "COURT"
    assert rows[0].name == "Amtsgericht Ingolstadt"


@pytest.mark.unit
def test_save_entities_overrides_court_misclassified_as_organization(
    db_session, sample_document
):
    """Same override logic for ORGANIZATION → COURT (e.g. 'Landgericht …')."""
    result = {
        "entities": [
            {
                "type": "ORGANIZATION",
                "name": "Landgericht Ingolstadt",
                "context_quote": "x",
            },
        ]
    }
    _save_entities(sample_document, result, db_session)
    rows = (
        db_session.query(Entity).filter(Entity.case_id == sample_document.case_id).all()
    )
    assert [r.type.name for r in rows] == ["COURT"]


@pytest.mark.unit
def test_save_entities_drops_case_title_stored_as_person(db_session, sample_document):
    """Regression: 'Hansen, Björn /. Liu, Yingying' was stored as a PERSON
    entity — that's a Rubrum string, not a person."""
    result = {
        "entities": [
            {
                "type": "PERSON",
                "name": "Hansen, Björn /. Liu, Yingying",
                "context_quote": "x",
            },
            # Other variants the regex must also reject:
            {"type": "PERSON", "name": "Müller v. Schmidt", "context_quote": "x"},
            {"type": "PERSON", "name": "Kläger gegen Beklagte", "context_quote": "x"},
            # ...and a real person who must NOT be dropped:
            {"type": "PERSON", "name": "Yingying Liu", "context_quote": "x"},
        ]
    }
    _save_entities(sample_document, result, db_session)
    rows = (
        db_session.query(Entity).filter(Entity.case_id == sample_document.case_id).all()
    )
    assert len(rows) == 1
    assert rows[0].name == "Yingying Liu"


@pytest.mark.unit
def test_save_entities_snaps_variants_to_known_party_canonical_name(
    db_session, sample_document
):
    """When a Case has `parties=[{"name": "Liu Yingying", …}]`, all variants
    that normalize to the same dedup key as 'Liu Yingying' (Liu, Yingying Liu,
    Liu Yingying, J. Liu Yingying, etc.) should snap to that canonical
    spelling — the row stores 'Liu Yingying', not the variant."""
    from app.models.database import Case

    case = db_session.query(Case).filter(Case.id == sample_document.case_id).one()
    case.parties = [
        {"name": "Liu Yingying", "role": "opposing", "document_count": 5},
        {"name": "Björn Hansen", "role": "own", "document_count": 0},
    ]
    db_session.flush()

    # Different orderings / forms all referring to the same person.
    result = {
        "entities": [
            {"type": "PERSON", "name": "Yingying Liu", "context_quote": "x"},
        ]
    }
    _save_entities(sample_document, result, db_session)

    rows = (
        db_session.query(Entity)
        .filter(
            Entity.case_id == sample_document.case_id,
            Entity.type.in_(
                [
                    __import__(
                        "app.models.enums", fromlist=["EntityType"]
                    ).EntityType.PERSON
                ]
            ),
        )
        .all()
    )
    # Exactly one row, with the canonical name from Case.parties.
    assert len(rows) == 1
    assert rows[0].name == "Liu Yingying", (
        f"expected canonical spelling 'Liu Yingying', got {rows[0].name!r}"
    )


@pytest.mark.unit
def test_save_entities_snap_dedupes_against_existing_canonical_row(
    db_session, sample_document
):
    """Round 6 regression: when Case.parties has "Liu Yingying" and an
    Entity row exists with name="Liu Yingying", a NEW extraction emitting
    "Yingying Liu" (Western order) must be snapped AND deduped — no
    second row.

    Pre-fix: the dedup key was computed from the AI-emitted raw name
    ("yingying liu"), which differed from the existing row's normalize key
    ("liu yingying"). The snap-to-canonical wrote the correct stored_name
    but inserted a duplicate row. Post-fix: dedup key uses the snapped
    name, so existing canonical rows are matched."""
    from app.models.database import Case
    from app.models.enums import EntityType as ET

    case = db_session.query(Case).filter(Case.id == sample_document.case_id).one()
    case.parties = [
        {"name": "Liu Yingying", "role": "opposing", "document_count": 5},
    ]
    db_session.flush()

    # Seed an existing canonical row (e.g. from a prior doc's extraction).
    db_session.add(
        Entity(
            case_id=sample_document.case_id,
            type=ET.PERSON,
            name="Liu Yingying",
            source_document_id=sample_document.id,
        )
    )
    db_session.flush()

    # New extraction emits the Western-order variant.
    result = {
        "entities": [
            {"type": "PERSON", "name": "Yingying Liu", "context_quote": "x"},
        ]
    }
    count = _save_entities(sample_document, result, db_session)

    assert count == 0, "snap must dedupe against the existing canonical row"
    rows = (
        db_session.query(Entity)
        .filter(
            Entity.case_id == sample_document.case_id,
            Entity.type == ET.PERSON,
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].name == "Liu Yingying"


@pytest.mark.parametrize(
    "variant",
    [
        "Y. Liu",  # initial Western order
        "Liu, Y.",  # initial East-Asian / comma form
        "L. Yingying",  # initial swapped
    ],
)
@pytest.mark.unit
def test_save_entities_snap_initial_form_to_canonical(
    db_session, sample_document, variant
):
    """Round 7: when Case.parties has 'Liu Yingying', initial-form variants
    of the same person should snap to the canonical spelling and dedupe
    against any existing canonical row. Closes the post-R6 doc 25
    ('Liu, Y.') / similar duplicates."""
    from app.models.database import Case
    from app.models.enums import EntityType as ET

    case = db_session.query(Case).filter(Case.id == sample_document.case_id).one()
    case.parties = [
        {"name": "Liu Yingying", "role": "opposing", "document_count": 5},
    ]
    db_session.flush()

    # Seed the canonical row (as if a prior doc had created it).
    db_session.add(
        Entity(
            case_id=sample_document.case_id,
            type=ET.PERSON,
            name="Liu Yingying",
            source_document_id=sample_document.id,
        )
    )
    db_session.flush()

    result = {
        "entities": [
            {"type": "PERSON", "name": variant, "context_quote": "x"},
        ]
    }
    count = _save_entities(sample_document, result, db_session)
    rows = (
        db_session.query(Entity)
        .filter(
            Entity.case_id == sample_document.case_id,
            Entity.type == ET.PERSON,
        )
        .all()
    )
    assert count == 0, f"variant {variant!r} should dedupe against canonical"
    assert len(rows) == 1
    assert rows[0].name == "Liu Yingying"


@pytest.mark.unit
def test_save_entities_does_not_snap_bare_surname(db_session, sample_document):
    """Defense: a bare-surname reference ('Liu' alone, or 'Frau Liu' that
    normalizes to just 'liu' after honorific strip) MUST NOT snap to the
    canonical party. Multiple Lius might be on a case (we have 'Liu Yingying'
    and 'Liu Jun' in case 8441-25); snapping would falsely merge them."""
    from app.models.database import Case
    from app.models.enums import EntityType as ET

    case = db_session.query(Case).filter(Case.id == sample_document.case_id).one()
    case.parties = [
        {"name": "Liu Yingying", "role": "opposing", "document_count": 5},
    ]
    db_session.flush()

    result = {
        "entities": [
            {"type": "PERSON", "name": "Frau Liu", "context_quote": "x"},
        ]
    }
    _save_entities(sample_document, result, db_session)
    rows = (
        db_session.query(Entity)
        .filter(
            Entity.case_id == sample_document.case_id,
            Entity.type == ET.PERSON,
        )
        .all()
    )
    assert len(rows) == 1
    # Frau Liu stored as-is — no false merge into "Liu Yingying"
    assert rows[0].name == "Frau Liu"
