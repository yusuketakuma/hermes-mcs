"""Wholly synthetic reproduction of a frozen reference/checkpoint retry loop."""
import json

import pytest

import extract_llm as worker
import extraction_context as frames
from inference_dependency_loss import source_binding
from extract_testkit import _ledger
from test_extraction_context import seed, target


@pytest.mark.parametrize("stale", [False, True])
def test_pending_checkpoint_never_reenters_commit_with_a_stale_reference(tmp_path, monkeypatch, stale):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(worker, "_writer_current", lambda **kwargs: True)
    monkeypatch.setattr(worker, "_rerender_cards", lambda *args: None)
    try:
        antecedent = seed(ledger)
        row = target(ledger)
        row.update(_target=("", "synthetic-model"), qc_src=None, thin_src=None, human_src=None)
        attempt = {"qc_src": None, "thin_src": None, "human_src": None, "auto_retry": 0}
        frame = frames.prepare(ledger, row, model="synthetic-model", contract=worker._context_contract(),
            attempt=attempt, enabled=True, base_context=None, version=worker.EXTRACT_VERSION)
        assert frame["refs"]
        row["_frame"] = frame
        context = frame["text"]
        lease = worker._claim(ledger, row)
        assert lease is not None
        worker._capture_checkpoint_state(ledger.db, row, context)
        value = {"summary": "合成の対象結果"}
        assert worker._persist_chunks(ledger, row, {0: value}, context,
            chunk_size=row["_chunk_size"], lease=lease, guard=row["_checkpoint_guard"])
        if not stale:
            assert worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=True) == frame
            assert worker._commit_extraction_atomic(
                ledger, row, json.dumps(value), context, None, None, None, (0,), check_current=True) == "written"
            assert ledger.artifacts(worker.KIND, message_id=row["message_id"])
            return
        with ledger.db:
            # An earlier result can cease to support the old S-reference while
            # this target's own occurrence, frame and paid candidate stay put.
            ledger.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (antecedent,))
        reference = {**frame["refs"]["S1"], "token": "S1"}
        assert not frames.reference_current(ledger.db, row, reference, version=worker.EXTRACT_VERSION)
        before = [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
        for _ in range(2):
            assert worker._owns_current_source(ledger, row, lease)
            assert source_binding(ledger.db, row["project_id"], row["message_id"]) == row["_source_binding"]
            with pytest.raises(ValueError, match="^pending_context_reference_changed$"):
                worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=True)
        assert [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before
        assert not ledger.artifacts(worker.KIND, message_id=row["message_id"])
    finally:
        ledger.close()
