"""Upgrade-scope operational diagnostics never change raw clinical readiness.

Real temporary ledgers and worker selection SQL; all bodies and artifacts are
synthetic. No inference, configuration service or real source is called.
"""
import json
import sqlite3
import time

import pytest

import extract_llm
import health_report
import llm_availability
import mcs_setup
import mcs_util
from extract_testkit import _hash, _ledger, _message
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_upgrade_scope_selection import ALL, NEW_WORK, _seed


def _scope_config(tmp_path, monkeypatch, cfg):
    path = tmp_path / "synthetic-scope.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(mcs_util, "CONF_PATH", str(path))


def _old(db, mid, pid):
    return db.artifact_add(
        extract_llm.KIND, '{"summary":"完全合成の過去結果です。"}',
        project_id=pid, message_id=mid, model=extract_llm.MODEL,
        meta={VERSION_KEY: CURRENT_VERSION, "hash": _hash(db, mid),
              "extract_version": extract_llm.EXTRACT_VERSION - 1})


@pytest.fixture
def down_lane(monkeypatch):
    monkeypatch.setattr(llm_availability, "llm_down_for_s", lambda *_a, **_k: 7200)
    monkeypatch.setattr(health_report, "prev_health", lambda: {})
    monkeypatch.setattr(health_report, "cloud_stop_reason", lambda *_a: None)
    monkeypatch.setattr(health_report, "transfer_review_holds", lambda *_a: [])


@pytest.mark.parametrize("scope,selected", [
    (None, ALL), ("all", ALL), ("none", NEW_WORK),
    ({"project_ids": [1]}, NEW_WORK | {1}),
    ({"project_ids": [2]}, NEW_WORK | {2, 7, 8}),
])
def test_diagnostic_counts_follow_real_selectors_without_changing_raw_readiness(
        tmp_path, monkeypatch, scope, selected):
    cfg = {} if scope is None else {"extract": {"upgrade_scope": scope}}
    _scope_config(tmp_path, monkeypatch, cfg)
    db = _ledger(tmp_path)
    try:
        _seed(db)
        before = list(db.db.iterdump())
        rows, _fresh = extract_llm._select_pending(db, 100, None, None, False)
        assert {row["message_id"] for row in rows} == selected
        assert {row[0] for row in db.db.execute(
            "SELECT m.message_id FROM messages m WHERE " + extract_llm.pending_pred(db.db))} == ALL
        queries = []
        db.db.set_trace_callback(queries.append)
        try:
            warnings = list(mcs_setup._extract_queue_warnings(db.db, cfg))
        finally:
            db.db.set_trace_callback(None)
        assert len([query for query in queries if query.startswith("SELECT COUNT(*),")]) == 1
        excluded = len(ALL - selected)
        if excluded:
            assert warnings == [
                f"AI解析の未完了は8件です（更新対象の待機{len(selected)}件・"
                f"更新対象外の過去世代{excluded}件）。"
                "更新対象外でも解析済みにはならず、患者コンテキストの準備判定は変わりません"]
        else:
            assert warnings == []
        # The same patient's old result cannot become clinical completion just
        # because no upgrade work for that patient is admitted.
        assert extract_llm.patient_extraction_complete(db, 1) is False
        assert extract_llm.patient_extraction_complete(db, 2) is False
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize("cfg,admitted", [
    ({}, True), ({"extract": {"upgrade_scope": "all"}}, True),
    ({"extract": {"upgrade_scope": "none"}}, False),
    ({"extract": {"upgrade_scope": {"project_ids": [2]}}}, False),
    ({"extract": {"upgrade_scope": {"project_ids": [1]}}}, True),
])
def test_local_lane_health_only_waits_for_admitted_old_work(
        tmp_path, down_lane, cfg, admitted):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1, "完全合成の経過記録です。", project_id=1)])
        _old(db, 1, 1)
        before = list(db.db.iterdump())
        queries = []
        db.db.set_trace_callback(queries.append)
        try:
            reasons = health_report.backlog_stalls(db.db, cfg, time.time())
        finally:
            db.db.set_trace_callback(None)
        assert ("local_llm_down" in reasons) is admitted
        probes = [query for query in queries if query.startswith("SELECT 1 FROM messages m WHERE")]
        assert len(probes) == 1 and probes[0].rstrip().endswith("LIMIT 1")
        assert extract_llm.patient_extraction_complete(db, 1) is False
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


def test_closed_scope_still_warns_for_new_work_and_semantic_due(tmp_path, down_lane):
    cfg = {"extract": {"upgrade_scope": "none"}}
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1, "完全合成の新しい投稿です。")])
        assert "local_llm_down" in health_report.backlog_stalls(db.db, cfg, time.time())
        _old(db, 1, 1)
        assert "local_llm_down" not in health_report.backlog_stalls(db.db, cfg, time.time())
        db.job_add("semantic", 1, 1)
        cfg["semantic"] = {"mode": "shadow", "project_ids": [1]}
        assert "local_llm_down" in health_report.backlog_stalls(db.db, cfg, time.time())
    finally:
        db.close()


@pytest.mark.parametrize("cfg", [
    None, {"extract": None}, {"extract": {"upgrade_scope": None}},
    {"extract": {"upgrade_scope": True}},
    {"extract": {"upgrade_scope": {"project_ids": []}}},
    {"extract": {"upgrade_scope": {"project_ids": [1, 1]}}},
])
def test_unknown_scope_diagnostics_do_not_report_zero_pending(tmp_path, cfg):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1, "完全合成の経過記録です。")])
        _old(db, 1, 1)
        assert list(mcs_setup._extract_queue_warnings(db.db, cfg)) == [
            "AI解析の更新対象を確認できません。解析設定を確認してください"]
        assert extract_llm.patient_extraction_complete(db, 1) is False
    finally:
        db.close()


def test_invalid_scope_health_is_unknown_instead_of_an_idle_lane(tmp_path, down_lane):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1, "完全合成の経過記録です。")])
        _old(db, 1, 1)
        cfg = {"extract": {"upgrade_scope": True}}
        with pytest.raises(ValueError, match="^extract_upgrade_scope_unverified$"):
            health_report.backlog_stalls(db.db, cfg, time.time())
        report = health_report.build_health(db.db, {"errors": [], "notify": {}}, "ok", cfg=cfg)
        assert "stall_check_failed" in report["state_reasons"]
        assert report["stall_check"]["status"] == "unknown"
        assert report["stall_check"]["reason"] == "observation_unavailable"
    finally:
        db.close()


def test_unreadable_queue_does_not_become_an_empty_queue(tmp_path):
    db = _ledger(tmp_path)
    db.close()
    with pytest.raises(sqlite3.Error):
        list(mcs_setup._extract_queue_warnings(db.db, {}))


def test_malformed_prior_evidence_stays_pending_in_closed_scope(tmp_path, down_lane):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(1, "完全合成の経過記録です。")])
        old = _old(db, 1, 1)
        with db.db:
            db.db.execute("UPDATE artifacts SET meta='{' WHERE artifact_id=?", (old,))
        cfg = {"extract": {"upgrade_scope": "none"}}
        # Corrupt evidence cannot establish an earlier-generation result to
        # exclude, or establish raw clinical completion.
        assert list(mcs_setup._extract_queue_warnings(db.db, cfg)) == []
        assert "local_llm_down" in health_report.backlog_stalls(db.db, cfg, time.time())
        assert extract_llm.patient_extraction_complete(db, 1) is False
    finally:
        db.close()


def test_default_backlog_wording_remains_equal_and_closed_work_is_named(tmp_path):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid, f"完全合成の経過記録 {mid} です。")
                          for mid in range(1, 502)])
        for mid in range(1, 502):
            _old(db, mid, 1)
        expected = ["AI解析の待機は501件です。"
                    "解析処理（ai.mcs.extract-drainer*）は実行可能な新しい投稿から処理します。"
                    "待機件数と最近の進捗を確認してください"]
        assert list(mcs_setup._extract_queue_warnings(db.db, {})) == expected
        assert list(mcs_setup._extract_queue_warnings(db.db, {"extract": {}})) == expected
        assert list(mcs_setup._extract_queue_warnings(
            db.db, {"extract": {"upgrade_scope": "all"}})) == expected
        assert list(mcs_setup._extract_queue_warnings(
            db.db, {"extract": {"upgrade_scope": "none"}})) == [
                "AI解析の未完了は501件です（更新対象の待機0件・更新対象外の過去世代501件）。"
                "更新対象外でも解析済みにはならず、患者コンテキストの準備判定は変わりません"]
    finally:
        db.close()
