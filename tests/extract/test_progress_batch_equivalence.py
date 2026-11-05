"""Synthetic, same-snapshot equivalence for single and batched progress."""
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_runner
import extract_llm_source_contract
import extract_llm_store
import json
import sqlite3

import pytest

import extract_llm as llm
import inference_dependency_loss
import inference_version
from common_testkit import make_ledger
from extract_testkit import _message


MODEL = "synthetic-progress-model"
BODY = "本人の合成の状態を報告しました。"
QUALITY = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
           "completeness_chunks": 1,
           "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', llm._WRITER_STATE)
    monkeypatch.setattr(llm, "_rerender_cards", lambda *a, **k: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', llm._rerender_cards)
    db = make_ledger(tmp_path)
    db.ensure_patient(1)
    db.ensure_patient(2)
    yield db
    db.close()


def source(db, mid, *, pid=1, body=BODY, state="full", parent=None):
    db.save_messages([_message(mid=mid, project_id=pid, body=body, state=state, parent_id=parent)])
    return dict(db.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())


def final(db, row, *, model=MODEL, integrity=QUALITY):
    row = {**row, "_source_binding": inference_dependency_loss.source_binding(
        db.db, row["project_id"], row["message_id"]), "_target": ("", model)}
    assert llm._replace_current(db, row, json.dumps({"summary": "完全合成"}), integrity=integrity)
    return row


def compare(db, rows, *, model=MODEL):
    before = db.total_changes
    db.execute("SAVEPOINT synthetic_comparison")
    try:
        single = [llm.progress_for_message(db, row, model=model) for row in rows]
        batch = []
        for offset in range(0, len(rows), 64):
            batch.extend(llm.progress_for_messages(db, rows[offset:offset + 64], model=model))
        assert batch == single
        assert db.total_changes == before
        return batch
    finally:
        db.execute("RELEASE synthetic_comparison")


def test_mixed_current_pending_corrupt_deleted_models_and_order(ledger):
    complete = source(ledger, 1)
    final(ledger, complete)
    pending = source(ledger, 2)
    corrupt = source(ledger, 3)
    ledger.artifact_add("extract_llm", "{", project_id=1, message_id=3, model=MODEL,
                        meta={inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                              "hash": corrupt["content_hash"]})
    deleted = source(ledger, 4, body="", state="deleted")
    other = source(ledger, 5, pid=2)
    final(ledger, other, model="another-synthetic-model")
    rows = [pending, complete, deleted, corrupt, other, complete]
    result = compare(ledger.db, rows)
    assert result[1]["state"] == "complete"
    assert result[0]["state"] == "processing"
    assert result[2]["state"] == "attention"
    assert result[1] == result[-1]  # duplicates retain their input positions
    assert result[4]["state"] != "complete"


def test_changed_source_occurrence_and_unfinished_quality_do_not_complete(ledger):
    first = source(ledger, 1)
    final(ledger, first)
    second = source(ledger, 2)
    final(ledger, second, integrity={"completeness_chunks": 1,
                                   "completeness": {"0": {"state": "pending"}}})
    source(ledger, 1, body="本人の別の合成記録。")
    restored = source(ledger, 1)
    assert restored["content_hash"] == first["content_hash"]
    result = compare(ledger.db, [restored, second])
    assert result[0]["state"] != "complete"
    assert result[1]["state"] != "complete"


def test_qc_feedback_and_held_prior_match_single_reader(ledger):
    reviewed = source(ledger, 1)
    final(ledger, reviewed)
    prior = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchone()[0]
    ledger.artifact_add("extract_qc", json.dumps({"qc": "done", "items": [{"verdict": "NO_MATCH"}]}),
                        project_id=1, message_id=1,
                        meta={inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                              "hash": reviewed["content_hash"], "extract_version": llm.EXTRACT_VERSION,
                              "source_artifact_id": prior})
    held = source(ledger, 2)
    held_input = final(ledger, held)
    held_artifact = ledger.db.execute("SELECT artifact_id FROM artifacts WHERE kind='extract_llm' AND message_id=2").fetchone()
    with ledger.db:
        llm._record_hold_tx(ledger, held_input, held_artifact, "protected_loss")
    result = compare(ledger.db, [held, reviewed])
    assert result[0]["state"] == "attention"
    assert result[1]["state"] == "attention"


@pytest.mark.parametrize("attempts", [1, 5])
def test_retry_budget_and_future_retry_preserve_single_state(ledger, attempts):
    row = source(ledger, 1)
    ledger.artifact_add("extract_llm", json.dumps({"_error": True}), project_id=1, message_id=1, model=MODEL,
                        meta={inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
                              "hash": row["content_hash"], "extract_version": llm.EXTRACT_VERSION,
                              "error": True, "attempts": attempts, "next_try": 4102444800})
    result = compare(ledger.db, [row, row])
    assert result[0]["state"] == ("processing" if attempts < 5 else "attention")


def test_missing_frozen_frame_preserves_early_attention(ledger):
    row = source(ledger, 1)
    final(ledger, row)
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.context_snapshot',999999) "
                          "WHERE kind='extract_llm' AND message_id=1")
    result = compare(ledger.db, [row, row])
    assert result[0]["state"] == "attention"


def test_source_bound_checkpoint_counts_are_preserved(ledger):
    row = source(ledger, 1)
    row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, 1, 1)
    row["_target"] = ("", MODEL)
    assert llm._persist_chunks(ledger, row, {0: {"summary": "完全合成"}}, None)
    result = compare(ledger.db, [row, row])
    assert result[0]["completed"] == result[0]["total"] == 1
    assert result[0]["state"] == "processing"


def test_iterative_progress_counts_normal_split_tree_and_quality_pending(ledger, monkeypatch):
    row = source(ledger, 1, body='ABCDEFGH')
    splits = {0: 4, '0:0': 2}
    saved = {'0:0:0': {}, '0:0:1': {'_quality_status': 'pending'}, '0:1': {}}
    monkeypatch.setattr(llm, '_saved_chunk_state', lambda *a, **k: (splits, saved))
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_store, '_saved_chunk_state', llm._saved_chunk_state)
    result = compare(ledger.db, [row, row])
    assert all((r['completed'], r['total']) == (2, 3) for r in result)


def test_multiple_roots_hold_only_above_exact_node_limit(ledger, monkeypatch):
    row = source(ledger, 1, body='ABCDEFGH')
    # Two four-character roots: root 0 and its two children, then root 1.
    monkeypatch.setattr(llm, '_saved_chunk_size', lambda *a, **k: 4)
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_size', llm._saved_chunk_size)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_size', llm._saved_chunk_size)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_size', llm._saved_chunk_size)
    monkeypatch.setattr(llm, '_saved_chunk_state', lambda *a, **k: ({0: 2}, {}))
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_store, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(llm, '_PROGRESS_MAX_NODES', 4)
    monkeypatch.setattr(extract_llm_progress, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
    monkeypatch.setattr(extract_llm_proofs, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
    within = compare(ledger.db, [row, row])
    assert all((r['completed'], r['total']) == (0, 3) for r in within)
    monkeypatch.setattr(llm, '_PROGRESS_MAX_NODES', 3)
    monkeypatch.setattr(extract_llm_progress, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
    monkeypatch.setattr(extract_llm_proofs, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
    held = compare(ledger.db, [row, row])
    assert all(r['state'] == 'attention' and (r['completed'], r['total']) == (0, 0) for r in held)


@pytest.mark.parametrize('saved,expected_completed', [({}, 1), ({'_quality_status': 'pending'}, 0)])
def test_saved_parent_takes_precedence_over_split_for_progress(ledger, monkeypatch, saved, expected_completed):
    row = source(ledger, 1, body='ABCDEFGH')
    monkeypatch.setattr(llm, '_saved_chunk_state', lambda *a, **k: ({0: 4}, {0: saved}))
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_store, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(llm, '_PROGRESS_MAX_NODES', 1)
    monkeypatch.setattr(extract_llm_progress, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
    monkeypatch.setattr(extract_llm_proofs, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
    result = compare(ledger.db, [row, row])
    assert all((r['completed'], r['total']) == (expected_completed, 1) for r in result)


def test_empty_body_does_not_create_a_completed_empty_root(ledger, monkeypatch):
    row = source(ledger, 1, body='')
    monkeypatch.setattr(llm, '_saved_chunk_state', lambda *a, **k: pytest.fail('empty source loaded chunks'))
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_store, '_saved_chunk_state', llm._saved_chunk_state)
    result = compare(ledger.db, [row, row])
    assert all(r['state'] == 'attention' and (r['completed'], r['total']) == (0, 0) for r in result)


def test_deep_valid_split_path_counts_without_python_recursion(ledger, monkeypatch):
    depth = 1050
    body = 'A' * (depth + 1)
    row = source(ledger, 1, body=body)
    splits = {}
    key = 0
    for remaining in range(len(body) - 1, 0, -1):
        splits[key] = remaining
        key = f'{key}:0'
    # This path corresponds to actual source-preserving subdivisions; no
    # untrusted path bypasses the existing checkpoint loader in production.
    assert llm._chunk_piece(body, llm._CHUNK_SIZE, key, splits) == 'A'
    monkeypatch.setattr(llm, '_saved_chunk_state', lambda *a, **k: (splits, {}))
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_store, '_saved_chunk_state', llm._saved_chunk_state)
    results = compare(ledger.db, [row, row])
    assert all((r['completed'], r['total']) == (0, len(body)) for r in results)
    assert all(r['state'] != 'complete' for r in results)


def test_progress_node_limit_holds_even_a_valid_final_and_keeps_caller_transaction(ledger, monkeypatch):
    row = source(ledger, 1, body='ABCDEFGH')
    final(ledger, row)
    monkeypatch.setattr(llm, '_saved_chunk_state', lambda *a, **k: ({0: 4}, {}))
    monkeypatch.setattr(extract_llm_prepare, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_progress, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_proofs, '_saved_chunk_state', llm._saved_chunk_state)
    monkeypatch.setattr(extract_llm_store, '_saved_chunk_state', llm._saved_chunk_state)
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-change' WHERE project_id=1")
    try:
        monkeypatch.setattr(llm, '_PROGRESS_MAX_NODES', 3)
        monkeypatch.setattr(extract_llm_progress, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
        monkeypatch.setattr(extract_llm_proofs, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
        within = compare(ledger.db, [row, row])
        assert all(r['state'] == 'complete' and r['total'] == 2 for r in within)
        monkeypatch.setattr(llm, '_PROGRESS_MAX_NODES', 2)
        monkeypatch.setattr(extract_llm_progress, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
        monkeypatch.setattr(extract_llm_proofs, '_PROGRESS_MAX_NODES', llm._PROGRESS_MAX_NODES)
        held = compare(ledger.db, [row, row])
        assert all(r['state'] == 'attention' and (r['completed'], r['total']) == (0, 0) for r in held)
        assert ledger.db.in_transaction
    finally:
        ledger.db.rollback()
    assert ledger.db.execute('SELECT fetch_reason FROM patients WHERE project_id=1').fetchone()[0] is None


def test_thread_context_and_snapshot_match_for_ordered_replies(ledger):
    root = source(ledger, 1, body="本人の合成の前提です。")
    earlier = source(ledger, 2, body="本人の合成の先行返信です。", parent=1)
    later = source(ledger, 3, body="本人の合成の後続返信です。", parent=1)
    final(ledger, root)
    final(ledger, earlier)
    final(ledger, later)
    result = compare(ledger.db, [later, root, earlier])
    assert [item["state"] for item in result] == ["complete"] * 3


def test_wrong_project_and_absent_or_invalid_model_match_single_reader(ledger):
    row = source(ledger, 1)
    final(ledger, row)
    other_scope = {**row, "project_id": 2}
    compare(ledger.db, [other_scope, row, other_scope])
    for model in (None, "", "other-synthetic-model"):
        compare(ledger.db, [row, other_scope], model=model)


def test_borrowed_uncommitted_source_is_read_without_transaction_ownership(ledger):
    row = source(ledger, 1)
    final(ledger, row)
    ledger.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    changed = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    assert ledger.db.in_transaction
    compare(ledger.db, [changed])
    assert ledger.db.in_transaction
    ledger.db.rollback()
    assert ledger.db.execute("SELECT body_state FROM messages WHERE message_id=1").fetchone()[0] == "full"


def test_invalid_early_inputs_do_not_touch_inaccessible_artifact_schema(ledger):
    row = source(ledger, 1, body="", state="deleted")
    before = ledger.db.total_changes

    def authorizer(op, table, *_):
        return sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_READ and table == "artifacts" else sqlite3.SQLITE_OK

    ledger.db.set_authorizer(authorizer)
    try:
        assert llm.progress_for_messages(ledger.db, [row, row], model=MODEL) == [
            llm.progress_for_message(ledger.db, row, model=MODEL)] * 2
        assert ledger.db.total_changes == before
    finally:
        ledger.db.set_authorizer(None)


def test_missing_schema_preserves_error_and_does_not_end_borrowed_transaction():
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE unrelated (x INTEGER)")
        db.execute("INSERT INTO unrelated VALUES (1)")
        assert db.in_transaction
        row = {"body_state": "full", "body_text": BODY, "project_id": 1, "message_id": 1}
        with pytest.raises(sqlite3.OperationalError) as single:
            llm.progress_for_message(db, row, model=MODEL)
        with pytest.raises(sqlite3.OperationalError) as batch:
            llm.progress_for_messages(db, [row], model=MODEL)
        assert str(batch.value) == str(single.value)
        assert db.in_transaction
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0] == 0
    finally:
        db.close()


def test_paged_batch_and_single_agree_without_dropping_duplicates(ledger):
    rows = [source(ledger, mid) for mid in range(1, 71)]
    result = compare(ledger.db, [rows[0], *rows, rows[0]])
    assert len(result) == 72 and result[0] == result[-1]


@pytest.mark.parametrize("prefix", [0, 1, 64, 65])
def test_iterators_are_rejected_before_they_can_mutate_or_fail(ledger, prefix):
    row = source(ledger, 1)
    seen = []

    def unsafe():
        seen.append("started")
        for _ in range(prefix):
            yield row
        ledger.db.execute("UPDATE messages SET body_text='完全合成の書き換え' WHERE message_id=1")
        raise RuntimeError("synthetic-late-input-failure")

    assert not ledger.db.in_transaction
    with pytest.raises(TypeError, match="progress_batch_rows_invalid"):
        llm.progress_for_messages(ledger.db, unsafe(), model=MODEL)
    assert seen == [] and not ledger.db.in_transaction
    assert ledger.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == BODY


def test_reused_mutable_row_generator_is_rejected_before_advancing(ledger):
    shared = source(ledger, 1)
    seen = []

    def unsafe():
        for mid in range(1, 67):
            seen.append(mid)
            shared["message_id"] = mid
            yield shared

    with pytest.raises(TypeError, match="progress_batch_rows_invalid"):
        llm.progress_for_messages(ledger.db, unsafe(), model=MODEL)
    assert seen == [] and shared["message_id"] == 1


def test_materialized_shared_rows_are_copied_before_batch_probes(ledger, monkeypatch):
    shared = source(ledger, 1)
    final(ledger, shared)
    expected = llm.progress_for_message(ledger.db, shared, model=MODEL)
    probe = llm._batch_progress_probes
    called = []

    def mutate_after_entry(db, probes):
        if not called:
            called.append(True)
            shared["body_state"] = "deleted"
        return probe(db, probes)

    monkeypatch.setattr(llm, "_batch_progress_probes", mutate_after_entry)
    monkeypatch.setattr(extract_llm_progress, '_batch_progress_probes', llm._batch_progress_probes)
    assert llm.progress_for_messages(ledger.db, [shared] * 64, model=MODEL) == [expected] * 64
    assert called and shared["body_state"] == "deleted"


def test_rejected_writer_generator_does_not_commit_borrowed_transaction(ledger):
    row = source(ledger, 1)
    ledger.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    seen = []

    def unsafe():
        seen.append(True)
        ledger.db.execute("UPDATE messages SET body_text='完全合成の書き換え' WHERE message_id=1")
        yield row

    with pytest.raises(TypeError, match="progress_batch_rows_invalid"):
        llm.progress_for_messages(ledger.db, unsafe(), model=MODEL)
    assert not seen and ledger.db.in_transaction
    ledger.db.rollback()
    assert ledger.db.execute("SELECT fetch_state FROM patients WHERE project_id=1").fetchone()[0] != "incomplete"
    assert ledger.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == BODY


def test_unsupported_values_are_rejected_before_touching_a_borrowed_transaction(ledger):
    row = source(ledger, 1)
    ledger.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    assert ledger.db.in_transaction
    with pytest.raises(TypeError, match="progress_batch_row_invalid"):
        llm.progress_for_messages(ledger.db, [row, {**row, "unsupported": object()}], model=MODEL)
    assert ledger.db.in_transaction
    ledger.db.rollback()
    assert ledger.db.execute("SELECT fetch_state FROM patients WHERE project_id=1").fetchone()[0] != "incomplete"


def test_batch_accepts_sqlite_rows_and_tuple_preserving_duplicate_order(ledger):
    row = source(ledger, 1)
    final(ledger, row)
    stored = ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    expected = llm.progress_for_message(ledger.db, stored, model=MODEL)
    assert llm.progress_for_messages(ledger.db, (stored, stored), model=MODEL) == [expected, expected]


def test_batch_rejects_more_than_64_before_reading_or_opening_transaction(ledger):
    row = source(ledger, 1)
    assert not ledger.db.in_transaction
    with pytest.raises(ValueError, match="progress_batch_limit"):
        llm.progress_for_messages(ledger.db, [row] * 65, model=MODEL)
    assert not ledger.db.in_transaction


def test_model_subclass_cannot_write_from_inside_savepoint(ledger):
    row = source(ledger, 1)
    seen = []

    class UnsafeModel(str):
        def __bool__(self):
            seen.append(True)
            ledger.db.execute("UPDATE messages SET body_text='完全合成の書き換え' WHERE message_id=1")
            raise RuntimeError("synthetic-model-callback")

    assert not ledger.db.in_transaction
    with pytest.raises(TypeError, match="progress_batch_model_invalid"):
        llm.progress_for_messages(ledger.db, [row], model=UnsafeModel(MODEL))
    assert seen == [] and not ledger.db.in_transaction
    assert ledger.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == BODY


def test_failed_batch_preserves_earlier_single_decision_error(ledger, monkeypatch):
    rows = [source(ledger, 1), source(ledger, 2)]

    def failed_batch(_db, _probes):
        raise RuntimeError("synthetic-later-batch-error")

    def original_order(_db, probe):
        kind, _project_id, message_id = probe
        if message_id == 1 and kind == "binding":
            return "synthetic-source-binding"
        if message_id == 1 and kind == "queued":
            return False
        if message_id == 1 and kind == "held":
            raise ValueError("synthetic-earlier-held-error")
        raise RuntimeError("synthetic-later-row-error")

    monkeypatch.setattr(llm, "_batch_progress_probes", failed_batch)
    monkeypatch.setattr(extract_llm_progress, '_batch_progress_probes', llm._batch_progress_probes)
    monkeypatch.setattr(llm, "_progress_probe", original_order)
    monkeypatch.setattr(extract_llm_progress, '_progress_probe', llm._progress_probe)
    with pytest.raises(ValueError, match="synthetic-earlier-held-error"):
        llm.progress_for_messages(ledger.db, rows, model=MODEL)


def test_batch_only_error_is_not_hidden_when_single_replay_succeeds(ledger, monkeypatch):
    rows = [source(ledger, 1), source(ledger, 2)]

    def failed_batch(_db, _probes):
        raise RuntimeError("synthetic-batch-only-error")

    monkeypatch.setattr(llm, "_batch_progress_probes", failed_batch)
    monkeypatch.setattr(extract_llm_progress, '_batch_progress_probes', llm._batch_progress_probes)
    with pytest.raises(RuntimeError, match="synthetic-batch-only-error"):
        llm.progress_for_messages(ledger.db, rows, model=MODEL)


def test_progress_cleanup_keeps_borrowed_transaction_on_success_and_failure(ledger, monkeypatch):
    row = source(ledger, 1)
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-pending' WHERE project_id=1")
    monkeypatch.setattr(llm, "_progress_page", lambda *_: [{"state": "attention"}])
    monkeypatch.setattr(extract_llm_progress, '_progress_page', llm._progress_page)
    assert llm.progress_for_messages(ledger.db, [row], model=MODEL) == [{"state": "attention"}]
    error = ValueError("synthetic progress failure")

    def fail(*_args):
        raise error

    monkeypatch.setattr(llm, "_progress_page", fail)
    monkeypatch.setattr(extract_llm_progress, '_progress_page', llm._progress_page)
    with pytest.raises(ValueError) as caught:
        llm.progress_for_messages(ledger.db, [row], model=MODEL)
    assert caught.value is error and ledger.db.in_transaction
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] == "synthetic-pending"
    ledger.db.rollback()
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_progress_success_does_not_hide_release_failure(ledger, monkeypatch):
    row = source(ledger, 1)
    monkeypatch.setattr(llm, "_progress_page", lambda *_: [{"state": "attention"}])
    monkeypatch.setattr(extract_llm_progress, '_progress_page', llm._progress_page)
    ledger.db.set_authorizer(lambda action, arg1, arg2, *_: sqlite3.SQLITE_DENY
                             if action == sqlite3.SQLITE_SAVEPOINT and arg1 == "RELEASE"
                             and arg2 == "extract_progress_batch" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            llm.progress_for_messages(ledger.db, [row], model=MODEL)
    finally:
        ledger.db.set_authorizer(None)
        ledger.db.rollback()


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR | (3 << 8)])
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, sqlite3.DatabaseError])
def test_progress_auto_rollback_preserves_error_and_does_not_replay(ledger, monkeypatch, code, error_type):
    rows = [source(ledger, 1), source(ledger, 2)]
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-pending' WHERE project_id=1")
    error = error_type("synthetic SQLite failure")
    if code is not None:
        error.sqlite_errorcode = code
    replay = []

    def failed_batch(db, _probes):
        db.rollback()  # Simulate SQLite discarding the entire transaction.
        raise error

    monkeypatch.setattr(llm, "_batch_progress_probes", failed_batch)
    monkeypatch.setattr(extract_llm_progress, '_batch_progress_probes', llm._batch_progress_probes)
    monkeypatch.setattr(llm, "_progress_probe", lambda *_: replay.append(True))
    monkeypatch.setattr(extract_llm_progress, '_progress_probe', llm._progress_probe)
    with pytest.raises(error_type) as caught:
        llm.progress_for_messages(ledger.db, rows, model=MODEL)
    assert caught.value is error and getattr(caught.value, "sqlite_errorcode", None) == code
    assert not replay and not ledger.db.in_transaction
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_classified_sql_error_keeps_original_ordered_single_fallback(ledger, monkeypatch):
    rows = [source(ledger, 1), source(ledger, 2), source(ledger, 3)]
    grouped = sqlite3.OperationalError("synthetic grouped SQL error")
    grouped.sqlite_errorcode = sqlite3.SQLITE_ERROR
    earlier = ValueError("synthetic earlier ordered error")
    visited = []

    def steps(_db, row, *, model):
        yield ("binding", row["project_id"], row["message_id"])
        return {"state": "attention"}

    def failed_batch(*_args):
        raise grouped

    def probe(_db, item):
        visited.append(item[2])
        if item[2] == 2:
            raise earlier
        return "synthetic binding"

    monkeypatch.setattr(llm, "_progress_steps", steps)
    monkeypatch.setattr(extract_llm_progress, '_progress_steps', llm._progress_steps)
    monkeypatch.setattr(llm, "_batch_progress_probes", failed_batch)
    monkeypatch.setattr(extract_llm_progress, '_batch_progress_probes', llm._batch_progress_probes)
    monkeypatch.setattr(llm, "_progress_probe", probe)
    monkeypatch.setattr(extract_llm_progress, '_progress_probe', llm._progress_probe)
    with pytest.raises(ValueError) as caught:
        llm.progress_for_messages(ledger.db, rows, model=MODEL)
    assert caught.value is earlier and visited == [1, 2]


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_interrupted_batch_is_not_replayed(ledger, monkeypatch, code):
    rows = [source(ledger, 1), source(ledger, 2)]
    error = sqlite3.OperationalError("synthetic-interrupted")
    if code is not None:
        error.sqlite_errorcode = code
    replay = []

    def failed_batch(_db, _probes):
        raise error

    monkeypatch.setattr(llm, "_batch_progress_probes", failed_batch)
    monkeypatch.setattr(extract_llm_progress, '_batch_progress_probes', llm._batch_progress_probes)
    monkeypatch.setattr(llm, "_progress_probe", lambda *args: replay.append(True))
    monkeypatch.setattr(extract_llm_progress, '_progress_probe', llm._progress_probe)
    with pytest.raises(sqlite3.OperationalError, match="synthetic-interrupted"):
        llm.progress_for_messages(ledger.db, rows, model=MODEL)
    assert not replay


@pytest.mark.parametrize("code", [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
@pytest.mark.parametrize("stage", ["prepare", "resume"])
def test_interrupted_decision_stage_does_not_finish_earlier_rows(ledger, monkeypatch, code, stage):
    rows = [source(ledger, 1), source(ledger, 2)]
    error = sqlite3.OperationalError("synthetic-interrupted-decision")
    error.sqlite_errorcode = code
    replay = []

    def steps(_db, row, *, model):
        if row["message_id"] == 2 and stage == "prepare":
            raise error
        yield ("queued", row["project_id"], row["message_id"])
        if row["message_id"] == 2 and stage == "resume":
            raise error
        yield ("held", row["project_id"], row["message_id"])
        return {"state": "attention"}

    monkeypatch.setattr(llm, "_progress_steps", steps)
    monkeypatch.setattr(extract_llm_progress, '_progress_steps', llm._progress_steps)
    monkeypatch.setattr(llm, "_batch_progress_probes", lambda _db, probes: [False] * len(probes))
    monkeypatch.setattr(extract_llm_progress, '_batch_progress_probes', llm._batch_progress_probes)
    monkeypatch.setattr(llm, "_progress_probe", lambda *_args: replay.append(True))
    monkeypatch.setattr(extract_llm_progress, '_progress_probe', llm._progress_probe)
    with pytest.raises(sqlite3.OperationalError, match="synthetic-interrupted-decision") as caught:
        llm.progress_for_messages(ledger.db, rows, model=MODEL)
    assert caught.value is error and not replay
    assert not ledger.db.in_transaction


def test_batch_reduces_sql_round_trips_without_skipping_decisions(ledger):
    rows = [source(ledger, mid) for mid in range(1, 9)]
    for row in rows:
        final(ledger, row)
    statements = []

    def count(sql):
        if sql.lstrip().upper().startswith("SELECT"):
            statements.append(None)  # never retain source or expanded SQL

    ledger.db.set_trace_callback(count)
    try:
        single = [llm.progress_for_message(ledger.db, row, model=MODEL) for row in rows]
        single_count = len(statements)
        statements.clear()
        batch = llm.progress_for_messages(ledger.db, rows, model=MODEL)
        batch_count = len(statements)
    finally:
        ledger.db.set_trace_callback(None)
    assert batch == single and all(item["state"] == "complete" for item in batch)
    assert batch_count < single_count
