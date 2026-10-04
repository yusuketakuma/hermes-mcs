"""Medication candidate stats/export integration with wholly fictional dictionaries."""

from copy import deepcopy

import pytest

import brain_export
import drug_map
from export_schema import project_record
from extract_testkit import _ledger
import mcs_stats
import rollup
from test_drug_map import DOCUMENT, _dictionary, _seed
from views_testkit import SNAP_TS


@pytest.fixture
def store(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    yield ledger
    ledger.close()


def _stats(store):
    return mcs_stats.run_stats(store.db, SNAP_TS, {"stat": "meds"})["stats"]["meds"]


def test_candidate_breakdown_keeps_surface_counts_and_honest_denominators(store, tmp_path):
    _seed(store, names=("キラナ", "別キラナ", "共通架空", "架空群薬", "架空不明"))
    before = _stats(store)
    drug_map.derive(store, _dictionary(tmp_path))
    after = _stats(store)
    assert after["by_name_month"] == before["by_name_month"]
    assert after["action_totals"] == before["action_totals"]
    assert after["distinct_names"] == before["distinct_names"] == 5
    block = after["by_ingredient_candidate"]
    assert block["status"] == "ok" and block["mentions"] == 5
    assert {k: v["numerator"] for k, v in block["by_resolution"].items()} == {
        "resolved": 2, "ambiguous": 1, "generic": 1, "unresolved": 1, "unavailable": 0}
    assert all(v["denominator"] == 5 for v in block["by_resolution"].values())
    item = block["items"]["items"][0]
    assert item["code"] == "fiction-i1" and item["mentions"] == 2
    assert item["share"]["value"] == 2 / 5 and item["candidate"] is True


def test_empty_and_missing_dictionary_are_unavailable_not_resolved(store):
    block = _stats(store)["by_ingredient_candidate"]
    assert block["status"] == "unavailable"
    assert block["by_resolution"]["resolved"]["value"] is None
    _seed(store)
    block = _stats(store)["by_ingredient_candidate"]
    assert block["by_resolution"]["unavailable"]["numerator"] == 1
    assert block["by_resolution"]["resolved"]["numerator"] == 0


def test_export_candidate_notes_and_machine_contract_remain_separate(store, tmp_path):
    _seed(store, names=("キラナ", "共通架空"))
    drug_map.derive(store, _dictionary(tmp_path))
    roll = rollup.build_rollup(store, 1)
    text = brain_export._patient_md(1, "fictional", {}, roll)
    assert "架空成分甲" in text
    assert "架空成分乙" not in text
    assert "複数候補" in text and "fictional-v1@" in text
    stat = _stats(store)
    projected = project_record({
        "type": "stat", "contract": "mcs-read-model/1",
        "snapshot_generation_id": "fictional-generation",
        "preset": "pharmacy", "name": "meds", "value": stat})
    assert "by_ingredient_candidate" not in projected["value"]
    assert projected["content_omitted"] is True
    assert projected["value"]["action_totals"] == stat["action_totals"]


def test_stats_do_not_read_dictionary_and_ignore_stale_source_refs(store, tmp_path):
    _seed(store)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    (tmp_path / "fictional-drug-map.json").unlink()
    assert _stats(store)["by_ingredient_candidate"]["status"] == "ok"
    _seed(store, names=("別キラナ",))
    assert _stats(store)["by_ingredient_candidate"]["status"] == "unavailable"
    assert not rollup.build_rollup(store, 1)["medications"][0].get("ref")


def test_class_alias_cannot_be_counted_as_resolved_ingredient(store, tmp_path):
    document = deepcopy(DOCUMENT)
    document["entries"][2]["aliases"].append("キラナ")
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path, document))
    block = _stats(store)["by_ingredient_candidate"]
    assert block["by_resolution"]["ambiguous"]["numerator"] == 1
    assert block["by_resolution"]["resolved"]["numerator"] == 0
    assert block["items"]["items"] == []
