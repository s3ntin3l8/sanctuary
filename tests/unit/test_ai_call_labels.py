"""Every AI debug label must survive ``_LABEL_RE`` and ``_resolve_run_ids``.

The regex is greedy: a stage containing ``_`` shifts the id (``doc_7_a_b`` →
scope ``7_a``), and ``int(scope_id)`` then fails after the call already ran.
"""

import re
from pathlib import Path

import pytest

from app.services.intelligence._ai_call import _LABEL_RE, _resolve_run_ids

APP = Path(__file__).resolve().parents[2] / "app"
_LABEL_LITERAL = re.compile(r'debug_label=f?"((?:doc|batch|case)_[^"]+)"')


def _labels() -> list[str]:
    found = []
    for path in APP.rglob("*.py"):
        for raw in _LABEL_LITERAL.findall(path.read_text()):
            found.append(re.sub(r"\{[^}]+\}", "7", raw))
    return found


@pytest.mark.unit
def test_labels_are_discovered():
    assert "doc_7_enricherreduce" in _labels()


@pytest.mark.unit
@pytest.mark.parametrize("label", sorted(set(_labels())))
def test_label_stage_has_no_underscore_and_id_parses(label):
    m = _LABEL_RE.match(label)
    assert m, label
    kind, scope_id, stage = m.groups()
    assert "_" not in stage
    if kind != "case":
        _resolve_run_ids(kind, scope_id, None, None)


@pytest.mark.unit
def test_enricher_reduce_label_resolves_to_its_document():
    kind, scope_id, _ = _LABEL_RE.match("doc_118_enricherreduce").groups()
    assert _resolve_run_ids(kind, scope_id, 44, None) == (118, 44, None)
