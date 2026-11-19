"""A closed extract.upgrade_scope keeps earlier-generation posts out of the LLM selection only.

New arrivals, edited bodies, same-generation QC fixes and human reports still
run; unset and "all" select exactly what they selected before. Synthetic
bodies, temp DB and a stubbed extractor only."""
import json
import time

import pytest

import cloud_policy
import extract_llm as llm
import extract_llm_runner
import extract_llm_selection
import mcs_util
from anthropic_runtime_testkit import old, routed
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY
from mcs_queries import EXTRACT_FEEDBACK_KIND
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ["routed"]

OLD = llm.EXTRACT_VERSION - 1
ALL = {1, 2, 3, 4, 5, 6, 7, 8}
NEW_WORK = {3, 4, 5, 6}  # arrival, edited body, same-generation QC fix, human report


def _config(tmp_path, monkeypatch, value):
    path = tmp_path / "config.json"
    if value is not None:
        path.write_text(json.dumps({"extract": {"upgrade_scope": value}}), encoding="utf-8")
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))


def _row(db, mid, version, *, pid, chash=None, content='{"summary":"合成の結果"}', **meta):
    return db.artifact_add(llm.KIND, content, project_id=pid, message_id=mid, model=llm.MODEL,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": chash or _hash(db, mid),
              "extract_version": version, **meta})


def _seed(db):
    db.save_messages([_message(1, "完全合成の記録一です。", project_id=1),
                      _message(2, "完全合成の記録二です。", project_id=2),
                      _message(3, "完全合成の新着です。", project_id=2),
                      _message(4, "完全合成の記録四を編集しました。", project_id=2),
                      _message(5, "脈は48回／分です", project_id=2),
                      _message(6, "完全合成の記録六です。", project_id=2),
                      _message(7, "完全合成の記録七です。", project_id=2),
                      _message(8, "完全合成の記録八です。", project_id=2)])
    _row(db, 1, OLD, pid=1)                                   # earlier generation, patient 1
    _row(db, 2, OLD, pid=2)                                   # earlier generation, patient 2
    _row(db, 4, OLD, pid=2, chash="0" * 64)                   # the body changed since
    source = _row(db, 5, llm.EXTRACT_VERSION, pid=2, content='{"vitals":{"bs":48},"summary":"s"}')
    db.artifact_add("extract_qc", json.dumps({"qc": "done", "items": [
        {"section": "vitals", "index": "bs", "item": {"vitals": {"bs": 48}}, "verdict": "NO_MATCH",
         "noul": 0.1}]}), project_id=2, message_id=5, model="jev-test",
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, 5), "extract_version": llm.EXTRACT_VERSION,
              "source_artifact_id": source, "qc": "done"})
    human = _row(db, 6, OLD, pid=2)                           # a human ⚠ report on the old result
    db.artifact_add(EXTRACT_FEEDBACK_KIND, json.dumps({"artifact_id": human}), project_id=2, message_id=6)
    prior = _row(db, 7, OLD, pid=2)                           # a hold a code update released
    db.artifact_add(llm.UPGRADE_KIND, json.dumps({
        "state": "held_prior", "hash": _hash(db, 7), "prior_artifact_id": prior,
        "target_version": llm.EXTRACT_VERSION, "contract": "synthetic-earlier-contract",
        "reason": "protected_loss"}), project_id=2, message_id=7, meta={VERSION_KEY: CURRENT_VERSION})
    _row(db, 8, OLD, pid=2)                                   # one failed try at this generation
    _row(db, 8, llm.EXTRACT_VERSION, pid=2, content="{}", error=1, attempts=1, next_try=time.time() - 1)


def _selected(db):
    rows, _fresh = llm._select_pending(db, 100, None, None, False)
    return {row["message_id"] for row in rows}


@pytest.mark.parametrize("value,expected", [
    (None, ALL), ("all", ALL), ("none", NEW_WORK),
    ({"project_ids": [1]}, NEW_WORK | {1}), ({"project_ids": [2]}, NEW_WORK | {2, 7, 8}),
    ({"project_ids": [1, 2]}, ALL), ({}, NEW_WORK), ("ALL", NEW_WORK), (True, NEW_WORK),
    ({"project_ids": ["1"]}, NEW_WORK), ({"project_ids": [1, 1]}, NEW_WORK),
], ids=repr)
def test_selection_follows_the_scope(tmp_path, monkeypatch, value, expected):
    _config(tmp_path, monkeypatch, value)
    db = _ledger(tmp_path)
    try:
        _seed(db)
        assert _selected(db) == expected
    finally:
        db.close()


def test_an_unreadable_config_selects_no_earlier_generation(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, None)
    (tmp_path / "config.json").write_text('{"extract": {"upgrade_scope": "all"', encoding="utf-8")
    db = _ledger(tmp_path)
    try:
        _seed(db)
        assert _selected(db) == NEW_WORK
    finally:
        db.close()


def test_open_scope_keeps_the_selection_sql_unchanged(tmp_path, monkeypatch):
    seen = []
    original = extract_llm_selection._selection_sql

    def spy(db, order, adm_sql, *args, **kwargs):
        seen[-1].append(adm_sql)
        return original(db, order, adm_sql, *args, **kwargs)
    monkeypatch.setattr(extract_llm_selection, "_selection_sql", spy)
    db = _ledger(tmp_path)
    try:
        _seed(db)
        for value in (None, "all", "none"):
            _config(tmp_path, monkeypatch, value)
            seen.append([])
            _selected(db)
        unset, explicit, closed = seen
        assert unset and explicit == unset and closed != unset
    finally:
        db.close()


def test_closed_scope_run_keeps_old_results_and_extracts_new_work(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, "none")
    calls = []

    def extract(body, *, hints, meta_out, **_kwargs):
        calls.append(body)
        result = {"summary": "合成の新結果"}
        meta_out.update(completeness_chunks=1, completeness={"0": llm._hint_coverage(result, hints, body)})
        return result
    monkeypatch.setattr(llm, "llm_extract", extract)
    monkeypatch.setattr(extract_llm_runner, "llm_extract", llm.llm_extract)
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1, "完全合成の記録一です。"), _message(2, "完全合成の新着です。")])
        old = _row(db, 1, OLD, pid=1)
        assert llm.run_pending(db, limit=5, budget_s=10)["done"] == 1
        assert llm.run_pending(db, limit=5, budget_s=10)["selected"] == 0
        assert calls == ["完全合成の新着です。"]
        rows = {r["message_id"]: (r["artifact_id"], json.loads(r["meta"])["extract_version"])
                for r in db.artifacts(llm.KIND)}
        assert rows == {1: (old, OLD), 2: (rows[2][0], llm.EXTRACT_VERSION)}
    finally:
        db.close()


def test_cloud_followup_selection_follows_the_same_scope(routed, tmp_path, monkeypatch):
    led, _cfg, *_ = routed
    old(led, 1)
    old(led, 2)
    led.save_messages([_message(3, "完全合成の新着です。")])
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 1200,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, "synthetic-upgrade-scope", time.time())
    seen = {}
    for value in (None, "none", {"project_ids": [1]}, {"project_ids": [2]}):
        _config(tmp_path, monkeypatch, value)
        with legacy_cloud_routes():
            rows = llm._select_pending(led, 64, None, None, False, cloud_only=True)[0]
        seen[json.dumps(value)] = {row["message_id"] for row in rows}
    assert seen == {"null": {1, 2, 3}, '"none"': {3}, '{"project_ids": [1]}': {1, 2, 3},
                    '{"project_ids": [2]}': {3}}
