"""Synthetic checkpoint replay must preserve newest-source and paid-dispatch boundaries."""
import json

import pytest

import extract_llm as llm
import inference_dependency_loss as dependency_loss
from extract_testkit import _ledger, _message


@pytest.fixture
def source(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    ledger.save_messages([_message(body="本人は合成の記録を共有しました。" * 10)])
    monkeypatch.setattr(llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(llm, "_rerender_cards", lambda *args: None)
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(ledger.db, 1, 1)
    yield ledger, row
    ledger.close()


def _completed(summary="合成の経過"):
    return {"summary": summary, "_quality_result": {"state": "hint_gaps_resolved",
            "remaining_hint_candidates": 0}}


def _rows(ledger):
    return list(ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id"))


def test_identical_success_replay_keeps_checkpoint_and_rerenders(source, monkeypatch):
    ledger, row = source
    renders = []
    monkeypatch.setattr(llm, "_rerender_cards", lambda *args: renders.append(args[1:]))
    for _ in range(12):
        assert llm._persist_chunks(ledger, row, {0: _completed()})
    records = _rows(ledger)
    assert len(records) == 1 and len(renders) == 12
    assert llm._saved_chunk_state(ledger.db, row)[1][0]["summary"] == "合成の経過"
    assert json.loads(records[0]["meta"])["quality_status"] == "hint_gaps_resolved"


def test_deferred_later_piece_reuses_completed_piece_without_new_rows(source, monkeypatch):
    ledger, row = source
    size = 50
    assert len(llm.plan_chunks(row["body_text"], size)) > 1
    assert llm._persist_chunks(ledger, row, {0: _completed()}, chunk_size=size)
    calls = []
    monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: calls.append(1) or llm._DEFERRED)
    for tick in range(5):
        splits, saved = llm._saved_chunk_state(ledger.db, row, chunk_size=size)
        meta = {}
        result = llm.llm_extract(row["body_text"], chunk_size=size, chunks_in=saved,
            splits_in=splits, meta_out=meta,
            on_chunk=lambda key, value: llm._persist_chunks(ledger, row, {key: value},
                                                            chunk_size=size, splits=splits))
        assert result is llm._DEFERRED and meta["checkpoint_reuses"] == 1
        assert len(calls) == tick + 1 and len(_rows(ledger)) == 1


@pytest.mark.parametrize("status", ["pending", "started"])
def test_paid_dispatch_checkpoints_are_always_recorded(source, status):
    ledger, row = source
    for _ in range(2):
        assert llm._persist_chunks(ledger, row, {0: {"_quality_status": status}})
    assert len(_rows(ledger)) == 2
    assert llm._saved_chunk_state(ledger.db, row)[1][0]["_quality_status"] == status


def test_newer_different_or_invalid_checkpoint_never_looks_past_it(source):
    ledger, row = source
    assert llm._persist_chunks(ledger, row, {0: _completed("合成A")})
    assert llm._persist_chunks(ledger, row, {0: _completed("合成B")})
    assert llm._persist_chunks(ledger, row, {0: _completed("合成A")})
    assert len(_rows(ledger)) == 3
    assert llm._saved_chunk_state(ledger.db, row)[1][0]["summary"] == "合成A"
    latest = _rows(ledger)[-1]
    bad_meta = json.loads(latest["meta"])
    bad_meta["invalidated"] = True
    ledger.artifact_add("extract_llm_chunk", latest["content"], project_id=1,
                        message_id=1, model=latest["model"], meta=bad_meta)
    assert llm._persist_chunks(ledger, row, {0: _completed("合成A")})
    assert len(_rows(ledger)) == 5


def test_quality_or_model_changes_are_not_collapsed(source):
    ledger, row = source
    assert llm._persist_chunks(ledger, row, {0: _completed()})
    changed = _completed()
    changed["_quality_result"]["remaining_hint_candidates"] = 1
    assert llm._persist_chunks(ledger, row, {0: changed})
    row["_target"] = ("", "synthetic-other-model")
    assert llm._persist_chunks(ledger, row, {0: changed})
    assert len(_rows(ledger)) == 3


def test_newer_legacy_scope_and_plan_change_do_not_reuse_old_rows(source, monkeypatch):
    ledger, row = source
    assert llm._persist_chunks(ledger, row, {0: _completed()})
    latest = _rows(ledger)[-1]
    ledger.artifact_add("extract_llm_chunk", latest["content"], project_id=None,
                        message_id=1, model=latest["model"], meta=json.loads(latest["meta"]))
    assert llm._persist_chunks(ledger, row, {0: _completed()})
    monkeypatch.setattr(llm, "PLAN_VERSION", llm.PLAN_VERSION + 1)
    assert llm._persist_chunks(ledger, row, {0: _completed()})
    assert len(_rows(ledger)) == 4


def test_failed_changed_checkpoint_keeps_previous_transaction(source, monkeypatch):
    ledger, row = source
    assert llm._persist_chunks(ledger, row, {0: _completed("合成A")})
    before = [tuple(record) for record in _rows(ledger)]
    def failed_insert(*args, **kwargs):
        raise RuntimeError("synthetic write failure")
    monkeypatch.setattr(ledger, "artifact_add_tx", failed_insert)
    with pytest.raises(RuntimeError, match="synthetic write failure"):
        llm._persist_chunks(ledger, row, {0: _completed("合成B")})
    assert [tuple(record) for record in _rows(ledger)] == before


def test_changed_source_rejects_replay_without_modifying_checkpoints(source):
    ledger, row = source
    assert llm._persist_chunks(ledger, row, {0: _completed()})
    ledger.save_messages([_message(body="完全合成の別の記録です。")])
    assert not llm._persist_chunks(ledger, row, {0: _completed()})
    assert len(_rows(ledger)) == 1
