"""Synthetic regressions: dictionary reuse, width-folded hints and a vanished pinned file."""

from copy import deepcopy
import time

import drug_info
import drug_map
import drug_mentions
from medications_testkit import DOCUMENT, _dictionary, store
from test_drug_info import BODY, PDF, ROWS, Gateway

__all__ = ["store"]


def _active_cfg(store, tmp_path, document):
    dictionary = _dictionary(tmp_path, document)
    with store.db:
        drug_map._save_progress(store.db, {"dictionary": [dictionary.dict_id, dictionary.sha256,
                                                        False, drug_map.RESOLVER_VERSION], "cursor": 0})
    return {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"),
                         "sha256": dictionary.sha256}}


def test_unchanged_pinned_file_is_parsed_once_and_a_replacement_is_reread(tmp_path, monkeypatch):
    first = _dictionary(tmp_path)
    path = tmp_path / "fictional-drug-map.json"
    parsed = []
    real = drug_map.json.loads
    monkeypatch.setattr(drug_map.json, "loads", lambda raw: parsed.append(1) or real(raw))
    assert drug_map.load(path, expected_sha256=first.sha256) is first
    assert parsed == []
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-v2"
    second = _dictionary(tmp_path, document)
    assert parsed == [1] and second is not first and second.dict_id == "fictional-v2"


def test_half_width_strength_matches_full_width_alias_at_original_span(store, tmp_path):
    document = deepcopy(DOCUMENT)
    document["entries"] = [{"id": "fiction-w1", "kind": "ingredient", "display": "架空成分丙",
                            "aliases": ["キラナ錠６０ｍｇ"], "codes": {}, "forms": []}]
    cfg = _active_cfg(store, tmp_path, document)
    body = "本人はキラナ錠60mgを頓用。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    row, = result["matches"]
    assert result["status"] == "active" and row["surface"] == "キラナ錠60mg"
    assert body[slice(*row["span"])] == row["surface"] and row["span"][0] == body.index("キラナ")
    assert row["candidates"][0]["id"] == "fiction-w1"
    assert row["candidates"][0]["matched_surface"] == "キラナ錠60mg"
    assert drug_mentions.validate(result, body)
    dictionary, _ = drug_map.active_dictionary(store.db, cfg)
    assert drug_mentions.matches_snapshot(dictionary, result, body, deadline=time.monotonic() + 10)


def test_vanished_pinned_dictionary_is_an_error_not_unconfigured(store, tmp_path):
    cfg = _active_cfg(store, tmp_path, DOCUMENT)
    (tmp_path / "fictional-drug-map.json").unlink()
    assert drug_map.configured(cfg) == (None, "missing_dictionary")
    assert drug_map.active_dictionary(store.db, cfg) == (None, "invalid")
    result = drug_mentions.build(store.db, cfg, "キラナ", deadline=time.monotonic() + 10)
    assert result["status"] == "invalid" and drug_mentions.validate(result, "キラナ")
    assert drug_map.configured({}) == (None, None)


def test_name_sharing_a_watched_insert_is_not_searched_again(tmp_path, monkeypatch):
    fake = Gateway(bodies={PDF.format("B"): BODY + " 架空アオ錠２０ｍｇ「架空」"})
    monkeypatch.setattr(drug_info, "call", fake)
    names = ["架空アオ錠１０ｍｇ「架空」", "架空アオ錠２０ｍｇ「架空」"]
    first = {r["name"]: r["status"] for r in drug_info.formulary_add(names, ROWS, root=tmp_path)}
    assert first == {names[0]: "registered", names[1]: "already_watched_url"}
    sent = len(fake.sent)
    again = drug_info.formulary_add(names, ROWS, root=tmp_path)
    assert [r["status"] for r in again] == ["already_watched", "already_watched"]
    assert len(fake.sent) == sent
    watch = drug_info.load_watch(tmp_path)
    assert len(watch["inserts"]) == 1 and watch["inserts"][0]["aliases"] == [names[1]]
