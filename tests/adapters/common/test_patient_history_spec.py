"""Dedicated history cards cannot acquire body, thread, stamp or broadcast parts."""
import copy

import pytest

from adapters.common.spec import validate
from notify_testkit import CFG, _dispatch, _latest_render, led, pinned_clock
from test_patient_history_cards import ready, seed
import json

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def test_patient_history_is_an_explicit_supported_kind(led, monkeypatch):
    event = seed(led)
    ready(monkeypatch)
    _dispatch(led, event, CFG)
    spec = json.loads(_latest_render(led)["spec_json"])
    assert validate(spec) is spec
    assert spec["kind"] == "patient_history"


@pytest.mark.parametrize("field,value", [("thread_name", "extra"), ("thread_body_parts", ["body"]),
                                         ("source_thread", True), ("post_source_id", 1)])
def test_history_forbids_source_post_and_companion_thread_keys(led, monkeypatch, field, value):
    event = seed(led)
    ready(monkeypatch)
    _dispatch(led, event, CFG)
    spec = copy.deepcopy(json.loads(_latest_render(led)["spec_json"]))
    spec["parts"][field] = value
    with pytest.raises(ValueError):
        validate(spec)
