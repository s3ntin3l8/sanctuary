"""Second-opinion OCR cross-check: which chandra words the scan does not back up."""

import pytest

from app.services.ingestion import ocr_crosscheck as oc

# Synthetic stand-ins: a faint-scan "second reading" that garbles words, and a
# chandra text that is either faithful to it or fluently invents words.
_SECOND = (
    "Amtsgerlcht Ingolstadt Schloßgasse 1 e 85120 Hepberg Schonung fur 2-3 Tage "
    "Wärmeanwendungen im LWS-Bereich Vorstellung beim Hausarzt"
)


def test_faithful_text_has_no_unsupported_words():
    chandra = (
        "Amtsgericht Ingolstadt Schloßgasse 1 e, 85120 Hepberg\n\n"
        "Schonung für 2-3 Tage, Wärmeanwendungen im LWS-Bereich, "
        "Vorstellung beim Hausarzt"
    )
    result = oc.check_page(chandra, _SECOND)
    assert result["unsupported"] == []
    assert not oc.is_unverified(result)


def test_garbled_second_reading_still_supports_close_spellings():
    # "Amtsgerlcht" vs "Amtsgericht", "fur" vs "für": the detector garbles.
    assert oc.check_page("Amtsgericht Ingolstadt", _SECOND)["unsupported"] == []


def test_words_the_second_reading_has_no_trace_of_are_reported():
    chandra = (
        "Wohnplatz: Nikotinkonsum Muniba Kardiologie\n\n"
        "Schonung, 2-3 Tage, Wärmeanwendungen im LWS-Bereich"
    )
    result = oc.check_page(chandra, _SECOND)
    assert set(result["unsupported"]) == {
        "wohnplatz",
        "nikotinkonsum",
        "muniba",
        "kardiologie",
    }
    assert oc.is_unverified(result)


def test_a_split_or_merged_word_counts_as_supported():
    assert oc.check_page("Wärmeanwendungen", "Wärme anwendungen")["unsupported"] == []


def test_image_descriptions_are_not_checked():
    chandra = (
        "Hausarzt Vorstellung\n\n"
        "![Official stamp of the court]()An official rectangular stamp. "
        "The text inside the stamp reads court at the top.\n\nWärmeanwendungen"
    )
    assert oc.check_page(chandra, _SECOND)["unsupported"] == []


def test_a_page_the_second_engine_reads_as_empty_is_unverified():
    chandra = "Das Gericht möge die bestehende Regelung der elterlichen Sorge ändern"
    result = oc.check_page(chandra, "")
    assert result["ratio"] == 1.0
    assert oc.is_unverified(result)


@pytest.mark.parametrize(
    ("count", "ratio", "expected"),
    [(3, 0.5, False), (4, 0.04, False), (4, 0.05, True), (20, 0.5, True)],
)
def test_unverified_needs_enough_words_and_enough_share(count, ratio, expected):
    assert oc.is_unverified({"count": count, "ratio": ratio}) is expected


def test_stored_words_are_capped_but_counted():
    chandra = " ".join(f"wortzahl{i:02d}x" for i in range(40))
    result = oc.check_page(chandra, "nichts")
    assert len(result["unsupported"]) == 20
    assert result["count"] == 40


def test_second_opinion_failure_is_none_not_an_exception(monkeypatch):
    def boom(_img):
        raise RuntimeError("engine gone")

    monkeypatch.setattr(oc, "read_text", boom)
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    assert oc.second_opinion(buf.getvalue()) is None
    assert oc.second_opinion(b"not a png") is None
