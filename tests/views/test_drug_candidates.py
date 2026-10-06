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


@pytest.mark.parametrize("field", ["content", "meta"])
def test_deep_annotation_keeps_stats_and_rollup_available_without_writes(
        store, tmp_path, field):
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path))
    valid = _stats(store)
    assert valid["by_ingredient_candidate"]["by_resolution"]["resolved"]["numerator"] == 1
    deep = "[" * 10000 + "0" + "]" * 10000
    store.db.execute(f"UPDATE artifacts SET {field}=? WHERE kind=?", (deep, drug_map.KIND))
    store.db.commit()
    before = tuple(store.db.iterdump())
    changes = store.db.total_changes
    stats = _stats(store)
    assert stats["action_totals"] == valid["action_totals"]
    assert stats["by_name_month"] == valid["by_name_month"]
    assert stats["by_ingredient_candidate"]["status"] == "unavailable"
    assert stats["by_ingredient_candidate"]["by_resolution"]["unavailable"]["numerator"] == 1
    roll = rollup.build_rollup(store, 1)
    assert len(roll["medications"]) == 1 and not roll["medications"][0].get("ref")
    text = brain_export._patient_md(1, "fictional", {}, roll)
    assert "架空成分甲" not in text and "fictional-v1@" not in text
    assert tuple(store.db.iterdump()) == before
    assert store.db.total_changes == changes


def test_class_alias_cannot_be_counted_as_resolved_ingredient(store, tmp_path):
    document = deepcopy(DOCUMENT)
    document["entries"][2]["aliases"].append("キラナ")
    _seed(store)
    drug_map.derive(store, _dictionary(tmp_path, document))
    block = _stats(store)["by_ingredient_candidate"]
    assert block["by_resolution"]["ambiguous"]["numerator"] == 1
    assert block["by_resolution"]["resolved"]["numerator"] == 0
    assert block["items"]["items"] == []


def test_official_identities_have_separate_blocks_with_one_ref_read_per_message(store, tmp_path, monkeypatch):
    from test_drug_map import _source_document
    _seed(store, names=("架空製品", "架空ブランド", "架空製品錠10mg", "キラナ"))
    before = _stats(store)
    drug_map.derive(store, _dictionary(tmp_path, _source_document()))
    calls = []
    original = mcs_stats.current_refs

    def counted(db, mid):
        calls.append(mid)
        return original(db, mid)

    monkeypatch.setattr(mcs_stats, "current_refs", counted)
    after = _stats(store)
    assert calls == [1]
    assert after["by_name_month"] == before["by_name_month"]
    assert after["action_totals"] == before["action_totals"]
    assert after["by_ingredient_candidate"]["items"]["items"] == []
    product = after["by_product_candidate"]["items"]["items"][0]
    general = after["by_general_name_candidate"]["items"]["items"][0]
    assert product["code"] == "mhlw:medicine:900000001"
    assert general["code"] == "mhlw:general:GEN000000001"
    assert product["candidate"] is general["candidate"] is True
    assert product["share"]["numerator"] == general["share"]["numerator"] == 1
    assert product["share"]["denominator"] == general["share"]["denominator"] == 4
    review = after["drug_map_review"]
    assert review["status"] == "ok" and review["review_mentions"] == 2
    assert {row["name"] for row in review["items"]["items"]} == {"架空製品錠10mg", "キラナ"}


def test_review_does_not_call_missing_annotations_unknown_aliases(store, tmp_path):
    _seed(store)
    unconfigured = _stats(store)["drug_map_review"]
    assert unconfigured["status"] == "unconfigured"
    assert unconfigured["reason"] == "dictionary_generation_unrecorded"
    assert unconfigured["review_mentions"] == 0 and unconfigured["unavailable_mentions"] == 1
    assert unconfigured["items"]["items"] == []
    drug_map.derive(store, _dictionary(tmp_path))
    drug_map.derive(store, None)
    unavailable = _stats(store)
    assert unavailable["drug_map_review"]["status"] == "unavailable"
    assert unavailable["drug_map_review"]["reason"] == "dictionary_disabled_or_unavailable"
    assert unavailable["drug_map_review"]["items"]["items"] == []
    assert all(unavailable[key]["items"]["items"] == [] for key in (
        "by_ingredient_candidate", "by_general_name_candidate", "by_product_candidate"))


def test_corrupt_dictionary_generation_hides_all_positive_stats_and_curation(store, tmp_path):
    _seed(store, names=("キラナ", "共通架空", "架空不明"))
    drug_map.derive(store, _dictionary(tmp_path))
    store.db.execute("UPDATE artifacts SET content='[' WHERE kind=?", (drug_map.PROGRESS_KIND,))
    store.db.commit()
    stats = _stats(store)
    assert stats["drug_map_review"]["reason"] == "dictionary_generation_invalid"
    assert stats["drug_map_review"]["review_mentions"] == 0
    assert stats["drug_map_review"]["unavailable_mentions"] == 3
    assert stats["by_ingredient_candidate"]["items"]["items"] == []


def test_raw_names_and_actions_remain_distinct_and_nonpatient_mentions_are_excluded(store, tmp_path):
    import json
    aid = _seed(store, names=("キラナ", "別キラナ", "共通架空", "架空不明", "キラナタル"))
    content = json.loads(store.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])
    content["meds"][0]["action"] = "stop"
    excluded = [{**content["meds"][0], **change} for change in (
        {"negated": True}, {"status": "past"}, {"subject": "family"}, {"unverified": True})]
    content["meds"].extend(excluded)
    store.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(content), aid))
    store.db.commit()
    drug_map.derive(store, _dictionary(tmp_path))
    stats = _stats(store)
    assert stats["distinct_names"] == 5
    assert stats["action_totals"] == {"stop": 1, "start": 4}
    assert stats["by_ingredient_candidate"]["mentions"] == 5
    assert stats["by_ingredient_candidate"]["items"]["items"][0]["mentions"] == 2
    assert stats["by_general_name_candidate"]["items"]["items"] == []
    assert stats["by_product_candidate"]["items"]["items"] == []
    review = stats["drug_map_review"]
    assert review["review_mentions"] == 3
    assert {row["status"] for row in review["items"]["items"]} == {"unresolved", "ambiguous"}
    assert {row["reason"] for row in review["items"]["items"]} == {
        "no_exact_dictionary_match", "multiple_identity_candidates"}


def test_review_list_is_bounded_while_total_names_and_mentions_are_complete(store, tmp_path):
    _seed(store, names=tuple(f"架空不明{i:03d}" for i in range(125)))
    drug_map.derive(store, _dictionary(tmp_path))
    review = _stats(store)["drug_map_review"]
    assert review["items"]["total"] == review["review_mentions"] == 125
    assert review["items"]["returned"] == 100 and review["items"]["truncated"] is True
    assert len(review["items"]["items"]) == 100
    assert all(row["share"]["denominator"] == 125 for row in review["items"]["items"])


def test_all_new_local_fields_are_omitted_from_c1_export(store, tmp_path):
    from test_drug_map import _source_document
    _seed(store, names=("架空製品", "架空ブランド", "架空不明"))
    drug_map.derive(store, _dictionary(tmp_path, _source_document()))
    stats = _stats(store)
    projected = project_record({
        "type": "stat", "contract": "mcs-read-model/1",
        "snapshot_generation_id": "fictional-generation",
        "preset": "pharmacy", "name": "meds", "value": stats})
    assert not any(key in projected["value"] for key in (
        "by_ingredient_candidate", "by_general_name_candidate", "by_product_candidate", "drug_map_review"))
    assert projected["content_omitted"] is True
    assert projected["value"]["action_totals"] == stats["action_totals"]
