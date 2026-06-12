"""Unit tests for _model_options — the role-card model <select> renderer.

Guards the fix where an unsaved/unknown model used to render with no `selected`
option, so the browser silently showed the first option as if chosen (and a
re-pick of that already-shown option fired no change event → nothing saved).
A disabled placeholder is now prepended so the dropdown matches the header and
a real pick always changes the value.
"""

import pytest

from app.api.settings_ai_config import _model_options

_PLACEHOLDER = "— select a model —"


@pytest.mark.unit
def test_no_models_returns_single_selected_saved_value():
    """Empty discovery list → keep the saved value as the only (selected) option."""
    html = _model_options([], "qwen3")
    assert html.count("<option") == 1
    assert 'value="qwen3"' in html
    assert "selected" in html
    assert _PLACEHOLDER not in html


@pytest.mark.unit
def test_empty_selection_prepends_disabled_placeholder():
    html = _model_options(["a-model", "b-model"], "")
    assert _PLACEHOLDER in html
    # Placeholder is the selected+disabled first option.
    assert '<option value="" selected disabled>' in html
    # The real options must NOT be pre-selected (no phantom first-option pick).
    assert '<option value="a-model" >' in html or '<option value="a-model">' in html
    assert html.count("<option") == 3


@pytest.mark.unit
def test_saved_value_in_list_is_selected_no_placeholder():
    html = _model_options(["a-model", "b-model"], "b-model")
    assert _PLACEHOLDER not in html
    assert '<option value="b-model" selected>' in html
    assert html.count("<option") == 2


@pytest.mark.unit
def test_saved_value_not_in_list_shows_placeholder():
    """A stored model no longer offered by the endpoint → placeholder, nothing auto-picked."""
    html = _model_options(["a-model", "b-model"], "removed-model")
    assert _PLACEHOLDER in html
    assert (
        "selected" in html.split(_PLACEHOLDER)[0]
    )  # placeholder carries the selection
    assert html.count("<option") == 3
