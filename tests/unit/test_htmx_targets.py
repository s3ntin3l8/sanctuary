"""Pins HTMX swap targets so dead-target drift is caught.

`#triage-doc-pane` was a target that no longer exists in the redesigned
triage HUD. Three sites referenced it, all silently no-op'ing user actions:
  - hud/_case.html : Ratify / Reject draft case
  - triage_row.html : retry-ai
"""

from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"


@pytest.mark.unit
def test_no_dead_triage_doc_pane_references():
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if "triage-doc-pane" in text:
            offenders.append(str(path.relative_to(TEMPLATES.parent)))
    assert not offenders, (
        "#triage-doc-pane is no longer defined anywhere in the templates, "
        "so HTMX swaps targeting it silently no-op. Offending files:\n  - "
        + "\n  - ".join(offenders)
    )


@pytest.mark.unit
def test_no_dead_triage_store_activeDoc_references():
    """The Alpine store property is `expandedActiveDocId`, not `activeDoc`.

    `$store.triage.activeDoc` evaluates to undefined and gets serialized as
    the literal string "undefined" when bound to a hidden form input.
    """
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if (
            "$store.triage.activeDoc" in text
            and "expandedActiveDocId"
            not in text.split("$store.triage.activeDoc")[0].split("\n")[-1]
        ):
            for i, line in enumerate(text.splitlines(), start=1):
                if (
                    "$store.triage.activeDoc" in line
                    and "expandedActiveDocId" not in line
                ):
                    offenders.append(
                        f"{path.relative_to(TEMPLATES.parent)}:{i}: {line.strip()}"
                    )
    assert not offenders, (
        "$store.triage.activeDoc is undefined — use expandedActiveDocId. "
        "Offenders:\n  - " + "\n  - ".join(offenders)
    )
