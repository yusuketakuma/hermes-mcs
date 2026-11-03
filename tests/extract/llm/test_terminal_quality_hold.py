"""Isolated reproduction of a stale terminal claim; no production cause is inferred."""
import time

import pytest

import extract_llm as worker
import extraction_context as frames
from extract_testkit import _ledger, _message
from inference_dependency_loss import source_binding
from test_anthropic_runtime import routed as routed


@pytest.mark.parametrize("state", ["hint_gaps_resolved", "unresolved", "dispatch_unknown", "skipped_budget"])
def test_inconsistent_terminal_is_preserved_without_replay_or_dispatch(tmp_path, monkeypatch, state):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(worker, "_writer_current", lambda **kwargs: True)
    monkeypatch.setattr(worker, "_rerender_cards", lambda *args: None)
    try:
        ledger.save_messages([_message(body="確認お願いします。")])
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row.update(_source_binding=source_binding(ledger.db, 1, 1), _target=("", "synthetic-model"),
                   qc_src=None, thin_src=None, human_src=None)
        frame = frames.prepare(ledger, row, model="synthetic-model", contract=worker._context_contract(),
            attempt={"qc_src": None, "thin_src": None, "human_src": None, "auto_retry": 0},
            enabled=False, base_context=None, version=worker.EXTRACT_VERSION)
        assert frame["refs"] == {}
        row["_frame"] = frame
        lease = worker._claim(ledger, row)
        assert lease is not None
        worker._capture_checkpoint_state(ledger.db, row, frame["text"])
        terminal = {"state": state, "remaining_hint_candidates": 0}
        leaf = worker._EvidenceLeaf({"summary": "合成の以前の候補", "_quality_result": terminal},
                                    worker._provider_evidence(["local"], True))
        assert worker._persist_chunks(ledger, row, {0: leaf}, frame["text"],
            chunk_size=row["_chunk_size"], lease=lease, guard=row["_checkpoint_guard"])
        saved = worker._capture_checkpoint_state(ledger.db, row, frame["text"])
        hints = worker._rule_hints(row)
        assert worker._hint_coverage(dict(leaf), hints, row["body_text"])["state"] == "unresolved"
        before = [tuple(value) for value in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]

        def no_call(*args, **kwargs):
            pytest.fail("a terminal cached leaf must not be silently resent")

        for _ in range(2):
            meta, chunks = {}, {}
            output = worker.llm_extract(row["body_text"], context=frame["text"], hints=hints,
                chunks_in=saved, chunks_out=chunks, meta_out=meta, provider=no_call,
                on_chunk_replay=no_call, on_chunk=no_call, deadline=time.monotonic() + 60)
            assert output is worker._DEFERRED and chunks == {}
            assert meta["failure_reason"] == "quality_checkpoint_unverified"
            assert meta["_quality_hold"] is True
            assert worker._CALL_NOTES.defer_reason == "quality_checkpoint_unverified"
            assert saved[0]["_quality_result"] == terminal
        assert [tuple(value) for value in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before
    finally:
        ledger.close()


@pytest.mark.parametrize("workers", [1, 2])
def test_quality_hold_uses_finite_claim_wait_and_next_pass_advances(routed, monkeypatch, tmp_path, workers):
    from test_anthropic_runtime import old
    from test_commit_hold_fairness import claim_rows

    ledger, _cfg, calls, reserves, results = routed
    old(ledger, mid=1, pid=2)
    old(ledger, mid=2, pid=2)
    from test_lane_slot_and_lock import _lock_stub
    real_extract = worker.llm_extract
    _lock_stub(monkeypatch, tmp_path, blocked_after_call=None)
    def cached_or_healthy(*args, **kwargs):
        return real_extract(*args, **kwargs) if kwargs.get('chunks_in') else {}
    monkeypatch.setattr(worker, 'llm_extract', cached_or_healthy)
    monkeypatch.setattr(worker, '_probe_format', lambda **kwargs: None)
    capture = worker._capture_checkpoint_state
    seen = []
    def terminal_checkpoint(db, row, context):
        saved = capture(db, row, context)
        if row['message_id'] == 2:
            seen.append(row)
            return {0: worker._EvidenceLeaf({
                "summary": "合成の保存候補",
                "_quality_result": {"state": "unresolved", "remaining_hint_candidates": 1}},
                worker._provider_evidence(["local"], True))}
        return saved
    monkeypatch.setattr(worker, '_capture_checkpoint_state', terminal_checkpoint)
    first = worker.run_pending(ledger, limit=1 if workers == 1 else 2, budget_s=180, workers=workers)
    assert first['done'] == (0 if workers == 1 else 1)
    assert first['failed'] == 0 and first['deferred'] == 1
    assert first['defer_reasons'] == {'quality_checkpoint_unverified': 1}
    assert len(seen) == 1
    waiting = claim_rows(ledger)
    assert len(waiting) == 1 and waiting[0]['message_id'] == 2
    now = worker.time.time()
    assert now < waiting[0]['next_try'] <= now + worker._PREPARE_HOLD_S
    assert not calls and not reserves and not results
    before = [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE message_id=2')]
    second = worker.run_pending(ledger, limit=1, budget_s=180, workers=workers)
    assert second['done'] == (1 if workers == 1 else 0)
    assert second['failed'] == second['deferred'] == 0
    assert len(seen) == 1 and not calls and not reserves and not results
    assert claim_rows(ledger) == waiting
    assert [tuple(row) for row in ledger.db.execute('SELECT * FROM artifacts WHERE message_id=2')] == before


@pytest.mark.parametrize('state,dropped', [
    ('hint_gaps_resolved', False), ('dispatch_unknown', False), ('skipped_budget', False),
    ('unresolved', True), ('hint_gaps_resolved', True),
])
def test_consistent_terminal_history_and_existing_drop_demotion_replay_without_call(state, dropped):
    body = "完全合成の記録。"
    leaf = {"summary": "合成の候補", "_quality_result": {
        "state": state, "remaining_hint_candidates": 0}}
    if dropped:
        leaf['_items_dropped'] = 1
    expected = {"state": "unresolved" if dropped else state, "remaining_hint_candidates": 0}
    callbacks = []
    def no_call(*args, **kwargs):
        pytest.fail("a consistent terminal must not dispatch again")
    meta = {}
    result = worker.llm_extract(body, hints=worker._rule_hints({'body_text': body}),
        chunks_in={0: leaf}, meta_out=meta, provider=no_call,
        on_chunk=lambda key, value: callbacks.append((key, value)),
        on_chunk_replay=lambda key, value: callbacks.append((key, value)),
        deadline=time.monotonic() + 60)
    assert isinstance(result, dict) and len(callbacks) == 1
    assert meta['completeness'] == {'0': expected}
    assert '_quality_hold' not in meta
    assert leaf['_quality_result']['state'] == state
