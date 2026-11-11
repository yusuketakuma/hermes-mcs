"""Wholly synthetic reproduction of a frozen reference/checkpoint retry loop."""
import extract_llm_carry
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import json

import pytest

import extract_llm as worker
import extraction_context as frames
from inference_dependency_loss import source_binding
from extract_testkit import _ledger
from test_extraction_context import seed, target


@pytest.fixture
def checkpointed(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(worker, "_writer_current", lambda **kwargs: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', worker._writer_current)
    monkeypatch.setattr(worker, "_rerender_cards", lambda *args: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', worker._rerender_cards)
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
        yield ledger, row, frame, attempt, lease, value, antecedent
    finally:
        ledger.close()


@pytest.mark.parametrize("stale", [False, True])
def test_pending_checkpoint_never_reenters_commit_with_a_stale_reference(checkpointed, stale):
    ledger, row, frame, attempt, lease, value, antecedent = checkpointed
    context = frame["text"]
    row["_commit_reject_reason"] = "commit_reference"
    if not stale:
        assert worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=True) == frame
        assert worker._commit_extraction_atomic(
            ledger, row, json.dumps(value), context, None, None, None, (0,), check_current=True) == "written"
        assert ledger.artifacts(worker.KIND, message_id=row["message_id"])
        assert "_commit_reject_reason" not in row
        return
    with ledger.db:
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


@pytest.mark.parametrize("boundary,reason", [
    ("transaction", "transaction"), ("owner", "source_or_owner"),
    ("conflict", "checkpoint"), ("missing_checkpoint", "checkpoint"),
    ("layout", "checkpoint"), ("prior", "prior_changed"), ("pinned_prior", "prior_changed"),
    ("completeness", "completeness_coverage"), ("context", "context"),
    ("reference", "reference"), ("medication", "medication"),
])
def test_commit_rejection_retains_only_fixed_diagnostic_and_preserves_rows(checkpointed, monkeypatch, boundary, reason):
    ledger, row, frame, _attempt, _lease, value, antecedent = checkpointed
    integrity, prior = None, None
    if boundary == "transaction":
        ledger.db.execute("BEGIN IMMEDIATE")
    elif boundary == "owner":
        row["_claim_token"] = "synthetic-other-owner"
    elif boundary == "conflict":
        row["_checkpoint_conflict"] = True
    elif boundary == "missing_checkpoint":
        row.pop("_checkpoint_identity")
    elif boundary == "layout":
        row["_checkpoint_layout"] = ("synthetic-different-layout",)
    elif boundary == "prior":
        prior = ()
    elif boundary == "pinned_prior":
        row["_expected_prior"] = ()
    elif boundary == "completeness":
        integrity = {"completeness_chunks": 1, "completeness": {
            "0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 999}}}
    elif boundary == "context":
        row["_input_context"] = "synthetic-different-context"
    elif boundary == "reference":
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (antecedent,))
    elif boundary == "medication":
        monkeypatch.setattr(worker, "_medication_reference_current", lambda *a, **k: False)
        monkeypatch.setattr(extract_llm_context, '_medication_reference_current', worker._medication_reference_current)
        monkeypatch.setattr(extract_llm_prepare, '_medication_reference_current', worker._medication_reference_current)
        monkeypatch.setattr(extract_llm_progress, '_medication_reference_current', worker._medication_reference_current)
        monkeypatch.setattr(extract_llm_store, '_medication_reference_current', worker._medication_reference_current)
    before = [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    assert worker._commit_extraction_atomic(ledger, row, json.dumps(value), frame["text"],
        integrity, None, None, (0,), check_current=True, expected_prior=prior) == "rejected"
    assert row["_commit_reject_reason"] == "commit_" + reason
    assert ledger.db.in_transaction is (boundary == "transaction")
    assert [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before
    ledger.db.rollback()


def test_minimal_row_rejection_diagnostic_reaches_original_caller(checkpointed):
    ledger, row, frame, _attempt, _lease, value, antecedent = checkpointed
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (antecedent,))
    minimal = {key: row[key] for key in ("message_id", "content_hash", "_source_binding", "_target", "_frame")}
    with ledger.db:
        ledger.db.execute("BEGIN IMMEDIATE")
        assert worker._replace_current_tx(ledger, minimal, json.dumps(value)) == "rejected"
    assert minimal["_commit_reject_reason"] == "commit_reference"


def test_unknown_commit_diagnostic_never_retains_arbitrary_data():
    row = {}
    assert worker._commit_rejected(row, "synthetic-private-detail /synthetic/path") == "rejected"
    assert row == {"_commit_reject_reason": "commit_rejected"}


@pytest.mark.parametrize("change", ["content", "meta", "model", "new_artifact"])
def test_changed_prior_blocks_dispatch_and_checkpoint_without_consuming_evidence(checkpointed, change):
    import time
    import extract_llm_qc
    from inference_version import CURRENT_VERSION, VERSION_KEY

    ledger, row, frame, _attempt, lease, value, _antecedent = checkpointed
    meta = {VERSION_KEY: CURRENT_VERSION, "hash": row["content_hash"],
            "source_binding": row["_source_binding"], "extract_version": worker.EXTRACT_VERSION - 1}
    aid = ledger.artifact_add(worker.KIND, json.dumps(value), project_id=row["project_id"],
                              message_id=row["message_id"], model="synthetic-model", meta=meta)
    prior = ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
    row["_expected_prior"] = extract_llm_qc._prior_identity(prior)
    assert extract_llm_context._extraction_dispatch_current(ledger, row, lease, time.monotonic() + 60)
    if change == "new_artifact":
        ledger.artifact_add(worker.KIND, prior["content"], project_id=row["project_id"],
                            message_id=row["message_id"], model=prior["model"], meta=meta)
    else:
        changed = (json.dumps({"summary": "別の合成結果"}) if change == "content" else
                   json.dumps({**meta, "synthetic_revision": 2}) if change == "meta" else "synthetic-new-model")
        with ledger.db:
            ledger.db.execute(f"UPDATE artifacts SET {change}=? WHERE artifact_id=?", (changed, aid))
    before = [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
    assert not extract_llm_context._extraction_dispatch_current(ledger, row, lease, time.monotonic() + 60)
    assert not worker._persist_chunks(ledger, row, {0: value}, frame["text"],
        chunk_size=row["_chunk_size"], lease=lease, guard=row["_checkpoint_guard"])
    assert [tuple(saved) for saved in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")] == before
    assert not ledger.db.in_transaction


def _pin_synthetic_prior(ledger, row):
    prior_content = {"meds": [{"name": "薬A", "subject": "family", "negated": True,
                               "evidence": "残薬"}]}
    meta = {"hash": row["content_hash"], "source_binding": row["_source_binding"],
            "extract_version": worker.EXTRACT_VERSION - 1}
    aid = ledger.artifact_add(worker.KIND, json.dumps(prior_content, ensure_ascii=False),
        project_id=row["project_id"], message_id=row["message_id"], meta=meta)
    prior = dict(ledger.db.execute(
        "SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone())
    row["_protected_prior"] = prior
    row["_expected_prior"] = extract_llm_qc._prior_identity(prior)
    row["_protected_prior_ref"] = extract_llm_qc._protected_prior_reference(
        prior, row["_source_binding"])
    return aid


def test_protected_prior_reference_rides_new_checkpoint_proof(checkpointed):
    ledger, row, frame, _attempt, lease, value, _antecedent = checkpointed
    metas = [json.loads(saved[0]) for saved in ledger.db.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' AND message_id=?",
        (row["message_id"],))]
    assert metas and all("protected_prior_ref" not in meta for meta in metas)
    _pin_synthetic_prior(ledger, row)
    ref = row["_protected_prior_ref"]
    leaf = worker._EvidenceLeaf(dict(value), worker._provider_evidence(["local"], True))
    assert worker._persist_chunks(ledger, row, {0: leaf}, frame["text"],
        chunk_size=row["_chunk_size"], lease=lease, guard=row["_checkpoint_guard"])
    metas = [json.loads(saved[0]) for saved in ledger.db.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' AND message_id=? ORDER BY artifact_id",
        (row["message_id"],))]
    assert "protected_prior_ref" not in metas[0]
    assert metas[-1]["protected_prior_ref"] == ref
    assert metas[-1]["provider_leaf"]["protected_prior_ref"] == ref
    _splits, saved = worker._saved_chunk_state(ledger.db, row, frame["text"],
                                             chunk_size=row["_chunk_size"])
    assert 0 in saved


def test_protected_prior_reference_flows_into_inline_proof_v2(checkpointed):
    ledger, row, frame, _attempt, lease, value, _antecedent = checkpointed
    aid = _pin_synthetic_prior(ledger, row)
    leaf = worker._EvidenceLeaf(dict(value), worker._provider_evidence(["local"], True))
    assert worker._persist_chunks(ledger, row, {0: leaf}, frame["text"],
        chunk_size=row["_chunk_size"], lease=lease, guard=row["_checkpoint_guard"])
    proof = extract_llm_proofs._extract_inline_proof(ledger.db, row, frame["text"], (0,))
    assert proof["version"] == 2
    assert set(proof["protected_prior"]) == {
        "artifact_id", "kind", "project_id", "message_id", "content", "model", "meta"}
    assert proof["protected_prior"]["artifact_id"] == aid
    _splits, saved = worker._saved_chunk_state(ledger.db, row, frame["text"],
                                             chunk_size=row["_chunk_size"])
    leaf_doc = {key: item for key, item in saved[0].items()
                if key not in ("_quality_result", "_provider_leaf")}
    document = {**leaf_doc, "_model": extract_llm_proofs._actual_model(row)}
    assert extract_llm_proofs._verify_inline_extract_proof(
        ledger.db, row, frame["text"], document, proof)
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (aid,))
    assert extract_llm_proofs._verify_inline_extract_proof(
        ledger.db, row, frame["text"], document, proof)
    tampered = json.loads(json.dumps(proof))
    tampered["protected_prior"]["artifact_id"] = aid + 1000
    assert not extract_llm_proofs._verify_inline_extract_proof(
        ledger.db, row, frame["text"], document, tampered)
    tampered = json.loads(json.dumps(proof))
    for proof_row in tampered["rows"]:
        row_meta = json.loads(proof_row["meta"])
        row_meta["protected_prior_ref"]["artifact_id"] = aid + 1000
        proof_row["meta"] = json.dumps(row_meta, ensure_ascii=False)
    assert not extract_llm_proofs._verify_inline_extract_proof(
        ledger.db, row, frame["text"], document, tampered)
    tampered = json.loads(json.dumps(proof))
    tampered["version"] = 3
    assert not extract_llm_proofs._verify_inline_extract_proof(
        ledger.db, row, frame["text"], document, tampered)
