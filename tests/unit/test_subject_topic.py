import pytest

from app.services.ingestion.extractors import extract_subject_topic


@pytest.mark.parametrize(
    ("subject", "topic"),
    [
        (
            "9418/26 - Hansen ./. Liu - wg. elterl. Sorge - 003 F 1221/26",
            "Hansen ./. Liu - wg. elterl. Sorge",
        ),
        ("AW: WG: 8372/25 Vogt", "Vogt"),
        ("Ladung 003 F 426/25", "Ladung"),
        ("9001/24", None),
        ("Re: 3 F 829/26", None),
        ("Rechnung", "Rechnung"),
    ],
)
def test_subject_topic(subject, topic):
    assert extract_subject_topic(subject) == topic
