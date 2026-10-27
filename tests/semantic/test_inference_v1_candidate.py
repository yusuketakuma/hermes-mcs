"""Synthetic candidate and hold bindings; fabricated approval evidence is never release evidence."""
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

import extract_llm as llm
import inference_version as inference
import inference_dependency_loss as dependency_loss
import mcs_util
import semantic_candidate as candidate
import semantic_policy
import writer_lock
from candidate_testkit import approved_config
from extract_testkit import _ledger, _message


MODULES = ("inference_version", "inference_dependency_loss", "clinical_events", "rollup", "patient_context_state", "patient_state_context")


@pytest.fixture
def approved(tmp_path, monkeypatch):
    # Every approval file is fabricated test evidence in a private temporary HOME.
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    cfg = approved_config({"semantic": {"mode": "assist", "project_ids": [1],
        "daily_request_budget": 0, "fact_source": "canonical"}})
    report = cfg["semantic"]["fact_source_evidence"]
    identity = cfg["semantic"]["fact_source_approval"]
    receipt = tmp_path / "data" / "semantic_approvals" / (identity + ".json")
    assert candidate.promotion_problem(cfg, report) is None
    assert candidate.approval_matches(cfg, report, identity)
    assert not semantic_policy.semantic_config(cfg)[1]
    return cfg, report, identity, receipt, receipt.read_bytes()


def _reject_old_approval(approved):
    cfg, report, identity, receipt, raw = approved
    assert candidate.candidate_fingerprint(cfg) != report["candidate_fingerprint"]
    assert candidate.promotion_problem(cfg, report) == "candidate_mismatch"
    assert not candidate.approval_matches(cfg, report, identity)
    settings, errors = semantic_policy.semantic_config(cfg)
    assert errors and settings["fact_source"] == "legacy" and settings["mode"] == "off"
    assert receipt.read_bytes() == raw  # refusal never deletes or rewrites the human receipt


@pytest.mark.parametrize("owner,member", [
    ("inference_version", "read_version"),
    ("clinical_events", "validate_record"),
    ("rollup", "inference_coverage"),
    ("measurement_sources", "_labs"),
    ("measurement_timeline", "project_measurements"),
    ("intervention_schedule", "project"),
    ("semantic_drain", "_reprocess_patient_state"),
    ("patient_context_state", "project"),
    ("patient_state_context", "reference_current"),
    ("inference_dependency_loss", "read"),
])
def test_changed_definition_invalidates_new_worker_approval_without_blessing_old_worker(
        approved, monkeypatch, owner, member):
    cfg, report, identity, _, _ = approved
    module = importlib.import_module(owner)
    original = candidate.candidate_fingerprint(cfg)
    def replacement(*args, **kwargs):
        raise AssertionError("fingerprinting must not execute interpretation functions")
    replacement.__module__ = module.__name__
    monkeypatch.setattr(module, member, replacement)
    # An existing worker keeps its original code identity; it cannot claim a new evaluation.
    assert candidate.candidate_fingerprint(cfg) == original
    assert candidate.approval_matches(cfg, report, identity)
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)  # simulate a fresh loaded capture
    _reject_old_approval(approved)


@pytest.mark.parametrize("owner,constant,value", [
    ("inference_version", "CURRENT_VERSION", "1.1"),
    ("clinical_events", "SUBJECTS", ("patient", "family", "other", "unknown", "synthetic_subject")),
    ("rollup", "PERIOD_CHECK_VERSION", -123),
    ("measurement_sources", "_MAX_RECORDS", 401),
    ("intervention_schedule", "_ROLES", {"架空職種": "synthetic"}),
    ("patient_context_state", "_VALUE_KEYS", {"actual_dose", "synthetic_dimension"}),
    ("patient_state_context", "STATE_MAX", 601),
    ("inference_dependency_loss", "SCHEMA", "dependency-loss/synthetic-change"),
])
def test_meaning_constant_changes_invalidate_old_promotion_receipt(approved, monkeypatch, owner, constant, value):
    cfg = approved[0]
    module = importlib.import_module(owner)
    before = candidate.candidate_fingerprint(cfg)
    monkeypatch.setattr(module, constant, value)
    assert candidate.candidate_fingerprint(cfg) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    _reject_old_approval(approved)


def test_rollup_runtime_paths_do_not_revoke_semantically_identical_approval(approved, monkeypatch, tmp_path):
    import rollup
    cfg, report, identity, _, raw = approved
    before = candidate.candidate_fingerprint(cfg)
    monkeypatch.setattr(rollup, "HOME", str(tmp_path / "relocated-home"))
    monkeypatch.setattr(rollup, "DB", str(tmp_path / "relocated-home" / "synthetic.db"))
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint(cfg) == before
    assert candidate.promotion_problem(cfg, report) is None
    assert candidate.approval_matches(cfg, report, identity)
    assert approved[3].read_bytes() == raw


@pytest.mark.parametrize("owner", MODULES)
def test_source_drift_refuses_old_worker_lock_even_while_its_candidate_identity_is_frozen(
        approved, tmp_path, monkeypatch, owner):
    module = importlib.import_module(owner)
    tree = tmp_path / "synthetic-source"
    tree.mkdir()
    source = tree / (owner + ".py")
    source.write_bytes(Path(module.__file__).read_bytes())
    home = tmp_path / "synthetic-runtime"
    monkeypatch.setattr(writer_lock, "HOME", str(home))
    monkeypatch.setattr(writer_lock, "_MCS_ROOT", str(tree))
    state = writer_lock.updater_state()
    monkeypatch.setattr(llm, "_WRITER_STATE", state)
    if owner == "inference_version":
        monkeypatch.setattr(inference, "__file__", str(source))
        monkeypatch.setattr(llm, "_INFERENCE_SOURCE_STAT", source.stat().st_mtime_ns)
    acquire = writer_lock.acquire_run_lock
    monkeypatch.setattr(llm, "acquire_run_lock", lambda: acquire(str(home / "data" / "run.lock")))
    before = candidate.candidate_fingerprint(approved[0])
    with llm._write_lock(True, wait_s=1) as held:
        assert held
    # Only the temporary source copy changes; no repository file or real runtime is touched.
    source.write_bytes(source.read_bytes() + b"\nSYNTHETIC_INTERPRETATION_REVISION = 2\n")
    os.utime(source, ns=(state[1] + 1_000_000_000, state[1] + 1_000_000_000))
    assert candidate.candidate_fingerprint(approved[0]) == before
    assert writer_lock.updater_state() != state
    with llm._write_lock(True, wait_s=1) as held:
        assert not held
    if owner == "inference_version":
        assert not llm._writer_current()  # lockless checkpoints also reject version-source drift


@pytest.mark.parametrize("owner,change", [
    ("inference_version", b"\n_MAX_VERSION_LENGTH = _MAX_VERSION_LENGTH - 1\n"),
    ("clinical_events", b"\nSUBJECTS = SUBJECTS + ('synthetic_subject',)\n"),
    ("patient_state_context", b"\nSTATE_MAX = STATE_MAX - 1\n"),
])
def test_new_loaded_interpretation_reopens_old_hold_but_keeps_its_legacy_source_facts(
        tmp_path, monkeypatch, owner, change):
    body = "本人に息切れはありません。"
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(llm, "_rerender_cards", lambda *a, **k: None)
    try:
        ledger.save_messages([_message(body=body)])
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row['_source_binding'] = dependency_loss.source_binding(ledger.db,row['project_id'],row['message_id'])
        original = {"symptoms": [{"text": "息切れ", "subject": "patient", "negated": True,
                                  "evidence": body}]}
        prior = ledger.artifact_add(llm.KIND, json.dumps(original), project_id=1, message_id=1,
            meta={"hash": row["content_hash"], "extract_version": llm.EXTRACT_VERSION})
        prior_row = tuple(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (prior,)).fetchone())
        weak = '{"summary":"合成の候補"}'
        assert not llm._replace_current(ledger, row, weak)
        assert not ledger.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(ledger.db)).fetchone()
        before = llm._upgrade_contract()
        module = importlib.import_module(owner)
        copied = tmp_path / (owner + ".py")
        copied.write_bytes(Path(module.__file__).read_bytes() + change)
        digest = hashlib.sha256(copied.read_bytes()).hexdigest()
        assert digest != llm._LOADED_SOURCE_DIGESTS[owner]
        monkeypatch.setattr(llm, "_LOADED_SOURCE_DIGESTS", {**llm._LOADED_SOURCE_DIGESTS, owner: digest})
        assert llm._upgrade_contract() != before
        # A changed rule contract releases only the old hold's suppression, not the source protection.
        assert ledger.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(ledger.db)).fetchone()
        assert not llm._replace_current(ledger, row, weak)
        assert not llm._current(ledger, 1, row["content_hash"])
        receipts = [json.loads(r["content"]) for r in ledger.artifacts(llm.UPGRADE_KIND)]
        assert len(receipts) == 2 and {r["contract"] for r in receipts} == {before, llm._upgrade_contract()}
        assert all(r["state"] == "held_prior" and r["prior_artifact_id"] == prior for r in receipts)
        assert tuple(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (prior,)).fetchone()) == prior_row
    finally:
        ledger.close()
