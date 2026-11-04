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
    monkeypatch.setattr(llm, "_probe_format", lambda **kwargs: "plain")
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(ledger.db, 1, 1)
    yield ledger, row
    ledger.close()


def _completed(summary="合成の経過"):
    return {"summary": summary, "_quality_result": {"state": "hint_gaps_resolved",
            "remaining_hint_candidates": 0}}


def _rows(ledger):
    return list(ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id"))


def _source_with_unresolved_hint(ledger):
    body = "本人の体温は38.0度です。" + "合成の連絡です。" * 16
    ledger.save_messages([_message(body=body)])
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(ledger.db, 1, 1)
    return row


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


@pytest.mark.parametrize("status", ["unresolved", "dispatch_unknown", "skipped_budget"])
def test_deferred_later_piece_does_not_duplicate_replayed_unconfirmed_checkpoint(source, monkeypatch, status):
    ledger, _ = source
    row = _source_with_unresolved_hint(ledger)
    size = 50
    assert len(llm.plan_chunks(row["body_text"], size)) > 1
    checkpoint = {"_quality_result": {"state": status, "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, row, {0: checkpoint}, chunk_size=size)
    original = [tuple(record) for record in _rows(ledger)]
    lease = llm._claim(ledger, row)
    assert lease is not None
    calls = []
    monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: calls.append(1) or llm._DEFERRED)
    for tick in range(5):
        splits, saved = llm._saved_chunk_state(ledger.db, row, chunk_size=size)
        assert saved[0]["_quality_result"] == checkpoint["_quality_result"]
        meta = {}
        result = llm.llm_extract(row["body_text"], chunk_size=size, chunks_in=saved,
            splits_in=splits, meta_out=meta,
            on_chunk=lambda key, value: llm._persist_chunks(ledger, row, {key: value},
                                                            chunk_size=size, splits=splits),
            on_chunk_replay=lambda key, value: llm._persist_chunks(
                ledger, row, {key: value}, chunk_size=size, splits=splits, replay=True, lease=lease))
        assert result is llm._DEFERRED and meta["checkpoint_reuses"] == 1
        assert meta["completeness"]["0"] == checkpoint["_quality_result"]
        assert len(calls) == tick + 1
        assert [tuple(record) for record in _rows(ledger)] == original


def test_ordinary_callback_still_receives_unsaved_caller_chunk(source, monkeypatch):
    ledger, _ = source
    row = _source_with_unresolved_hint(ledger)
    size = 50
    checkpoint = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    assert not _rows(ledger)
    received = []
    monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: llm._DEFERRED)
    result = llm.llm_extract(row["body_text"], chunk_size=size, chunks_in={0: checkpoint},
                             on_chunk=lambda key, value: received.append((key, value)))
    assert result is llm._DEFERRED and received == [(0, checkpoint)]
    assert not _rows(ledger)


def test_replay_requires_matching_live_lease_and_does_not_commit_caller_transaction(source):
    ledger, row = source
    ledger.ensure_patient(1)
    checkpoint = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, row, {0: checkpoint})
    before = [tuple(record) for record in _rows(ledger)]
    lease = llm._claim(ledger, row)
    assert lease is not None
    assert ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-uncommitted' "
                             "WHERE project_id=1").rowcount == 1
    assert not llm._persist_chunks(ledger, row, {0: checkpoint}, replay=True, lease=lease + 1)
    assert llm._persist_chunks(ledger, row, {0: checkpoint}, replay=True, lease=lease)
    assert ledger.db.in_transaction and [tuple(record) for record in _rows(ledger)] == before
    ledger.db.rollback()
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_replay_checks_latest_row_and_preserves_changed_or_invalidated_evidence(source):
    ledger, row = source
    checkpoint = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, row, {0: checkpoint})
    lease = llm._claim(ledger, row)
    assert lease is not None
    old = _rows(ledger)[-1]
    bad = json.loads(old["meta"])
    bad["invalidated"] = True
    ledger.artifact_add("extract_llm_chunk", old["content"], project_id=1, message_id=1,
                        model=old["model"], meta=bad)
    assert llm._persist_chunks(ledger, row, {0: checkpoint}, replay=True, lease=lease)
    # The established reader ignores invalidated metadata and selects the
    # earlier valid key; replay must not manufacture a third row.
    assert len(_rows(ledger)) == 2
    assert llm._saved_chunk_state(ledger.db, row)[1][0]["_quality_result"] == checkpoint["_quality_result"]


def test_replay_does_not_skip_a_newer_different_status(source):
    ledger, row = source
    unknown = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    unresolved = {"_quality_result": {"state": "unresolved", "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, row, {0: unknown})
    lease = llm._claim(ledger, row)
    assert lease is not None
    assert llm._persist_chunks(ledger, row, {0: unresolved})
    assert not llm._persist_chunks(ledger, row, {0: unknown}, replay=True, lease=lease)
    assert [json.loads(record["meta"])["quality_status"] for record in _rows(ledger)] == [
        "dispatch_unknown", "unresolved"]
    assert llm._saved_chunk_state(ledger.db, row)[1][0]["_quality_result"] == unresolved["_quality_result"]


def test_replay_write_failure_rolls_back_only_its_savepoint(source, monkeypatch):
    ledger, row = source
    ledger.ensure_patient(1)
    checkpoint = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, row, {0: checkpoint})
    lease = llm._claim(ledger, row)
    assert lease is not None
    assert llm._persist_chunks(ledger, row, {0: {"summary": "別の記録"}})
    before = [tuple(record) for record in _rows(ledger)]
    assert ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-uncommitted' "
                             "WHERE project_id=1").rowcount == 1
    def fail_during_read(*args, **kwargs):
        assert ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-replay-only' "
                                 "WHERE project_id=1").rowcount == 1
        raise RuntimeError("synthetic replay failure")

    monkeypatch.setattr(llm, "_saved_chunk_state", fail_during_read)
    with pytest.raises(RuntimeError, match="synthetic replay failure"):
        llm._persist_chunks(ledger, row, {0: checkpoint}, replay=True, lease=lease)
    assert ledger.db.in_transaction and [tuple(record) for record in _rows(ledger)] == before
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] == "synthetic-uncommitted"
    ledger.db.rollback()
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_replay_accepts_its_key_when_another_chunk_was_written_later(source):
    ledger, row = source
    size = 50
    checkpoint = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, row, {0: checkpoint}, chunk_size=size)
    assert llm._persist_chunks(ledger, row, {1: _completed()}, chunk_size=size)
    before = [tuple(record) for record in _rows(ledger)]
    lease = llm._claim(ledger, row)
    assert lease is not None
    assert llm._persist_chunks(ledger, row, {0: checkpoint}, chunk_size=size, replay=True, lease=lease)
    assert [tuple(record) for record in _rows(ledger)] == before


def test_replay_rejects_a_child_after_its_parent_split_changes(source):
    ledger, _ = source
    ledger.save_messages([_message(body="本人合成記録" * 30)])
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(ledger.db, 1, 1)
    size = 50
    old_splits = {0: 25}
    old_piece = llm._chunk_piece(row["body_text"], size, "0:0", old_splits)
    new_piece = llm._chunk_piece(row["body_text"], size, "0:0", {0: 20})
    assert old_piece is not None and new_piece is not None and old_piece != new_piece
    checkpoint = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, row, {0: {}}, chunk_size=size, splits=old_splits, split_size=25)
    assert llm._persist_chunks(ledger, row, {"0:0": checkpoint}, chunk_size=size, splits=old_splits)
    assert llm._saved_chunk_state(ledger.db, row, chunk_size=size)[1]["0:0"] == checkpoint
    lease = llm._claim(ledger, row)
    assert lease is not None
    assert llm._persist_chunks(ledger, row, {0: {}}, chunk_size=size, splits={0: 20}, split_size=20)
    before = [tuple(record) for record in _rows(ledger)]
    assert not llm._persist_chunks(ledger, row, {"0:0": checkpoint}, chunk_size=size,
                                   splits=old_splits, replay=True, lease=lease)
    assert [tuple(record) for record in _rows(ledger)] == before
    current_splits, current_saved = llm._saved_chunk_state(ledger.db, row, chunk_size=size)
    assert current_splits[0] == 20 and "0:0" not in current_saved


@pytest.mark.parametrize("workers", [1, 2])
def test_run_pending_reuses_unconfirmed_checkpoint_without_writing_it_again(source, monkeypatch, workers):
    ledger, _ = source
    first = _source_with_unresolved_hint(ledger)
    ledger.save_messages([_message(mid=2, body=first["body_text"])])
    monkeypatch.setattr(llm, "_CHUNK_SIZE", 50)
    monkeypatch.setattr(llm, "load_config", lambda: {
        "extract": {"patient_state_context": False, "prefilter": False},
        "local_llm": {"model": "synthetic-model"}})
    pending, _ = llm._select_pending(ledger, 2, None, None, False)
    jobs, skipped, blocked = llm._prepare_jobs(ledger, pending, llm._resolved_llm(), False, allow_cloud=False)
    assert len(jobs) == 2 and not skipped and not blocked
    checkpoint = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    for row, context, _, _, _ in jobs:
        assert len(llm.plan_chunks(row["body_text"], row["_chunk_size"])) > 1
        assert llm._persist_chunks(ledger, row, {0: checkpoint}, context, chunk_size=row["_chunk_size"])
    before = [tuple(record) for record in _rows(ledger)]
    calls = []
    monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: calls.append(1) or llm._DEFERRED)
    for attempt in range(2):
        result = llm.run_pending(ledger, limit=2, budget_s=200, workers=workers)
        assert result["deferred"] == 2 and len(calls) == 2 * (attempt + 1)
        assert [tuple(record) for record in _rows(ledger)] == before


def test_claim_refreshes_a_checkpoint_advanced_after_preparation(source, monkeypatch):
    ledger, _ = source
    monkeypatch.setattr(llm, "load_config", lambda: {"extract": {"prefilter": False},
                                                    "local_llm": {"model": "synthetic-model"}})
    pending, _ = llm._select_pending(ledger, 1, None, None, False)
    jobs, skipped, blocked = llm._prepare_jobs(ledger, pending, llm._resolved_llm(), False, allow_cloud=False)
    assert len(jobs) == 1 and not skipped and not blocked
    row, context, _, _, _ = jobs[0]
    assert llm._persist_chunks(ledger, row, {0: _completed("合成A")}, context,
                               chunk_size=row["_chunk_size"])
    original_claim = llm._claim
    advanced = []

    def claim_after_other_owner(current_ledger, current_row, *args, **kwargs):
        if not advanced:
            assert llm._persist_chunks(current_ledger, current_row, {0: _completed("合成B")},
                                       current_row["_context"], chunk_size=row["_chunk_size"],
                                       splits=row["_splits"])
            advanced.append(True)
        return original_claim(current_ledger, current_row, *args, **kwargs)

    monkeypatch.setattr(llm, "_claim", claim_after_other_owner)
    monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: pytest.fail("cached source was resent"))
    result = llm.run_pending(ledger, limit=1, budget_s=200)
    assert advanced == [True] and result["done"] == 1
    [final] = ledger.db.execute("SELECT content FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchall()
    assert json.loads(final[0])["summary"] == "合成B"


@pytest.mark.parametrize("workers", [1, 2])
def test_newer_checkpoint_during_replay_defers_before_final_commit(source, monkeypatch, workers):
    ledger, first = source
    if workers == 2:
        ledger.save_messages([_message(mid=2, body=first["body_text"])])
    monkeypatch.setattr(llm, "load_config", lambda: {"extract": {"prefilter": False},
                                                    "local_llm": {"model": "synthetic-model"}})
    pending, _ = llm._select_pending(ledger, workers, None, None, False)
    jobs, skipped, blocked = llm._prepare_jobs(ledger, pending, llm._resolved_llm(), False, allow_cloud=False)
    assert len(jobs) == workers and not skipped and not blocked
    for row, context, _, _, _ in jobs:
        assert llm._persist_chunks(ledger, row, {0: _completed("合成A")}, context,
                                   chunk_size=row["_chunk_size"])
    original_persist = llm._persist_chunks
    observed = []

    def advance_before_replay(current_ledger, row, chunks, context=None, **kwargs):
        if kwargs.get("replay") and row["message_id"] == 1 and not observed:
            assert original_persist(current_ledger, row, {0: _completed("合成B")}, context,
                                    chunk_size=kwargs["chunk_size"], splits=kwargs["splits"],
                                    lease=kwargs["lease"])
            observed.append("advanced")
        result = original_persist(current_ledger, row, chunks, context, **kwargs)
        if kwargs.get("replay") and row["message_id"] == 1:
            observed.append("accepted" if result else "deferred")
        return result

    monkeypatch.setattr(llm, "_persist_chunks", advance_before_replay)
    monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: pytest.fail("cached source was resent"))
    result = llm.run_pending(ledger, limit=workers, budget_s=200, workers=workers)
    assert observed == ["advanced", "deferred"] and result["deferred"] >= 1
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchone()
    current = ledger.db.execute("SELECT content FROM artifacts WHERE kind='extract_llm_chunk' "
                                "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert json.loads(current[0])["summary"] == "合成B"


def test_repeated_real_dispatch_with_unresolved_result_retains_both_attempts(source, monkeypatch):
    ledger, _ = source
    body = "本人に発熱と咳があります。本人の血圧120/80です。"
    ledger.save_messages([_message(body=body)])
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(ledger.db, 1, 1)
    model_result = {"symptoms": [
        {"text": "発熱", "status": "ongoing", "subject": "patient", "evidence": "発熱"},
        {"text": "咳", "status": "ongoing", "subject": "patient", "evidence": "咳"}]}
    calls = []
    monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: calls.append(1) or model_result)
    hints = llm._rule_hints({"body_text": body, "posted_at": row["posted_at"]})
    for attempt in range(2):
        meta = {}
        result = llm.llm_extract(body, posted_at=row["posted_at"], hints=hints, meta_out=meta,
                                 on_chunk=lambda key, value: llm._persist_chunks(ledger, row, {key: value}))
        assert result is not None and result is not llm._DEFERRED
        assert meta["completeness"]["0"]["state"] == "unresolved"
        assert len(calls) == 2 * (attempt + 1)
    statuses = [json.loads(record["meta"]).get("quality_status") for record in _rows(ledger)]
    assert statuses == ["pending", "started", "unresolved", "pending", "started", "unresolved"]


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


def test_source_aba_does_not_revive_or_overwrite_unconfirmed_checkpoint(source):
    ledger, old = source
    body = old["body_text"]
    unknown = {"_quality_result": {"state": "dispatch_unknown", "remaining_hint_candidates": 1}}
    assert llm._persist_chunks(ledger, old, {0: unknown})
    old_id = _rows(ledger)[0]["artifact_id"]
    lease = llm._claim(ledger, old)
    assert lease is not None
    ledger.save_messages([_message(body="本人の別の合成記録です。")])
    ledger.save_messages([_message(body=body)])
    fresh = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    fresh["_source_binding"] = dependency_loss.source_binding(ledger.db, 1, 1)
    assert fresh["content_hash"] == old["content_hash"]
    assert fresh["_source_binding"] != old["_source_binding"]
    assert not llm._persist_chunks(ledger, old, {0: unknown})
    assert not llm._persist_chunks(ledger, old, {0: unknown}, replay=True, lease=lease)
    assert llm._saved_chunk_state(ledger.db, fresh) == ({}, {})
    assert llm._persist_chunks(ledger, fresh, {0: unknown})
    assert llm._saved_chunk_state(ledger.db, fresh)[1][0]["_quality_result"] == unknown["_quality_result"]
    assert old_id in {record["artifact_id"] for record in _rows(ledger)}
