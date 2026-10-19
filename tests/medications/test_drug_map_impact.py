"""Read-only dictionary-update impact over wholly fictional sources and names."""
from copy import deepcopy
import json

import pytest

import drug_map
from extract_testkit import _message
from test_drug_map import DOCUMENT, _dictionary, _seed, store  # noqa: F401


def _after(tmp_path):
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-next"
    first, second = document["entries"][0], document["entries"][1]
    first["aliases"].remove("別キラナ")          # lost_match
    second["aliases"].append("キラーナ")         # became_ambiguous
    second["aliases"].remove("共通架空")         # disambiguated
    second["aliases"].remove("キラナタール")     # candidates_changed (moves to new entry)
    document["entries"].append({"id": "fiction-i3", "kind": "ingredient",
        "display": "架空成分丙", "aliases": ["新架空薬", "キラナタール"],
        "codes": {}, "forms": []})              # newly_matched
    return _dictionary(tmp_path, document)


def _corpus(store):  # noqa: F811
    _seed(store, 1, ("キラナ", "キラナ", "新架空薬"))
    _seed(store, 2, ("別キラナ", "キラーナ", "共通架空", "キラナタール", "ｷﾗﾅ"))
    store.save_messages([_message(mid=3, body="fictional, never extracted")])
    store.save_messages([_message(mid=4, body="fictional malformed source")])
    store.artifact_add("extract_llm", json.dumps({"meds": "x"}), project_id=1,
                       message_id=4, meta={"hash": store.db.execute(
                           "SELECT content_hash FROM messages WHERE message_id=4").fetchone()[0],
                           "engine_version": 4})
    _seed(store, 5, ("キラナ",), kind="extract_v1")


def _keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_impact_classifies_each_transition_and_aggregates_names_only(store, tmp_path):  # noqa: F811
    _corpus(store)
    before = _dictionary(tmp_path)
    after = _after(tmp_path)
    changes = store.db.total_changes
    report = drug_map.impact(store.db, before, after)
    assert store.db.total_changes == changes
    assert report["messages"] == {"evaluated": 3, "unevaluated_no_extraction": 1,
                                  "unevaluated_malformed": 1}
    assert report["counts"] == {"unchanged": 4, "newly_matched": 1, "lost_match": 1,
                                "became_ambiguous": 1, "disambiguated": 1,
                                "candidates_changed": 1}
    assert report["mentions"] == 9 and report["names"]["unchanged"] == 1
    assert [(row["name"], row["transition"]) for row in report["examples"]] == sorted(
        [("キラーナ", "became_ambiguous"), ("キラナタール", "candidates_changed"),
         ("共通架空", "disambiguated"), ("別キラナ", "lost_match"), ("新架空薬", "newly_matched")])
    row = {r["transition"]: r for r in report["examples"]}["became_ambiguous"]
    assert row == {"name": "キラーナ", "transition": "became_ambiguous", "mentions": 1,
                   "before": {"status": "resolved", "candidate_count": 1},
                   "after": {"status": "ambiguous", "candidate_count": 2}}
    assert report["truncated"] == {"examples": False}
    assert report["read_only"] is report["candidate_only"] is True
    assert report["before"]["sha256"] == before.sha256 and report["after"]["id"] == "fictional-next"
    assert not _keys(report) & {"message_id", "project_id", "patient_id", "mid", "pid", "code"}
    encoded = json.dumps(report, ensure_ascii=False)
    assert "fiction-i" not in encoded and "fictional source" not in encoded
    assert "診療上の問題件数ではありません" in report["explanation"]


def test_impact_sorts_by_mentions_and_truncates_examples_not_counts(store, tmp_path):  # noqa: F811
    _seed(store, 1, ("新架空薬", "新架空薬", "別キラナ"))
    _seed(store, 2, ("新架空薬",))
    report = drug_map.impact(store.db, _dictionary(tmp_path), _after(tmp_path), limit=1)
    assert report["examples"] == [{"name": "新架空薬", "transition": "newly_matched",
        "mentions": 3, "before": {"status": "unresolved", "candidate_count": 0},
        "after": {"status": "resolved", "candidate_count": 1}}]
    assert report["counts"]["lost_match"] == 1 and report["truncated"] == {"examples": True}
    empty = drug_map.impact(store.db, _dictionary(tmp_path), _after(tmp_path), limit=0)
    assert empty["examples"] == [] and empty["truncated"]["examples"] is True


def test_impact_same_dictionary_is_all_unchanged(store, tmp_path):  # noqa: F811
    _corpus(store)
    dictionary = _dictionary(tmp_path)
    report = drug_map.impact(store.db, dictionary, dictionary)
    assert report["counts"]["unchanged"] == report["mentions"] == 9
    assert report["examples"] == [] and report["truncated"] == {"examples": False}


@pytest.mark.parametrize("limit", [-1, 201, True, "1"])
def test_impact_rejects_bad_arguments(store, tmp_path, limit):  # noqa: F811
    dictionary = _dictionary(tmp_path)
    with pytest.raises(ValueError):
        drug_map.impact(store.db, dictionary, dictionary, limit=limit)
    with pytest.raises(ValueError):
        drug_map.impact(store.db, dictionary, None)


def test_source_meds_keeps_indices_and_rejects_bad_shapes():
    good = (1, "extract_llm", json.dumps({"meds": [{"name": "甲"}, {"x": 1}, "s", {"name": " "},
                                                   {"name": "乙"}]}))
    assert drug_map._source_meds(good) == [(0, "甲"), (4, "乙")]
    assert drug_map._source_meds((1, "extract_v1", '{"medications": [{"name": "丙"}]}')) == [(0, "丙")]
    assert drug_map._source_meds((1, "extract_llm", "{}")) == []
    for bad in ('{"meds": "x"}', "[]", "null", "{bad", None):
        with pytest.raises(ValueError):
            drug_map._source_meds((1, "extract_llm", bad))
