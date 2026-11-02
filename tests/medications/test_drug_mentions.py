"""Synthetic, source-bound dictionary hint construction."""

from copy import deepcopy
import sqlite3
import time

import pytest

import drug_map
import drug_mentions
from medications_testkit import DOCUMENT, _dictionary, store

__all__ = ["store"]


def configured(store, tmp_path, document=None, *, active=True):
    dictionary = _dictionary(tmp_path, document)
    cfg = {"drug_map": {"path": str(tmp_path / "fictional-drug-map.json"),
                        "sha256": dictionary.sha256}}
    if active:
        with store.db:
            drug_map._save_progress(store.db, {"dictionary": [dictionary.dict_id, dictionary.sha256,
                                                            False, drug_map.RESOLVER_VERSION], "cursor": 0})
    return cfg


def test_source_spans_and_ambiguous_candidates_are_unconfirmed(store, tmp_path, capsys):
    cfg = configured(store, tmp_path)
    body = "本人はキラナを服用。共通架空は未確認。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    assert result["status"] == "active"
    assert result["dictionary"]["sha256"] == cfg["drug_map"]["sha256"]
    first, second = result["matches"]
    assert first["surface"] == "キラナ" and first["candidate_count"] == 1
    assert second["surface"] == "共通架空" and second["candidate_count"] == 2
    assert {candidate["id"] for candidate in second["candidates"]} == {"fiction-i1", "fiction-i2"}
    assert all(body[slice(*row["span"])] == row["surface"] for row in result["matches"])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("fault", ["unset", "unapproved", "no_generation", "bad_pin"])
def test_only_approved_current_pinned_dictionary_is_used(store, tmp_path, fault):
    doc = deepcopy(DOCUMENT)
    if fault == "unapproved":
        doc["source"].pop("approved_by")
    cfg = configured(store, tmp_path, doc, active=fault != "no_generation")
    if fault == "unset":
        cfg = {}
    if fault == "bad_pin":
        cfg["drug_map"]["sha256"] = "0" * 64
    result = drug_mentions.build(store.db, cfg, "キラナ", deadline=time.monotonic() + 10)
    assert result["status"] == {"unset": "unconfigured", "unapproved": "inactive",
                                "no_generation": "inactive", "bad_pin": "invalid"}[fault]
    assert result["dictionary"] is None and result["matches"] == []
    assert drug_mentions.validate(result, "キラナ")


def test_short_substring_and_overlapping_aliases_are_handled_conservatively(store, tmp_path):
    doc = deepcopy(DOCUMENT)
    doc["entries"][0]["aliases"] = ["アア", "キラナ", "キラナ錠", "架空薬甲"]
    cfg = configured(store, tmp_path, doc)
    result = drug_mentions.build(store.db, cfg, "アアとニセキラナ、キラナ錠を服用。架空薬甲を服用。",
                                 deadline=time.monotonic() + 10)
    assert result["status"] == "active"
    assert [row["surface"] for row in result["matches"]] == ["キラナ錠", "架空薬甲"]
    assert result["matches"][0]["candidates"][0]["matched_surface"] == "キラナ錠"
    assert result["overlapping_spans_omitted"] == 0


def test_product_forms_and_strengths_remain_source_grounded(store, tmp_path):
    doc = deepcopy(DOCUMENT)
    doc["entries"] = [
        {"id": f"fiction-{i}", "kind": "ingredient", "display": f"架空成分{i}",
         "aliases": aliases, "codes": {}, "forms": []}
        for i, aliases in enumerate((["架空ナリン"], ["架空セリン", "架空セリン錠"]))]
    cfg = configured(store, tmp_path, doc)
    body = "本人は架空ナリン5mgと架空セリン錠60mgを服用。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    assert [row["surface"] for row in result["matches"]] == ["架空ナリン5mg", "架空セリン錠60mg"]
    assert [row["candidates"][0]["matched_surface"] for row in result["matches"]] == ["架空ナリン", "架空セリン錠"]
    assert drug_mentions.validate(result, body)


def test_short_name_needs_medication_context_and_whole_word(store, tmp_path):
    doc = deepcopy(DOCUMENT)
    doc["entries"][0]["aliases"] = ["アア", "キラナ"]
    cfg = configured(store, tmp_path, doc)
    body = "ああアアた。本人はアアを服用。ひらがなキラナた。ＡキラナＢ。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    assert [row["surface"] for row in result["matches"]] == ["アア"]
    assert drug_mentions.validate(result, body)


def test_many_candidates_for_one_alias_do_not_exhaust_occurrence_limit(store, tmp_path):
    doc = deepcopy(DOCUMENT)
    doc["entries"] = [{"id": f"fiction-{i:03}", "kind": "ingredient", "display": f"架空成分{i}",
                       "aliases": ["共通架空"], "codes": {}, "forms": []} for i in range(129)]
    cfg = configured(store, tmp_path, doc)
    body = "本人は共通架空を服用。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    row, = result["matches"]
    assert result["status"] == "active" and row["candidate_count"] == 129
    assert row["candidates_truncated"] is True and len(row["candidates"]) == 3
    assert drug_mentions.validate(result, body)


def test_contained_aliases_keep_both_unconfirmed_identities(store, tmp_path):
    doc = deepcopy(DOCUMENT)
    doc["entries"] = [{"id": f"fiction-{i}", "kind": "ingredient", "display": f"架空成分{i}",
                       "aliases": [alias], "codes": {}, "forms": []}
                      for i, alias in enumerate(("キラナ", "キラナ錠"))]
    cfg = configured(store, tmp_path, doc)
    body = "本人はキラナ錠を服用。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    row, = result["matches"]
    assert row["surface"] == "キラナ錠" and row["candidate_count"] == 2
    assert {candidate["matched_surface"] for candidate in row["candidates"]} == {"キラナ", "キラナ錠"}
    assert drug_mentions.validate(result, body)


def test_match_limit_returns_partial_snapshot(store, tmp_path, monkeypatch):
    cfg = configured(store, tmp_path)
    monkeypatch.setattr(drug_mentions, "MAX_MATCHES", 1)
    body = "本人はキラナを服用。共通架空は未確認。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    assert result["status"] == "partial" and len(result["matches"]) == 1
    assert drug_mentions.validate(result, body)


def test_unsafe_candidate_label_and_database_error_fail_closed(store, tmp_path, monkeypatch):
    doc = deepcopy(DOCUMENT)
    doc["entries"][0]["display"] = "架空\u202e改変"
    cfg = configured(store, tmp_path, doc)
    body = "本人はキラナを服用。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    assert result["status"] == "partial" and not result["matches"]
    assert drug_mentions.validate(result, body)
    monkeypatch.setattr(drug_map, "active_dictionary", lambda *args: (_ for _ in ()).throw(sqlite3.Error("private")))
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    assert result["status"] == "invalid" and result["matches"] == []
    assert drug_mentions.validate(result, body)


def test_candidate_cap_reports_omission_without_selecting_identity(store, tmp_path):
    doc = deepcopy(DOCUMENT)
    doc["entries"] = [{"id": f"fiction-{i}", "kind": "ingredient", "display": f"架空成分{i}",
                       "aliases": ["共通架空"], "codes": {}, "forms": []} for i in range(5)]
    cfg = configured(store, tmp_path, doc)
    body = "本人は共通架空を服用。"
    result = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    row, = result["matches"]
    assert row["candidate_count"] == 5 and row["candidates_truncated"] is True
    assert len(row["candidates"]) == 3
    assert drug_mentions.validate(result, body)


def test_caps_and_deadline_never_emit_partial_matches(store, tmp_path, monkeypatch):
    cfg = configured(store, tmp_path)
    deadline = time.monotonic() + 10
    assert drug_mentions.build(store.db, cfg, "キラナ", deadline=time.monotonic() - 1)["status"] == "incomplete"
    monkeypatch.setattr(drug_mentions, "MAX_ALIASES", 1)
    result = drug_mentions.build(store.db, cfg, "キラナ", deadline=deadline)
    assert result["status"] == "incomplete" and result["dictionary"] is None and result["matches"] == []
    assert drug_mentions.validate(result, "キラナ")


def test_generation_change_discards_prepared_hints(store, tmp_path, monkeypatch):
    cfg = configured(store, tmp_path)
    real = drug_map.active_dictionary
    calls = []

    def switched(*args):
        calls.append(None)
        return real(*args) if len(calls) == 1 else (None, "inactive")

    monkeypatch.setattr(drug_map, "active_dictionary", switched)
    result = drug_mentions.build(store.db, cfg, "キラナ", deadline=time.monotonic() + 10)
    assert result["status"] == "inactive" and result["dictionary"] is None and result["matches"] == []
    assert drug_mentions.validate(result, "キラナ")


def test_bad_body_or_deadline_never_opens_dictionary(store, monkeypatch):
    monkeypatch.setattr(drug_map, "active_dictionary", lambda *args: pytest.fail("unexpected read"))
    for body, deadline in [("a" * 3001, 1), (None, 1), ("a", float("nan")), ("a", True)]:
        assert drug_mentions.build(store.db, {}, body, deadline=deadline)["status"] == "invalid_input"


@pytest.mark.parametrize("fault", ["source", "span", "candidate", "version", "dictionary", "extra"])
def test_snapshot_validation_rejects_bad_structure_and_source(store, tmp_path, fault):
    cfg = configured(store, tmp_path)
    body = "本人はキラナを服用。"
    snapshot = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    assert drug_mentions.validate(snapshot, body)
    if fault == "source":
        body = body.replace("服用", "中止")
    elif fault == "span":
        snapshot["matches"][0]["span"][1] += 1
    elif fault == "candidate":
        snapshot["matches"][0]["candidate_count"] = 0
    elif fault == "version":
        snapshot["version"] = "future"
    elif fault == "dictionary":
        snapshot["dictionary"]["sha256"] = "bad"
    else:
        snapshot["matches"][0]["unexpected"] = True
    assert not drug_mentions.validate(snapshot, body)


@pytest.mark.parametrize("fault", ["candidate_removed", "count", "boundary", "empty", "truncated", "deadline"])
def test_dictionary_snapshot_replay_rejects_self_consistent_forgery(store, tmp_path, monkeypatch, fault):
    cfg = configured(store, tmp_path)
    body = "本人は共通架空を服用。"
    snapshot = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    dictionary, state = drug_map.active_dictionary(store.db, cfg)
    assert state == "active"
    monkeypatch.setattr(drug_map, "active_dictionary", lambda *args: pytest.fail("unexpected dictionary read"))
    assert drug_mentions.matches_snapshot(dictionary, snapshot, body, deadline=time.monotonic() + 10)
    row = snapshot["matches"][0]
    if fault == "candidate_removed":
        row["candidates"].pop()
        row["candidate_count"] = 1
    elif fault == "count":
        row["candidate_count"] = 3
        row["candidates"].append({**row["candidates"][-1], "id": "fiction-i3"})
    elif fault == "boundary":
        row["span"][1] -= 1
        row["surface"] = row["surface"][:-1]
        for candidate in row["candidates"]:
            candidate["matched_surface"] = row["surface"]
    elif fault == "empty":
        snapshot["matches"] = []
    elif fault == "truncated":
        snapshot["status"] = "partial"
    deadline = time.monotonic() - 1 if fault == "deadline" else time.monotonic() + 10
    assert drug_mentions.validate(snapshot, body)
    assert not drug_mentions.matches_snapshot(dictionary, snapshot, body, deadline=deadline)


def test_dictionary_snapshot_replay_rejects_unknown_dictionary_and_exhausted_scan(store, tmp_path, monkeypatch):
    cfg = configured(store, tmp_path)
    body = "本人は共通架空を服用。"
    snapshot = drug_mentions.build(store.db, cfg, body, deadline=time.monotonic() + 10)
    dictionary, _ = drug_map.active_dictionary(store.db, cfg)
    assert not drug_mentions.matches_snapshot(None, snapshot, body, deadline=time.monotonic() + 10)
    monkeypatch.setattr(drug_mentions, "MAX_ALIASES", 1)
    assert not drug_mentions.matches_snapshot(dictionary, snapshot, body, deadline=time.monotonic() + 10)
