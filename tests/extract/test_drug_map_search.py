"""Synthetic reference search is distinct from exact candidate resolution."""
from copy import deepcopy
import json

import pytest

from test_drug_map import DOCUMENT, _dictionary, _source_document


def test_reference_search_keeps_strength_and_form_products_separate(tmp_path):
    document = _source_document()
    first = document["entries"][0]
    first["aliases"] = ["架空製品錠５ｍｇ", "ｶｸｳｾｲﾋﾝ5mg"]
    second = deepcopy(first)
    second["id"] = "mhlw:medicine:900000002"
    second["display"] = "架空製品液10mg"
    second["aliases"] = ["架空製品液１０ｍｇ", "ｶｸｳｾｲﾋﾝ10mg"]
    second["codes"] = {"medicine": "900000002"}
    second["source_codes"][0]["medicine"] = "900000002"
    document["entries"] = [first, second]
    dictionary = _dictionary(tmp_path, document)
    assert dictionary.resolve("架空製品")["status"] == "unresolved"
    result = dictionary.search("架空製品")
    assert result["total"] == 2
    assert [row["id"] for row in result["items"]] == [first["id"], second["id"]]
    assert all(row["kind"] == "product" for row in result["items"])
    assert dictionary.search("かくうせいひん５ｍｇ")["total"] == 1
    assert dictionary.search("900000002")["items"][0]["matched_by"] == ["id"]
    assert result["read_only"] is result["candidate_only"] is True
    assert "照合済み候補の付与" in result["explanation"]
    assert "status" not in result and "approved_by" not in json.dumps(result)
    assert dictionary.resolve("架空製品")["status"] == "unresolved"


def test_search_collision_results_are_unique_without_automatic_assignment(tmp_path):
    dictionary = _dictionary(tmp_path)
    before = dict(dictionary.aliases)
    result = dictionary.search("共通架空")
    assert result["total"] == 2
    assert [row["id"] for row in result["items"]] == ["fiction-i1", "fiction-i2"]
    assert dictionary.resolve("共通架空")["status"] == "ambiguous"
    assert dictionary.search("キラナタル")["total"] == 0  # no fuzzy LASA repair
    assert dictionary.search("存在しない架空名称")["items"] == []
    assert dict(dictionary.aliases) == before
    assert dictionary.search("共通架空") == result


def test_search_limits_rows_and_aliases_but_counts_all_identities(tmp_path):
    document = deepcopy(DOCUMENT)
    document["entries"] = [
        {"id": f"fiction-{i:03d}", "kind": "ingredient", "display": f"架空群{i:03d}",
         "aliases": [f"共通架空別名{j:03d}" for j in range(8)], "codes": {}, "forms": []}
        for i in range(30)]
    document["source"].pop("approved_by")
    dictionary = _dictionary(tmp_path, document)
    result = dictionary.search("共通架空", limit=2)
    assert result["total"] == 30 and result["returned"] == 2
    assert result["truncated"] is True and result["dictionary"]["approved"] is False
    assert all(row["matching_alias_count"] == 8 and row["aliases_truncated"] is True
               and len(row["matching_aliases"]) == 2 for row in result["items"])
    counts = dictionary.search("共通架空", limit=0)
    assert counts["total"] == 30 and counts["items"] == []
    assert counts["truncated"] is True


@pytest.mark.parametrize("query", ["", " ", "・()", "名" * 513, None, True])
def test_invalid_queries_are_rejected_without_search(tmp_path, query):
    dictionary = _dictionary(tmp_path)
    with pytest.raises(ValueError, match="drug_map_search_argument"):
        dictionary.search(query)


@pytest.mark.parametrize("limit", [-1, 201, True, "20", None])
def test_invalid_limits_are_rejected(tmp_path, limit):
    dictionary = _dictionary(tmp_path)
    with pytest.raises(ValueError, match="drug_map_search_argument"):
        dictionary.search("架空", limit=limit)
