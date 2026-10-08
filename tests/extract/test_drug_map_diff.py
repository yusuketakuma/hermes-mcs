"""Read-only dictionary comparison with wholly fictional names and identities."""
from copy import deepcopy
import json

import pytest

import drug_map
from test_drug_map import DOCUMENT, _dictionary


def _pair(tmp_path, edit):
    before = _dictionary(tmp_path)
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-next"
    edit(document)
    after = _dictionary(tmp_path, document)
    return before, after


def test_diff_counts_added_removed_display_kind_and_alias_changes(tmp_path):
    def edit(document):
        document["entries"].pop(1)
        document["entries"][0]["display"] = "架空成分甲の改名"
        document["entries"][0]["kind"] = "class"
        document["entries"][0]["aliases"].append("追加架空別名")
        document["entries"].append({"id": "fiction-new", "kind": "ingredient",
            "display": "架空新成分", "aliases": [], "codes": {}, "forms": []})
    before, after = _pair(tmp_path, edit)
    result = before.diff(after)
    assert {key: result["counts"][key] for key in (
        "added", "removed", "changed", "display_changed", "kind_changed", "aliases_changed"
    )} == {"added": 1, "removed": 1, "changed": 1, "display_changed": 1,
           "kind_changed": 1, "aliases_changed": 1}
    assert [change["id"] for change in result["changes"]] == ["fiction-i1", "fiction-i2", "fiction-new"]
    assert result["changes"][0]["fields"] == ["display", "kind", "aliases"]
    assert result["before"]["sha256"] == before.sha256
    assert result["after"]["sha256"] == after.sha256
    assert "同等性" in result["explanation"]
    assert "approved_by" not in json.dumps(result)


def test_normalized_alias_collision_and_existing_collision_expansion_are_reported(tmp_path):
    def edit(document):
        document["entries"][1]["aliases"].append("ｷﾗﾅ")
        document["entries"][2]["aliases"].append("共通架空")
    before, after = _pair(tmp_path, edit)
    result = before.diff(after)
    assert result["counts"]["new_ambiguous_aliases"] == 1
    assert result["counts"]["expanded_ambiguous_aliases"] == 1
    collisions = {row["alias"]: row for row in result["collisions"]}
    assert collisions[drug_map.fold("キラナ")]["after_count"] == 2
    assert collisions[drug_map.fold("共通架空")]["after_count"] == 3
    assert after.resolve("ｷﾗﾅ")["status"] == "ambiguous"


def test_diff_is_bounded_but_counts_are_complete_and_inputs_unchanged(tmp_path):
    def edit(document):
        document["entries"].extend({"id": f"new-{i:04d}", "kind": "ingredient",
            "display": f"架空追加{i}", "aliases": ["衝突架空"], "codes": {}, "forms": []}
            for i in range(80))
        document["source"].pop("approved_by")
    before, after = _pair(tmp_path, edit)
    snapshot = (dict(before.aliases), dict(after.aliases))
    result = before.diff(after, limit=2)
    assert result["counts"]["added"] == 80
    assert len(result["changes"]) == 2
    assert result["collisions"][0]["after_count"] == 80
    assert len(result["collisions"][0]["after_candidates"]) == 2
    assert result["collisions"][0]["candidates_truncated"] is True
    assert result["truncated"] == {"changes": True, "collisions": False}
    assert result["after"]["approved"] is False
    assert before.diff(after, limit=2) == result
    assert (dict(before.aliases), dict(after.aliases)) == snapshot
    counts_only = before.diff(after, limit=0)
    assert counts_only["counts"] == result["counts"]
    assert counts_only["changes"] == counts_only["collisions"] == []


def test_unchanged_dictionary_and_lasa_names_do_not_become_equivalent(tmp_path):
    dictionary = _dictionary(tmp_path)
    assert not any(dictionary.diff(dictionary)["counts"].values())
    assert dictionary.diff(dictionary)["changes"] == []
    assert dictionary.resolve("キラナタル")["status"] == "unresolved"


@pytest.mark.parametrize("limit", [-1, 201, True, "50", None])
def test_diff_invalid_limit_is_rejected(tmp_path, limit):
    dictionary = _dictionary(tmp_path)
    with pytest.raises(ValueError, match="drug_map_diff_argument"):
        dictionary.diff(dictionary, limit=limit)
