"""Exhibit labels and their order (Anlage K 3, B 2, 1)."""

import pytest

from app.services.ingestion.exhibits import (
    ListItem,
    assign_exhibits,
    parse_anlage_list,
    parse_filename_label,
    parse_label,
    sort_key,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Anlage K3", "K 3"),
        ("K 3", "K 3"),
        ("k3", "K 3"),
        ("B2", "B 2"),
        ("Anlage 1", "1"),
        ("Anlage K 3a", "K 3a"),
        ("ASt 4", "ASt 4"),
        ("Bf 2", "Bf 2"),
        ("Anlage K 007", "K 7"),
        ("Hallo 3", None),
        ("Klage 5", None),
        ("3", None),  # a bare number is not an exhibit without "Anlage"
        ("AG 3", None),  # a court and a number, not an exhibit
        ("ASG 2", None),
        ("Anlage AG 3", "AG 3"),
        ("Anlage ASG 2", "ASG 2"),
    ],
)
def test_labels_are_normalised(text, label):
    assert parse_label(text) == label


@pytest.mark.parametrize(
    ("filename", "label"),
    [
        ("Anlage_K3_Vertrag.pdf", "K 3"),
        ("Anlage K 10 Rechnung.pdf", "K 10"),
        ("K3 Vertrag.pdf", "K 3"),
        ("B2.pdf", "B 2"),
        ("Anlage 1.pdf", "1"),
        ("SCHR_LG IN V_26_05_26.PDF", None),
        ("AG 3 C 123-26.pdf", None),  # a court and a file number, not an exhibit
        ("Anlage AG 3.pdf", "AG 3"),
        ("Rechnung K1.pdf", None),
        ("scan.pdf", None),
    ],
)
def test_filename_labels(filename, label):
    assert parse_filename_label(filename) == label


def test_a_list_gives_labels_in_the_senders_order():
    note = (
        "Sehr geehrte Damen und Herren,\n"
        "Anlagen:\n"
        "Anlage K1: Mietvertrag vom 01.01.2024\n"
        "- K 2 – Kündigungsschreiben\n"
        "Anlage 3: Schriftverkehr\n"
        "1. Kein Label hier\n"
        "Anlage K1: wiederholt\n"
        "Viele Grüße\n"
    )
    assert parse_anlage_list(note) == [
        ListItem("K 1", "Mietvertrag vom 01.01.2024", 1),
        ListItem("K 2", "Kündigungsschreiben", 2),
        ListItem("3", "Schriftverkehr", 3),
    ]


def test_ag_and_asg_on_a_list_line_need_the_word_anlage():
    assert parse_anlage_list("AG 3: Urteil\nASG 2: Beschluss\n") == []
    assert parse_anlage_list("Anlage AG 3: Urteil\nAnlage ASG 2: Beschluss\n") == [
        ListItem("AG 3", "Urteil", 1),
        ListItem("ASG 2", "Beschluss", 2),
    ]


def test_listed_labels_find_their_files_by_label_then_description():
    items = [
        ListItem("K 1", "Mietvertrag vom 01.01.2024", 1),
        ListItem("K 2", "Kündigungsschreiben", 2),
        ListItem("K 3", "Schriftverkehr", 3),
    ]
    found = assign_exhibits(
        [
            ("0.1", "Anlage_K3_scan.pdf"),  # by its label
            ("0.2", "Mietvertrag 2024.pdf"),  # by its description
            ("0.3", "Kuendigungsschreiben.pdf"),  # too different: stays unset
        ],
        items,
    )
    assert found["0.1"] == {"label": "K 3", "position": 3, "source": "anlage_list"}
    assert found["0.2"] == {"label": "K 1", "position": 1, "source": "anlage_list"}
    assert "0.3" not in found


def test_an_ambiguous_description_is_left_unset():
    items = [ListItem("K 1", "Vertrag vom Mieter", 1)]
    found = assign_exhibits(
        [("0.1", "Vertrag Mieter.pdf"), ("0.2", "Vertrag Mieter 2.pdf")], items
    )
    assert found == {}


def test_two_files_claiming_one_label_are_both_left_unset():
    found = assign_exhibits(
        [("0.1", "Anlage_K1_a.pdf"), ("0.2", "Anlage_K1_b.pdf")],
        [ListItem("K 1", "Vertrag", 1)],
    )
    assert found == {}


def test_filename_labels_alone_give_an_exhibit_without_a_position():
    found = assign_exhibits([("0.1", "B2.pdf"), ("0.2", "notes.pdf")], [])
    assert found == {"0.1": {"label": "B 2", "position": None, "source": "filename"}}


def test_sort_order_lists_first_then_numbers_then_the_rest():
    def meta(label, position=None):
        return {"exhibit": {"label": label, "position": position, "source": "x"}}

    docs = {
        "listed 2": meta("K 9", 2),
        "listed 1": meta("K 7", 1),
        "bare K10": meta("K 10"),
        "bare K2": meta("K 2"),
        "none": None,
    }
    assert sorted(docs, key=lambda k: sort_key(docs[k])) == [
        "listed 1",
        "listed 2",
        "bare K2",
        "bare K10",
        "none",
    ]
