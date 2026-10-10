import pytest

from app.services.intelligence.prompts import CLAIM_EXTRACTOR_SYSTEM
from app.services.user_settings_service import get_party_identity, set_party_identity


@pytest.mark.unit
def test_multiline_own_parties_become_one_entry_per_name(db_session):
    # The blob that cost rulings could never match against a name in a document
    set_party_identity(
        {
            "own_self": " Björn Hansen ",
            "own_parties": ["Haidl Funk\nHr. Funk\r\nAndreas Funk", "  ", "Kanzlei X"],
        },
        db_session,
    )
    db_session.commit()
    identity = get_party_identity(db_session)
    assert identity["own_self"] == "Björn Hansen"
    assert identity["own_parties"] == [
        "Haidl Funk",
        "Hr. Funk",
        "Andreas Funk",
        "Kanzlei X",
    ]


@pytest.mark.unit
def test_claim_prompt_pins_claim_text_to_english_and_excerpts_to_the_source():
    assert "Write every `claim_text` in English" in CLAIM_EXTRACTOR_SYSTEM
    assert "copy it verbatim" in CLAIM_EXTRACTOR_SYSTEM
