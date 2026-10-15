"""A document longer than one LLM call can take is read in windows, never sampled."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.ai_summary import content_windows
from app.services.intelligence.schemas import (
    ClaimExtraction,
    DocumentEnrichment,
    EntityList,
    SummaryReduction,
)

pytestmark = pytest.mark.unit

LIMIT = 1_000


def _doc(content: str, **kw):
    """A stand-in with the attributes the windowing and the AI stages read."""
    base = {
        "id": 7,
        "content": content,
        "title": "Schriftsatz",
        "originator_type": None,
        "ai_summary": {"legal_significance": "Antrag auf Sorgerecht"},
        "key_passages": None,
        "ingest_batch_id": None,
        "case_id": None,
        "role": None,
        "attributed_originator": None,
        "received_date": None,
        "sender": None,
        "issued_date": None,
    }
    return SimpleNamespace(**{**base, **kw})


def _pages(n: int, size: int = 400) -> str:
    return "\n\n".join(
        f"--- PAGE {p} ---\n\n" + (f"Seite {p}. " * 80)[:size] for p in range(1, n + 1)
    )


# --- windows -------------------------------------------------------------------


def test_a_document_that_fits_is_one_window():
    windows = content_windows(_doc(_pages(2)), max_chars=LIMIT * 5)

    assert len(windows) == 1
    only = windows[0]
    assert (only.index, only.total, only.first_page, only.last_page) == (1, 1, 1, 2)
    assert only.note("Extract this part only.") == ""


def test_a_long_document_is_cut_at_page_boundaries_without_losing_text():
    doc = _doc(_pages(12))

    windows = content_windows(doc, max_chars=LIMIT)

    assert "".join(w.text for w in windows) == doc.content
    assert all(len(w.text) <= LIMIT for w in windows)
    # The fewest windows, filled evenly: no tiny tail.
    assert len(windows) == -(-len(doc.content) // LIMIT)
    assert all(w.text.startswith("--- PAGE ") for w in windows)
    firsts = [w.first_page for w in windows]
    assert firsts == sorted(firsts) and windows[-1].last_page == 12
    assert [w.index for w in windows] == list(range(1, len(windows) + 1))
    assert {w.total for w in windows} == {len(windows)}


def test_a_single_page_larger_than_a_window_is_split_inside_the_page():
    big = "--- PAGE 1 ---\n\n" + "\n".join(f"Zeile {i}" for i in range(400))
    doc = _doc(big)

    windows = content_windows(doc, max_chars=LIMIT)

    assert len(windows) > 1
    assert "".join(w.text for w in windows) == big
    assert all(len(w.text) <= LIMIT for w in windows)


def test_text_without_page_markers_is_cut_at_blank_lines():
    doc = _doc("\n\n".join(f"Absatz {i}. " + "x" * 200 for i in range(20)))

    windows = content_windows(doc, max_chars=LIMIT)

    assert len(windows) > 1
    assert "".join(w.text for w in windows) == doc.content
    assert all(w.text.endswith("\n\n") or w is windows[-1] for w in windows)
    assert all(w.first_page is None for w in windows)


def test_a_huge_document_is_read_up_to_the_window_cap():
    doc = _doc(_pages(60))

    windows = content_windows(doc, max_chars=LIMIT, max_windows=3)

    assert len(windows) == 3 and {w.total for w in windows} == {3}
    assert doc.content.startswith("".join(w.text for w in windows))
    assert len("".join(w.text for w in windows)) < len(doc.content)


def test_a_part_tells_the_model_which_part_it_reads():
    windows = content_windows(_doc(_pages(12)), max_chars=LIMIT)

    note = windows[1].note("Extract this part only.")

    assert note.startswith(f"This is PART 2 of {len(windows)} of a longer document")
    assert f"pages {windows[1].first_page}-{windows[1].last_page}" in note


# --- claims ---------------------------------------------------------------------


def _claim(text: str) -> dict:
    return {"claim_text": text, "claim_type": "factual", "excerpt": None}


def test_claims_are_extracted_from_every_window_and_not_repeated():
    from app.services.intelligence import claim_extractor

    doc = _doc(_pages(12))
    prompts: list[str] = []
    answers = iter(
        [
            ClaimExtraction(
                new_claims=[_claim("Die Antragstellerin wohnt in Hepberg.")]
            ),
            ClaimExtraction(
                new_claims=[
                    _claim("Die Antragstellerin wohnt in Hepberg."),  # repeated
                    _claim("Der Antragsgegner hat Unterhalt vollstreckt."),
                ]
            ),
            ClaimExtraction(
                new_claims=[_claim("Das Strafverfahren wurde eingestellt.")]
            ),
        ]
    )

    def fake(**kwargs):
        prompts.append(kwargs["user_prompt"])
        return next(answers)

    with (
        patch.object(claim_extractor, "content_windows") as windows,
        patch.object(claim_extractor, "call_json_ai", side_effect=fake),
    ):
        windows.return_value = content_windows(doc, max_chars=len(doc.content) // 3 + 1)
        assert len(windows.return_value) == 3
        merged = claim_extractor._call_claim_extractor_sync(doc, [])

    assert [c["claim_text"] for c in merged["new_claims"]] == [
        "Die Antragstellerin wohnt in Hepberg.",
        "Der Antragsgegner hat Unterhalt vollstreckt.",
        "Das Strafverfahren wurde eingestellt.",
    ]
    assert "PART 1 of 3" in prompts[0] and "ALREADY EXTRACTED" not in prompts[0]
    assert "PART 3 of 3" in prompts[2]
    assert (
        "Die Antragstellerin wohnt in Hepberg." in prompts[1]
    )  # earlier parts' claims
    assert "ALREADY EXTRACTED" in prompts[2]


def test_a_short_document_reads_exactly_as_before():
    from app.services.intelligence import claim_extractor

    doc = _doc("Kurzer Text.")
    seen: list[str] = []

    def fake(**kwargs):
        seen.append(kwargs["user_prompt"])
        return ClaimExtraction()

    with patch.object(claim_extractor, "call_json_ai", side_effect=fake):
        claim_extractor._call_claim_extractor_sync(doc, [])

    assert len(seen) == 1
    assert "PART" not in seen[0] and "ALREADY EXTRACTED" not in seen[0]
    assert "CONTENT:\n<document>\nKurzer Text.\n</document>" in seen[0]


# --- entities -------------------------------------------------------------------


def test_entities_are_collected_from_every_window_without_duplicates():
    from app.services.intelligence import entity_extractor

    doc = _doc(_pages(12))
    answers = iter(
        [
            EntityList(entities=[{"type": "court", "name": "Amtsgericht Ingolstadt"}]),
            EntityList(
                entities=[
                    {"type": "court", "name": "Amtsgericht Ingolstadt"},
                    {"type": "person", "name": "Yingying Liu"},
                ]
            ),
        ]
    )
    with (
        patch.object(entity_extractor, "content_windows") as windows,
        patch.object(
            entity_extractor, "call_json_ai", side_effect=lambda **_: next(answers)
        ),
    ):
        windows.return_value = content_windows(doc, max_chars=len(doc.content) // 2 + 1)
        merged = entity_extractor._call_entity_extractor_sync(doc)

    assert [e["name"] for e in merged["entities"]] == [
        "Amtsgericht Ingolstadt",
        "Yingying Liu",
    ]


# --- enrichment -----------------------------------------------------------------


def _passage(text: str) -> dict:
    return {"text": text, "rationale": None}


def _item(title: str, due: str) -> dict:
    return {"title": title, "action_type": "deadline", "due_date": due}


def _enrich(doc, answers, reduce=None):
    from app.services.intelligence import document_enricher

    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        if kwargs["schema"] is SummaryReduction:
            if isinstance(reduce, Exception):
                raise reduce
            return reduce
        return answers[len([c for c in calls if c["schema"] is DocumentEnrichment]) - 1]

    with (
        patch.object(document_enricher, "content_windows") as windows,
        patch.object(document_enricher, "call_json_ai", side_effect=fake),
    ):
        windows.return_value = content_windows(doc, max_chars=len(doc.content) // 3 + 1)
        result = document_enricher._call_enricher_sync(doc)
    return result, calls


def _three_parts():
    return [
        DocumentEnrichment(
            title="Antragsschrift Sorgerecht",
            significance_tier="significant",
            document_type="motion",
            key_passages=[_passage("Der Antrag wird gestellt.")],
            action_items=[_item("Stellungnahme", "2026-09-15")],
            management_summary={"legal_significance": "Part one"},
        ),
        DocumentEnrichment(
            title="Etwas anderes",
            significance_tier="critical",  # only the first part decides the tier
            document_type="ruling",
            key_passages=[
                _passage("Der Antrag wird gestellt."),  # repeated
                _passage("Frau Liu trägt eine Gefährdung vor."),
            ],
            action_items=[
                _item("Stellungnahme", "2026-09-15"),
                _item("Zahlen", "2026-10-01"),
            ],
            cost_delta={"kind": "streitwert", "amount": 3000.0, "direction": "none"},
        ),
        DocumentEnrichment(key_passages=[_passage("Beweis: Zeugnis.")]),
    ]


def test_a_long_document_is_enriched_from_every_part():
    doc = _doc(_pages(12))
    reduced = SummaryReduction(
        legal_significance="Frau Liu beantragt den Entzug der Sorge.",
        required_action="Stellungnahme bis 15.09.2026.",
    )

    result, calls = _enrich(doc, _three_parts(), reduce=reduced)

    # Title, type and tier stand as the document opens; the rest is collected.
    assert result["title"] == "Antragsschrift Sorgerecht"
    assert (
        result["document_type"] == "motion"
        and result["significance_tier"] == "significant"
    )
    assert [p["text"] for p in result["key_passages"]] == [
        "Der Antrag wird gestellt.",
        "Frau Liu trägt eine Gefährdung vor.",
        "Beweis: Zeugnis.",
    ]
    assert [(i["title"], i["due_date"]) for i in result["action_items"]] == [
        ("Stellungnahme", "2026-09-15"),
        ("Zahlen", "2026-10-01"),
    ]
    assert result["cost_delta"]["amount"] == 3000.0
    assert result["management_summary"]["legal_significance"].startswith("Frau Liu")
    assert [c["schema"] for c in calls].count(DocumentEnrichment) == 3
    reduce_prompt = next(c for c in calls if c["schema"] is SummaryReduction)[
        "user_prompt"
    ]
    assert "PART 1 of 3" in reduce_prompt and "Part one" in reduce_prompt


def test_the_first_summary_stands_when_the_reduction_fails():
    doc = _doc(_pages(12))

    result, _calls = _enrich(doc, _three_parts(), reduce=RuntimeError("model down"))

    assert result["management_summary"]["legal_significance"] == "Part one"


def test_a_short_document_is_enriched_in_one_call():
    from app.services.intelligence import document_enricher

    doc = _doc("Kurzer Beschluss.")
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return DocumentEnrichment(title="Beschluss")

    with patch.object(document_enricher, "call_json_ai", side_effect=fake):
        result = document_enricher._call_enricher_sync(doc)

    assert len(calls) == 1 and result["title"] == "Beschluss"
    assert "PART" not in calls[0]["user_prompt"]


def test_the_document_records_that_it_was_read_in_parts(db_session, sample_document):
    from app.services.intelligence.document_enricher import _apply_enrichment

    sample_document.content = _pages(200, size=900)

    _apply_enrichment(sample_document, {"title": "Schriftsatz"}, db_session)

    meta = sample_document.meta
    assert meta["ai_context_strategy"] == "chunked"
    assert meta["ai_context_windows"] == len(content_windows(sample_document))
    assert meta["ai_context_chars"] == len(sample_document.content)
    assert meta["ai_context_truncated"] is False


def test_a_document_cut_short_by_the_window_cap_says_so(db_session, sample_document):
    from app.services.intelligence import document_enricher

    sample_document.content = _pages(300, size=900)

    with patch.object(
        document_enricher,
        "content_windows",
        lambda doc: content_windows(doc, max_windows=1),
    ):
        document_enricher._apply_enrichment(
            sample_document, {"title": "Schriftsatz"}, db_session
        )

    meta = sample_document.meta
    assert meta["ai_context_windows"] == 1
    assert meta["ai_context_truncated"] is True
    assert meta["ai_context_chars"] < len(sample_document.content)


def test_the_fuller_quotation_wins_when_a_stance_is_seen_twice():
    from app.services.intelligence import claim_extractor

    doc = _doc(_pages(12))
    link = {"claim_id": 5, "role": "contests"}
    answers = iter(
        [
            ClaimExtraction(evidence_links=[{**link, "excerpt": "kurz"}]),
            ClaimExtraction(evidence_links=[{**link, "excerpt": "ein längeres Zitat"}]),
        ]
    )
    with (
        patch.object(claim_extractor, "content_windows") as windows,
        patch.object(
            claim_extractor, "call_json_ai", side_effect=lambda **_: next(answers)
        ),
    ):
        windows.return_value = content_windows(doc, max_chars=len(doc.content) // 2 + 1)
        merged = claim_extractor._call_claim_extractor_sync(doc, [])

    assert [link["excerpt"] for link in merged["evidence_links"]] == [
        "ein längeres Zitat"
    ]


def test_context_chars_measure_the_condensed_text(db_session, sample_document):
    from app.services.intelligence.document_enricher import _apply_enrichment

    sample_document.content = "![Crest]()" + "N" * 5000 + "\nShort"

    _apply_enrichment(sample_document, {"title": "Schriftsatz"}, db_session)

    assert sample_document.meta["ai_context_strategy"] == "full"
    assert sample_document.meta["ai_context_chars"] == len("\nShort")
