"""Synthetic visit-sheet reads: scoped, read-only, one snapshot."""
import hashlib
import json
from pathlib import Path

import pytest

import mcs_view
import patient_brief
from test_local_chat import snapshot as snapshot  # noqa: F401 - shared synthetic snapshot fixture


def _brief(snapshot, **kwargs):
    view = mcs_view.View(snapshot)
    try:
        return patient_brief.visit_brief(view, 1, cfg={}, **kwargs)
    finally:
        view.close()


def test_brief_has_every_visit_section_scoped_to_the_patient(snapshot):
    before = hashlib.sha256(Path(snapshot).read_bytes()).digest()
    brief = _brief(snapshot)
    assert [s["key"] for s in brief["sections"]] == list(patient_brief.VISIT_SECTIONS)
    assert all(s["title"] for s in brief["sections"])
    assert all(set(item) <= set(patient_brief.ITEM_KEYS) for s in brief["sections"] for item in s["items"])
    assert brief["signals"]["key"] == "signals" and brief["read_only"] is True
    assert "非対象" not in json.dumps(brief, ensure_ascii=False)
    assert hashlib.sha256(Path(snapshot).read_bytes()).digest() == before


def test_since_the_previous_visit_is_used_for_changes(snapshot):
    brief = _brief(snapshot, since=0, sections=("changes",))
    assert brief["since"] == 0 and [s["key"] for s in brief["sections"]] == ["changes"]


@pytest.mark.parametrize("kwargs", [{"since": -1}, {"since": True}, {"sections": ("measurements",)},
                                    {"sections": ("unknown",)}, {"role": "nope"}])
def test_arguments_are_strict(snapshot, kwargs):
    with pytest.raises(ValueError):
        _brief(snapshot, **kwargs)


def test_unknown_patient_is_refused(snapshot):
    view = mcs_view.View(snapshot)
    try:
        with pytest.raises(ValueError, match="unknown_project"):
            patient_brief.visit_brief(view, 99, cfg={})
    finally:
        view.close()
