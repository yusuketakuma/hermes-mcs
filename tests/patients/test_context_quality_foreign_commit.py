"""Synthetic tests: another process's commit does not stop a quality check whose inputs are unchanged."""
import json
import os
import sqlite3
import time
from contextlib import closing

import pytest

import extract_llm as worker
import patient_context_quality_controller
import patient_context_quality_receipts
import patient_context_refresh_readiness
import patient_context_refresh_state
from anthropic_context_testkit import CFG, cloud, drain, runtime, step
from extract_testkit import _ledger
from test_patient_context_refresh import seed
from test_patient_summary_quality import quality_owner, repair

__all__ = ["runtime", "cloud", "quality_owner", "repair"]


def _db_path(led):
    return led.db.execute("PRAGMA database_list").fetchone()[2]


def _other_process(led, statement=None, params=()):
    """Commit from a second connection, as another worker's process does."""
    with closing(sqlite3.connect(_db_path(led), timeout=5)) as other:
        other.execute("CREATE TABLE IF NOT EXISTS synthetic_other_work(n INTEGER)")
        other.execute("INSERT INTO synthetic_other_work VALUES (1)")
        if statement is not None:
            assert other.execute(statement, params).rowcount >= 1
        other.commit()


def _commit_at_every_lock(monkeypatch, led, statement=None, params=(), *, when=lambda: True):
    """Let another process commit between each read outside the lock and the write that follows."""
    commits = []

    def acquire():
        if when():
            _other_process(led, statement, params)
            commits.append(1)
        return os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(patient_context_refresh_state, "acquire_run_lock", acquire)
    return commits


def _observe_patient(led):
    return patient_context_quality_receipts._quality_read(led, time.monotonic() + 60,
        lambda reader: reader.db.execute("SELECT project_id FROM patients WHERE project_id=1").fetchone())


def test_quality_read_rejects_an_unexpected_write_on_its_own_connection(tmp_path):
    with closing(_ledger(tmp_path)) as led:
        led.ensure_patient(1)
        _, current = _observe_patient(led)
        _other_process(led)
        assert current()
        led.db.execute("INSERT INTO synthetic_other_work VALUES (2)")
        assert not current() and current(1)


def test_quality_read_rechecks_the_writer_snapshot(tmp_path):
    with closing(_ledger(tmp_path)) as led:
        led.ensure_patient(1)
        _, current = _observe_patient(led)
        _other_process(led)
        led.db.execute("BEGIN IMMEDIATE")
        try:
            assert current() and led.db.in_transaction
            assert current() and led.db.in_transaction
        finally:
            led.db.rollback()


def test_quality_read_rejects_a_change_during_the_initial_snapshot(tmp_path):
    with closing(_ledger(tmp_path)) as led:
        led.ensure_patient(1)

        def read(reader):
            row = reader.db.execute("SELECT probe_mid FROM patients WHERE project_id=1").fetchone()
            _other_process(led, "UPDATE patients SET probe_mid=? WHERE project_id=1", (7,))
            return row

        with pytest.raises(ValueError, match="patient_summary_quality_source_changed"):
            patient_context_quality_receipts._quality_read(led, time.monotonic() + 60, read)
        assert not led.db.in_transaction


def test_quality_pass_survives_unrelated_commits_but_not_a_lost_answer(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        monkeypatch.setattr(patient_context_refresh_state, "acquire_run_lock",
                            lambda: os.open(os.devnull, os.O_RDONLY))
        identity = {"input_key": "a" * 64, "candidate_key": "d" * 64, "failure_key": "f" * 64,
                    "audit_key": "b" * 64, "fact_ids": ["c" * 64, "e" * 64]}
        claim = patient_context_quality_receipts._quality_claim(
            led, 1, identity["input_key"], time.monotonic() + 60, lambda *_: True)
        patient_context_quality_receipts._quality_store_answers(led, 1, identity, claim, {
            "c" * 64: {"choice": "supports", "confidence": 0.99},
            "e" * 64: {"choice": "omission_ok", "confidence": 0.99}}, lambda *_: True, 0.9, {"c" * 64})
        # Another process deletes one answer after the receipts were read and before the write:
        # the receipts are read again under the writer lock and the PASS is refused.
        lost = _commit_at_every_lock(
            monkeypatch, led, "DELETE FROM artifacts WHERE kind=? AND json_extract(meta,'$.fact_id')=?",
            (patient_context_refresh_state.QUALITY, "e" * 64))
        with pytest.raises(ValueError, match="patient_summary_quality_source_changed"):
            patient_context_quality_receipts._quality_mark_pass(led, 1, identity, claim, {"c" * 64}, lambda *_: True, 0.9)
        assert lost and not patient_context_quality_receipts._quality_passed(led.db, 1, identity)
        assert not led.db.in_transaction
        monkeypatch.setattr(patient_context_refresh_state, "acquire_run_lock",
                            lambda: os.open(os.devnull, os.O_RDONLY))
        patient_context_quality_receipts._quality_store_answers(led, 1, identity, claim, {
            "e" * 64: {"choice": "omission_ok", "confidence": 0.99}}, lambda *_: True, 0.9, {"c" * 64})
        # An unrelated commit at the same point changes nothing the PASS depends on.
        unrelated = _commit_at_every_lock(monkeypatch, led)
        assert patient_context_quality_receipts._quality_mark_pass(led, 1, identity, claim, {"c" * 64}, lambda *_: True, 0.9)
        assert unrelated and patient_context_quality_receipts._quality_passed(led.db, 1, identity)
    finally:
        led.close()


def _summary_step(led, clock, cfg, doc, **kwargs):
    return patient_context_quality_controller._quality_step(
        led, 1, cfg, "2026-10-17", doc, clock.mono + 600, lambda: True,
        cloud_mode=worker._cloud_mode(led, cfg, "patient_context", 1), **kwargs)


def test_summary_audit_survives_another_process_refreshing_seen_time(quality_owner, monkeypatch):
    """Re-observing an unchanged post still rewrites updated_seen; that
    maintenance column is not a quality input, so the audit must proceed."""
    led, clock, cfg, doc, sends, _ = quality_owner
    commits = _commit_at_every_lock(
        monkeypatch, led,
        "UPDATE messages SET updated_seen=COALESCE(updated_seen,0)+1 WHERE message_id=1")
    status, _ = _summary_step(led, clock, cfg, doc)
    assert status == "pending" and len(sends) == 1
    clock.mono += 61
    status, identity = _summary_step(led, clock, cfg, doc)
    assert status == "passed" and len(sends) == 1   # the answer was sent and billed once
    assert patient_context_quality_receipts._quality_passed(led.db, 1, identity)
    assert len(commits) >= 2


@pytest.mark.parametrize("statement,params", [
    ("UPDATE messages SET body_text=body_text||'変更' WHERE message_id=1", ()),
    ("UPDATE messages SET content_hash='synthetic-rewritten' WHERE message_id=1", ()),
    ("UPDATE messages SET first_seen=COALESCE(first_seen,0)+1 WHERE message_id=1", ()),
    # Maintenance columns a broad message read also returns stay signed: only
    # updated_seen may differ without stopping the audit.
    ("UPDATE messages SET reply_count=COALESCE(reply_count,0)+1 WHERE message_id=1", ()),
    ("UPDATE messages SET is_unread=1-is_unread WHERE message_id=1", ()),
    ("INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) "
     "VALUES (1,99,'synthetic','full',0)", ()),
    ("UPDATE patients SET fetch_state='forbidden' WHERE project_id=1", ()),
    ("UPDATE artifacts SET content=content||' ' WHERE kind=? AND project_id=1",
     (patient_context_refresh_state.CHUNK,)),
])
def test_summary_audit_still_stops_when_another_process_changes_inputs(
        quality_owner, monkeypatch, statement, params):
    """Body, hash, first_seen, reply_count, is_unread, revisions, permission and
    the summary's chunk artifacts are inputs: bumping them at the lock position
    must still refuse the quality write."""
    led, clock, cfg, doc, sends, _ = quality_owner
    _commit_at_every_lock(monkeypatch, led, statement, params)
    with pytest.raises(ValueError, match="patient_summary_quality_source_changed"):
        _summary_step(led, clock, cfg, doc)
    assert not sends and not led.db.in_transaction


def test_summary_audit_passes_while_another_process_keeps_committing(quality_owner, monkeypatch):
    led, clock, cfg, doc, sends, _ = quality_owner
    commits = _commit_at_every_lock(monkeypatch, led)
    status, _ = _summary_step(led, clock, cfg, doc)
    assert status == "pending" and len(sends) == 1
    status, identity = _summary_step(led, clock, cfg, doc)
    assert status == "passed" and len(sends) == 1
    assert patient_context_quality_receipts._quality_passed(led.db, 1, identity)
    # the claim, the reservation, the answers and the PASS each took the lock after such a commit
    assert len(commits) >= 4


def test_summary_audit_stops_when_another_process_changes_the_audited_summary(quality_owner, monkeypatch):
    led, clock, cfg, doc, sends, _ = quality_owner
    _commit_at_every_lock(
        monkeypatch, led, "UPDATE artifacts SET content=json_set(content,'$.summary',json('[]')) "
        "WHERE kind=? AND project_id=1", (patient_context_refresh_state.KIND,))
    with pytest.raises(ValueError, match="patient_summary_quality_source_changed"):
        _summary_step(led, clock, cfg, doc)
    assert not sends and not led.artifacts("semantic_usage")
    assert not led.db.in_transaction


@pytest.mark.parametrize("model_changed", [True, False])
def test_summary_pass_rechecks_config_at_the_writer(quality_owner, monkeypatch, model_changed):
    led, clock, cfg, doc, sends, _ = quality_owner
    status, identity = _summary_step(led, clock, cfg, doc)
    assert status == "pending" and len(sends) == 1
    read = patient_context_quality_receipts._quality_pass_content
    acquire = patient_context_quality_receipts._acquire
    proofs = []
    changed = []

    def read_proof(*args, **kwargs):
        content = read(*args, **kwargs)
        if content is not None:
            proofs.append(content)
        return content

    def change_before_save(*args, **kwargs):
        if proofs and not changed:
            if model_changed:
                monkeypatch.setitem(cfg, "local_llm", {"model": "synthetic-new-model"})
            else:
                monkeypatch.setitem(cfg["cloud_llm"], "daily_request_budget", 50)
            changed.append(True)
        return acquire(*args, **kwargs)

    monkeypatch.setattr(patient_context_quality_receipts, "_quality_pass_content", read_proof)
    monkeypatch.setattr(patient_context_quality_receipts, "_acquire", change_before_save)
    if model_changed:
        with pytest.raises(ValueError, match="patient_summary_quality_source_changed"):
            _summary_step(led, clock, cfg, doc)
    else:
        assert _summary_step(led, clock, cfg, doc)[0] == "passed"
    assert changed and len(sends) == 1 and len(led.artifacts("semantic_usage")) == 1
    assert patient_context_quality_receipts._quality_passed(led.db, 1, identity) == (not model_changed)


def test_summary_repair_is_reserved_while_another_process_keeps_committing(repair, monkeypatch):
    led, run, facts, _, sent, _, _ = repair
    commits = _commit_at_every_lock(monkeypatch, led)
    status, result = run()
    assert status == "candidate" and result["fact_ids"] == [facts[1]["id"]]
    assert commits and len(sent) == 1
    assert len(led.artifacts(patient_context_refresh_state.QUALITY_REPAIR)) == 1


def test_summary_repair_is_not_spent_when_another_process_changes_an_answer(repair, monkeypatch):
    led, run, facts, _, sent, _, _ = repair
    # The request was built from "important_missing"; before the reservation another process
    # rewrites that answer, so the findings read under the writer lock no longer match.
    commits = _commit_at_every_lock(
        monkeypatch, led, "UPDATE artifacts SET content=? WHERE kind=? AND json_extract(meta,'$.fact_id')=?",
        (json.dumps({"choice": "omission_ok", "confidence": 0.99}, separators=(",", ":")),
         patient_context_refresh_state.QUALITY, facts[1]["id"]))
    assert run() == ("deferred", None)
    assert commits and not sent and not led.db.in_transaction
    assert not led.artifacts(patient_context_refresh_state.QUALITY_REPAIR)
    assert not led.artifacts("cloud_usage_reserve", project_id=1)


def test_cloud_context_is_audited_and_published_while_another_process_keeps_committing(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    commits = _commit_at_every_lock(monkeypatch, led)
    drain(runtime)
    assert commits
    assert led.artifacts("patient_context_source_quality_pass_v1", project_id=1)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)


def test_source_audit_is_not_adopted_when_another_process_removes_its_answer(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body="見出しのない完全合成の居住記録。")
    kind = "patient_context_source_quality_v1"
    # Once the source audit holds answers, another process removes one before each later write.
    removed = _commit_at_every_lock(
        monkeypatch, led, "DELETE FROM artifacts WHERE artifact_id=(SELECT MIN(artifact_id) FROM artifacts "
        "WHERE kind=? AND project_id=1)", (kind,),
        when=lambda: bool(led.db.execute("SELECT 1 FROM artifacts WHERE kind=? AND project_id=1", (kind,)).fetchone()))
    for _ in range(12):
        result = step(runtime)
        assert result["status"] in ("progressed", "deferred", "source_changed"), result
    # the audit did reach the point of holding answers, and lost one before a later write
    assert removed and led.synthetic_jev_requests
    assert not led.db.in_transaction
    assert not led.artifacts("patient_context_source_quality_pass_v1", project_id=1)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
