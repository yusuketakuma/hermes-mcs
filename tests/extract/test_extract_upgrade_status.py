"""Generation progress distinguishes queued, retained, failed and genuinely completed rows."""
import json
import sqlite3
import sys
import time

import pytest

import extract_llm as llm
from extract_testkit import _hash, _ledger, _message
from mcs_queries import V4_PROJECTION_KIND


@pytest.mark.parametrize("state", ["missing", "stale", "old", "retry", "backoff", "leased", "exhausted", "poison"])
def test_queue_progress_uses_the_actual_retry_budget_read_only(tmp_path, state):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="合成の経過を報告します。")])
        row = dict(db.db.execute("SELECT * FROM messages").fetchone())
        if state not in ("missing", "leased"):
            meta = {"hash": "stale" if state == "stale" else _hash(db),
                    "extract_version": llm.EXTRACT_VERSION - (state == "old")}
            if state in ("retry", "backoff", "exhausted"):
                meta.update(error=True, attempts=5 if state == "exhausted" else 1,
                            next_try=time.time() + (3600 if state == "backoff" else -1))
            aid = db.artifact_add(llm.KIND, '{"summary":"合成の旧結果"}', project_id=1, message_id=1, meta=meta)
            if state == "poison":
                with db.db:
                    db.db.execute("UPDATE artifacts SET meta='invalid' WHERE artifact_id=?", (aid,))
        if state == "leased":
            assert llm._claim(db, row) is not None
        db.db.execute("PRAGMA query_only=ON")
        progress = llm.progress_for_message(db.db, row, model=llm.MODEL)
        attention = state in ("exhausted", "poison")
        assert progress["state"] == ("attention" if attention else "processing")
        status = llm.upgrade_status(db.db, model=llm.MODEL)
        key = "failed" if state == "exhausted" else "needs_review" if state == "poison" else "pending"
        assert status[key] == 1 and status["total"] == 1 and status["done_clean"] == 0
        with pytest.raises(sqlite3.OperationalError):
            db.db.execute("DELETE FROM artifacts")
    finally:
        db.close()


def test_new_generation_settles_once_and_projection_is_a_disjoint_population(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    calls = []
    monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: calls.append(1) or {"summary": "合成の新結果"})
    try:
        db.save_messages([_message(mid=1, body="合成の報告です。"), _message(mid=2, body="投影側の合成報告です。")])
        for mid in (1, 2):
            db.artifact_add(llm.KIND, '{"summary":"合成の旧結果"}', project_id=1, message_id=mid,
                meta={"hash": _hash(db, mid), "extract_version": llm.EXTRACT_VERSION - 1})
        db.artifact_add(V4_PROJECTION_KIND, '{"facts":[]}', project_id=1, message_id=2,
            meta={"hash": _hash(db, 2), "engine_version": 4, "projection_version": 5})
        before = llm.upgrade_status(db.db, model=llm.MODEL)
        assert before["pending"] == before["projection_shadowed"] == 1
        assert llm.run_pending(db, limit=3, budget_s=10)["done"] == 1
        assert llm.run_pending(db, limit=3, budget_s=10)["selected"] == 0 and calls == [1]
        after = llm.upgrade_status(db.db, model=llm.MODEL)
        assert after == {"target_version": llm.EXTRACT_VERSION, "done_clean": 1, "held_prior": 0,
                         "needs_review": 0, "failed": 0, "pending": 0, "projection_shadowed": 1, "total": 2}
        meta = json.loads(db.artifacts(llm.KIND, message_id=1)[0]["meta"])
        assert meta["integrity"]["validation"] == {"items_dropped": 0, "evidence_dropped": 0}
    finally:
        db.close()


@pytest.mark.parametrize("validation", [None, [], {"items_dropped": True, "evidence_dropped": 0},
    {"items_dropped": 1, "evidence_dropped": 0}, {"items_dropped": 0}])
def test_malformed_or_positive_validation_counts_never_look_clean(tmp_path, monkeypatch, validation):
    db = _ledger(tmp_path)
    monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: {"summary": "合成の結果"})
    try:
        db.save_messages([_message(body="合成の報告です。")])
        assert llm.run_pending(db, limit=3, budget_s=10)["done"] == 1
        artifact = db.artifacts(llm.KIND)[0]
        meta = json.loads(artifact["meta"])
        meta["integrity"]["validation"] = validation
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), artifact["artifact_id"]))
        status = llm.upgrade_status(db.db, model=llm.MODEL)
        assert status["done_clean"] == 0 and status["needs_review"] == 1
    finally:
        db.close()


def test_status_cli_is_read_only_and_never_starts_the_extractor(tmp_path, monkeypatch, capsys):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="SOURCE_CANARY 合成の本文")])
        path = db.db.execute("PRAGMA database_list").fetchone()[2]
        before = db.db.total_changes
        monkeypatch.setattr(llm, "DB", path)
        monkeypatch.setattr(llm, "Ledger", lambda *a, **k: pytest.fail("write connection"))
        monkeypatch.setattr(llm, "run_pending", lambda *a, **k: pytest.fail("model execution"))
        monkeypatch.setattr(sys, "argv", ["extract_llm.py", "--upgrade-status"])
        assert llm._main() == 0
        output = capsys.readouterr().out
        assert json.loads(output)["pending"] == 1 and "CANARY" not in output
        assert db.db.total_changes == before
    finally:
        db.close()


def test_positive_drops_do_not_change_process_completion_but_are_not_clean_quality(tmp_path, monkeypatch):
    from extraction_audit import _quality
    db = _ledger(tmp_path)
    monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: {"summary": "合成の結果", "_items_dropped": 1})
    try:
        db.save_messages([_message(body="合成の報告です。")])
        assert llm.run_pending(db, limit=3, budget_s=10)["done"] == 1
        row = db.db.execute("SELECT * FROM messages").fetchone()
        artifact = db.artifacts(llm.KIND)[0]
        meta = json.loads(artifact["meta"])
        assert meta["integrity"]["validation"]["items_dropped"] == 1
        assert llm.progress_for_message(db.db, row, model=llm.MODEL)["state"] == "complete"
        assert llm.upgrade_status(db.db, model=llm.MODEL)["needs_review"] == 1
        assert _quality(meta, json.loads(artifact["content"])) == "attention"
    finally:
        db.close()
