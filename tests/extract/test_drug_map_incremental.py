"""Synthetic dictionary generations and bounded cursor resumption only."""
from copy import deepcopy
import json

import drug_map
from test_drug_map import DOCUMENT, _dictionary, _seed, store as store


def _tick(store, dictionary, monkeypatch, budget=4):
    clock = iter(range(1000))
    monkeypatch.setattr(drug_map.time, "monotonic", lambda: next(clock))
    return drug_map.derive(store, dictionary, deadline=budget)


def test_small_ticks_advance_past_unchanged_prefix_and_resume_after_reload(store, tmp_path, monkeypatch):
    for mid in range(1, 31):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    _seed(store, mid=31)
    statuses = []
    for _ in range(40):
        # No process-local cursor: every call reads the committed artifact.
        result = _tick(store, dictionary, monkeypatch)
        statuses.append(result["status"])
        if drug_map.current_refs(store.db, 31):
            break
    assert "partial" in statuses
    assert drug_map.current_refs(store.db, 31)[0]["cands"][0]["code"] == "fiction-i1"
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                            (drug_map.PROGRESS_KIND,)).fetchone()[0] == 1


def test_earlier_source_changes_are_hidden_then_revisited_next_cycle(store, tmp_path, monkeypatch):
    for mid in range(1, 8):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    _tick(store, dictionary, monkeypatch)
    _seed(store, mid=1, names=("キラナタール",))
    assert drug_map.current_refs(store.db, 1) == []
    for _ in range(15):
        _tick(store, dictionary, monkeypatch)
        if drug_map.current_refs(store.db, 1):
            break
    assert drug_map.current_refs(store.db, 1)[0]["cands"][0]["code"] == "fiction-i2"


def test_dictionary_switch_and_disable_hide_unprocessed_old_rows_immediately(store, tmp_path, monkeypatch):
    for mid in range(1, 9):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    document = deepcopy(DOCUMENT)
    document["dict_id"] = "fictional-next"
    replacement = _dictionary(tmp_path, document)
    result = _tick(store, replacement, monkeypatch)
    assert result["status"] == "partial" and result["pids"] == [1]
    assert drug_map.current_refs(store.db, 8) == []
    for _ in range(15):
        _tick(store, replacement, monkeypatch)
    assert drug_map.current_refs(store.db, 8)[0]["dict_sha256"] == replacement.sha256
    assert _tick(store, None, monkeypatch)["status"] == "unavailable"
    assert drug_map.current_refs(store.db, 8) == []
    for _ in range(15):
        _tick(store, None, monkeypatch)
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                            (drug_map.KIND,)).fetchone()[0] == 0


def test_bad_source_does_not_block_later_messages_or_retain_old_candidate(store, tmp_path):
    _seed(store, mid=1)
    _seed(store, mid=2)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    store.db.execute("UPDATE artifacts SET content='[]' WHERE message_id=1 AND kind='extract_llm'")
    store.db.commit()
    assert drug_map.derive(store, dictionary)["status"] == "ok"
    assert drug_map.current_refs(store.db, 1) == []
    assert drug_map.current_refs(store.db, 2)


def test_lookup_explains_aliases_provenance_and_never_guesses_lasa(tmp_path):
    dictionary = _dictionary(tmp_path)
    result = dictionary.lookup("ｷﾗﾅ")
    assert result["status"] == "resolved"
    assert "キラーナ" in result["matched_aliases"]
    assert result["dictionary"]["sha256"] == dictionary.sha256
    assert result["dictionary"]["approved"] is True
    assert "確定ではありません" in result["explanation"]
    assert dictionary.lookup("キラナタル")["status"] == "unresolved"
    assert dictionary.lookup("共通架空")["status"] == "ambiguous"
    assert dictionary.lookup("架空群薬")["status"] == "generic"
    assert json.dumps(result, ensure_ascii=False)


def test_generation_signature_ignores_cursor_and_changes_on_disable(store, tmp_path, monkeypatch):
    for mid in range(1, 8):
        _seed(store, mid=mid)
    dictionary = _dictionary(tmp_path)
    _tick(store, dictionary, monkeypatch)
    signature = drug_map.generation_signature(store.db)
    _tick(store, dictionary, monkeypatch)
    assert drug_map.generation_signature(store.db) == signature
    _tick(store, None, monkeypatch)
    assert drug_map.generation_signature(store.db) != signature


def test_lookup_unapproved_is_candidate_only_and_does_not_expose_operator_identity(tmp_path):
    document = deepcopy(DOCUMENT)
    document["source"].pop("approved_by")
    result = _dictionary(tmp_path, document).lookup("キラナ")
    assert result["dictionary"]["approved"] is False
    assert "approved_by" not in result["dictionary"]["source"]
    assert all(candidate["candidate"] is True for candidate in result["cands"])


def test_present_corrupt_progress_is_not_legacy_and_recovers_from_latest_marker(store, tmp_path):
    _seed(store)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    assert drug_map.current_refs(store.db, 1)
    store.db.execute("DELETE FROM artifacts WHERE kind=?", (drug_map.PROGRESS_KIND,))
    store.db.commit()
    legacy_signature = drug_map.generation_signature(store.db)
    assert drug_map.current_refs(store.db, 1)  # true absence supports old versions
    store.artifact_add(drug_map.PROGRESS_KIND,
        json.dumps({"dictionary": [dictionary.dict_id, dictionary.sha256, False,
                                  drug_map.RESOLVER_VERSION], "cursor": 0}))
    store.artifact_add(drug_map.PROGRESS_KIND, "[")  # latest marker wins
    assert drug_map.current_refs(store.db, 1) == []
    invalid_signature = drug_map.generation_signature(store.db)
    assert invalid_signature != legacy_signature
    store.db.execute("UPDATE artifacts SET content='[]' WHERE kind=?",
                     (drug_map.PROGRESS_KIND,))
    store.db.commit()
    assert drug_map.generation_signature(store.db) == invalid_signature
    assert drug_map.derive(store, dictionary)["status"] == "ok"
    assert drug_map.current_refs(store.db, 1)


def test_invalid_progress_types_never_publish_stale_refs_or_skip_unprocessed_rows(store, tmp_path):
    _seed(store, mid=1)
    dictionary = _dictionary(tmp_path)
    drug_map.derive(store, dictionary)
    _seed(store, mid=2)
    generation = [dictionary.dict_id, dictionary.sha256, False, drug_map.RESOLVER_VERSION]
    damaged = [
        {"dictionary": generation, "cursor": True},
        {"dictionary": generation, "cursor": -1},
        {"dictionary": generation, "cursor": 2**100},
        {"dictionary": generation, "cursor": "2"},
        {"dictionary": generation, "cursor": float("nan")},
        {"dictionary": True, "cursor": 0},
        {"dictionary": [True, dictionary.sha256, False, drug_map.RESOLVER_VERSION], "cursor": 0},
        {"dictionary": [dictionary.dict_id, True, False, drug_map.RESOLVER_VERSION], "cursor": 0},
        {"dictionary": [dictionary.dict_id, dictionary.sha256, 0, drug_map.RESOLVER_VERSION], "cursor": 0},
        {"dictionary": generation[:3], "cursor": 0},
        {"dictionary": generation},
    ]
    for progress in damaged:
        store.db.execute("UPDATE artifacts SET content=? WHERE kind=?",
                         (json.dumps(progress), drug_map.PROGRESS_KIND))
        store.db.commit()
        assert drug_map.current_refs(store.db, 1) == []
        assert drug_map.derive(store, dictionary)["status"] == "ok"
        assert drug_map.current_refs(store.db, 1)
        assert drug_map.current_refs(store.db, 2)
    store.db.execute("UPDATE artifacts SET content=? WHERE kind=?",
        ('{"dictionary":null,"cursor":0,"dictionary":' + json.dumps(generation) + '}',
         drug_map.PROGRESS_KIND))
    store.db.commit()
    assert drug_map.current_refs(store.db, 1) == []
    drug_map.derive(store, None)
    assert drug_map.current_refs(store.db, 1) == []
    assert store.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=?",
                            (drug_map.KIND,)).fetchone()[0] == 0
