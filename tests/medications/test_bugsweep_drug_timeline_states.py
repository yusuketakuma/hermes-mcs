"""Synthetic regressions: the drug timeline says 照合中 only while matching can still happen."""

from copy import deepcopy

import pytest

import drug_map
import drug_timeline
from medications_testkit import DOCUMENT, _dictionary
from test_drug_timeline import canonical_source, corpus, current, view_of

__all__ = ["corpus"]


def test_unconfigured_dictionary_is_not_reported_as_matching(corpus):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert report["coverage"]["dictionary"] == "unconfigured"
    assert report["items"][0]["matching"] == "not_performed"
    assert report["coverage"]["matching_pending_on_page"] == 0
    assert "照合中" not in report["explanation"]


def test_reference_only_pin_is_not_reported_as_matching(corpus, tmp_path):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    document = deepcopy(DOCUMENT)
    del document["source"]["approved_by"]
    dictionary = _dictionary(tmp_path, document)
    assert drug_map.derive(corpus, dictionary)["status"] == "unavailable"
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"), "sha256": dictionary.sha256}}
    report = drug_timeline.timeline(view_of(corpus), 1, cfg=cfg)
    assert report["coverage"]["dictionary"] == "reference_only"
    assert report["items"][0]["candidate"] is None
    assert "照合中" not in report["explanation"]


def test_approved_pin_not_yet_derived_is_still_matching(corpus, tmp_path):
    current(corpus, 1, [{"name": "キラナ", "subject": "patient"}])
    dictionary = _dictionary(tmp_path)
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"), "sha256": dictionary.sha256}}
    report = drug_timeline.timeline(view_of(corpus), 1, cfg=cfg)
    assert report["coverage"]["dictionary"] == "pending" and "照合中" in report["explanation"]


@pytest.mark.parametrize("case", ["missing", "ambiguous"])
def test_unestablished_names_never_wait_for_matching(corpus, tmp_path, monkeypatch, case):
    canonical_source(corpus, monkeypatch, grounded=case != "missing",
                     extra_names=("キラーナ錠10mg",) if case == "ambiguous" else ())
    dictionary = _dictionary(tmp_path)
    with corpus.db:
        drug_map._save_progress(corpus.db, {"dictionary": [dictionary.dict_id, dictionary.sha256,
                                                         False, drug_map.RESOLVER_VERSION], "cursor": 0})
    report = drug_timeline.timeline(view_of(corpus), 1)
    assert report["coverage"]["dictionary"] == "derived"
    item, = report["items"]
    assert item["mention"]["name_state"] in ("unknown", "ambiguous")
    assert item["matching"] == "not_applicable"
    assert report["coverage"]["matching_pending_on_page"] == 0
    assert "照合中" not in report["explanation"]
