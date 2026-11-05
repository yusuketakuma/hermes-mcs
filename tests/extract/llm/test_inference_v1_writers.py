"""Synthetic inference v1 writers contracts with isolated shared fixtures."""
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import hashlib
import json
import sqlite3
from contextlib import closing
import pytest
import extract
import extract_llm as llm
import cloud_inference
import inference_version as inference
import inference_dependency_loss as dependency_loss
import mcs_queries
from extract_testkit import _message
from inference_v1_writers_testkit import _scoped_error_rows as _scoped_error_rows, _synthetic_inference as _synthetic_inference, _write_intervention_v1 as _write_intervention_v1, add as add, source as source


def test_same_internal_version_legacy_is_pending_and_preserved_after_success(source):
    led, row = source
    old = add(led, row)
    assert not llm._current(led, 1, row["content_hash"])
    assert led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(led.db)).fetchone()
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert led.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (old,)).fetchone()
    assert llm._current(led, 1, row["content_hash"])
    assert not led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(led.db)).fetchone()
    current = led.db.execute("SELECT a.meta FROM artifacts a JOIN messages m USING(message_id) "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred(), (llm.KIND,)).fetchall()
    assert len(current) == 1
    assert json.loads(current[0][0])[inference.VERSION_KEY] == inference.CURRENT_VERSION


def test_retry_budget_does_not_inherit_or_erase_legacy_errors(source):
    led, row = source
    old = add(led, row, error=True, attempts=5, auto_retry=3, next_try=10**12)
    assert llm._error_attempts(led, 1, row["content_hash"]) == 0
    assert llm._error_auto_retry(led, 1, row["content_hash"]) == 0
    assert llm._next_retry(led) is None
    assert led.db.execute("SELECT 1 FROM messages m WHERE " + llm._retry_budget_sql()).fetchone()
    llm._fail(led, row, 0)
    current = [json.loads(a[0]) for a in led.db.execute("SELECT meta FROM artifacts WHERE kind=?", (llm.KIND,))
               if inference.is_current(json.loads(a[0]))]
    assert len(current) == 1 and current[0]["attempts"] == 1
    assert led.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (old,)).fetchone()


def test_old_hold_receipt_does_not_hold_new_generation(source):
    led, row = source
    prior = add(led, row)
    add(led, row, kind=llm.UPGRADE_KIND, content={"state": "held_prior", "reason": "protected_loss",
        "hash": row["content_hash"], "prior_artifact_id": prior, "target_version": llm.EXTRACT_VERSION,
        "contract": llm._upgrade_contract()})
    assert not led.db.execute("SELECT 1 FROM messages m WHERE EXISTS (" + llm._hold_sql() + ")").fetchone()
    assert led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(led.db)).fetchone()


def test_legacy_success_is_protection_baseline_but_never_current(source):
    led, row = source
    prior = add(led, row, content={"symptoms": [{"text": "発熱", "negated": True,
        "subject": "patient", "status": "ongoing", "evidence": "本人は発熱なし。"}]})
    assert llm._prior_extract(led.db, row)["artifact_id"] == prior
    assert not llm._current(led, 1, row["content_hash"])
    assert not llm._replace_current(led, row, '{"summary":"合成の候補"}')
    receipt = led.artifacts(llm.UPGRADE_KIND)[0]
    assert json.loads(receipt["meta"])[inference.VERSION_KEY] == inference.CURRENT_VERSION
    assert json.loads(receipt["content"])["prior_artifact_id"] == prior
    assert not led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred(led.db)).fetchone()


@pytest.mark.parametrize("invalid", ["project", "hash", "snippet", "empty", "error", "content_error", "malformed"])
def test_protection_baseline_requires_matching_complete_valid_source(source, invalid):
    led, row = source
    aid = add(led, row)
    with led.db:
        if invalid == "project":
            led.db.execute('DROP TRIGGER g1_artifacts_msg_upd')  # Simulate a corrupt imported snapshot.
            led.db.execute("UPDATE artifacts SET project_id=2 WHERE artifact_id=?", (aid,))
        elif invalid == "hash":
            led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", ('{"hash":"different"}', aid))
        elif invalid in ("snippet", "empty"):
            led.db.execute("UPDATE messages SET body_state=?,body_text=? WHERE message_id=1",
                ("snippet" if invalid == "snippet" else "full", row["body_text"] if invalid == "snippet" else ""))
        elif invalid == "error":
            led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                (json.dumps({"hash": row["content_hash"], "error": True}), aid))
        else:
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                ('{"_error":true}' if invalid == "content_error" else "{", aid))
    assert llm._prior_extract(led.db, row) is None


def test_checkpoints_require_exact_public_version_even_with_forged_current_binding(source):
    led, row = source
    meta = {"ver": llm.EXTRACT_VERSION, "chunk": 0, "chunk_size": llm._CHUNK_SIZE,
        "context": llm._chunk_context(row, None, checkpoint=True),
        "piece_sha256": hashlib.sha256(row["body_text"].encode()).hexdigest()}
    old = add(led, row, kind="extract_llm_chunk", **meta)
    assert llm._saved_chunk_state(led.db, row) == ({}, {})
    assert llm._persist_chunks(led, row, {0: {"summary": "合成の経過報告"}})
    splits, saved = llm._saved_chunk_state(led.db, row)
    assert not splits and 0 in saved
    assert led.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (old,)).fetchone()


@pytest.mark.parametrize("change", ["source", "generation", "version_source"])
@pytest.mark.parametrize("writer", ["success", "failure", "checkpoint", "rule_hint", "claim"])
def test_changed_source_or_loaded_generation_cannot_write(source, monkeypatch, change, writer):
    led, row = source
    if change == "source":
        with led.db:
            led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
    elif change == "generation":
        monkeypatch.setattr(inference, "CURRENT_VERSION", "2.0")
    else:
        monkeypatch.setattr(llm, "_INFERENCE_SOURCE_STAT", -1)
        monkeypatch.setattr(extract_llm_source_contract, '_INFERENCE_SOURCE_STAT', llm._INFERENCE_SOURCE_STAT)
    if writer == "success":
        assert not llm._replace_current(led, row, '{"summary":"合成"}')
    elif writer == "failure":
        llm._fail(led, row, 0)
    elif writer == "checkpoint":
        assert not llm._persist_chunks(led, row, {0: {"summary": "合成"}})
    elif writer == "rule_hint":
        llm._ensure_v1(led, row, {"v": extract.RULE_VERSION})
    else:
        assert llm._claim(led, row) is None
    assert not led.db.execute("SELECT 1 FROM artifacts").fetchone()
    assert not led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()


def test_rule_source_change_during_extraction_does_not_commit(source, monkeypatch):
    led, row = source
    def infer(*args):
        with led.db:
            led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
        return {"v": extract.RULE_VERSION}
    monkeypatch.setattr(extract, "extract_message", infer)
    assert extract._extract_rows(led, [row]) == (0, set())
    assert not led.artifacts("extract_v1")


@pytest.mark.parametrize('change', ['posted_at', 'body_aba', 'metadata'])
def test_rule_run_rechecks_selected_occurrence_before_writing(source, monkeypatch, change):
    led, row = source
    original = _message(body=row['body_text'], posted_at=row['posted_at'])
    real_extract = extract.extract_message

    def infer(*args):
        if change == 'posted_at':
            led.save_messages([_message(body=row['body_text'], posted_at='2026-09-20T00:00:00+09:00')])
        elif change == 'body_aba':
            led.save_messages([_message(body='別の完全合成投稿です。')])
            led.save_messages([original])
        else:
            with led.db:
                led.db.execute("UPDATE messages SET sender_name='別の合成投稿者' WHERE message_id=1")
        return real_extract(*args)

    monkeypatch.setattr(extract, 'extract_message', infer)
    assert extract.run_pending(led)['done'] == 0
    assert not led.artifacts('extract_v1')
    monkeypatch.setattr(extract, 'extract_message', real_extract)
    assert extract.run_pending(led)['done'] == 1
    meta = json.loads(led.artifacts('extract_v1')[0]['meta'])
    assert meta['source_binding'] == dependency_loss.source_binding(led.db, row['project_id'], row['message_id'])


def test_run_lock_reacquire_rejects_loaded_worker_after_update(source, monkeypatch):
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 2))
    monkeypatch.setattr(llm, "acquire_run_lock", lambda: 123)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', llm.acquire_run_lock)
    closed = []
    monkeypatch.setattr(llm.os, "close", closed.append)
    with llm._write_lock(True, wait_s=1) as held:
        assert not held
    assert closed == [123]


def test_rule_full_source_scan_is_once_per_200_row_transaction(source, monkeypatch):
    led, row = source
    scans = []
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: scans.append(1) or (False, 1))
    monkeypatch.setattr(extract, "extract_message", lambda *a: {"v": extract.RULE_VERSION})
    assert extract._extract_rows(led, [row] * 201)[0] == 201
    assert len(scans) == 2


def test_checkpoint_bookkeeping_never_scans_all_mcs_sources(source, monkeypatch):
    led, row = source
    def forbidden():
        raise AssertionError("full source scan per checkpoint")
    monkeypatch.setattr(llm.writer_lock, "updater_state", forbidden)
    for _ in range(3):
        assert llm._persist_chunks(led, row, {0: {"summary": "合成"}})


@pytest.mark.parametrize("version", ["1.1", "2.0"])
def test_future_public_version_changes_all_binding_contracts(source, monkeypatch, version):
    _, row = source
    before = (llm._upgrade_contract(), llm._context_contract(), llm._chunk_context(row, None, checkpoint=True))
    monkeypatch.setattr(llm, "_INFERENCE_VERSION", version)
    monkeypatch.setattr(extract_llm_context, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_errors, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_prepare, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_progress, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_proofs, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_provider, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_qc, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_resident, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_selection, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_source_contract, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    monkeypatch.setattr(extract_llm_store, '_INFERENCE_VERSION', llm._INFERENCE_VERSION)
    after = (llm._upgrade_contract(), llm._context_contract(), llm._chunk_context(row, None, checkpoint=True))
    assert all(left != right for left, right in zip(before, after))
    assert len(llm._LOADED_SOURCE_DIGESTS["inference_version"]) == 64


def test_qc_and_feedback_pin_only_exact_current_public_generation(source):
    led, row = source
    legacy = add(led, row)
    current = add(led, row, version=inference.CURRENT_VERSION)
    add(led, row, version="2.0")  # newest id alone cannot select a different generation
    old_feedback = add(led, row, kind=mcs_queries.EXTRACT_FEEDBACK_KIND,
        content={"artifact_id": legacy})
    current_feedback = add(led, row, kind=mcs_queries.EXTRACT_FEEDBACK_KIND,
        content={"artifact_id": current})
    flags = {r["artifact_id"]: r["current"] for r in mcs_queries.extract_feedback(led.db)}
    assert flags[old_feedback] == 0 and flags[current_feedback] == 1
    content = {"qc": "done", "items": [{"section": "symptoms", "verdict": "NO_MATCH", "item": {"text": "発熱"}}]}
    add(led, row, kind="extract_qc", source_artifact_id=current, content=content)
    assert led.db.execute("SELECT (" + llm._qc_flagged_sql() + ") FROM messages m").fetchone()[0] is None
    add(led, row, kind="extract_qc", version=inference.CURRENT_VERSION,
        source_artifact_id=current, content=content)
    assert led.db.execute("SELECT (" + llm._qc_flagged_sql() + ") FROM messages m").fetchone()[0] == current


def test_zero_hint_first_pass_records_explicit_completed_leaf(source, monkeypatch):
    led, row = source
    meta, checkpoints = {}, {}
    _synthetic_inference(monkeypatch, lambda *a, **k: {"symptoms": [{"text": "発熱", "negated": True,
        "subject": "patient", "evidence": "本人は発熱なし。"}], "summary": "合成の経過報告"})
    result = llm.llm_extract(row["body_text"], meta_out=meta, chunks_out=checkpoints)
    assert result is not None and result is not llm._DEFERRED
    assert meta["completeness_chunks"] == 1
    assert meta["completeness"] == {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}
    assert checkpoints[0]["_quality_result"] == meta["completeness"]["0"]
    assert llm._replace_current(led, row, json.dumps(result), integrity=meta)
    assert llm.progress_for_message(led.db, row, model=llm.MODEL)["state"] == "complete"


def test_missing_current_coverage_is_attention_until_explicit_check(source):
    led, row = source
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert llm.progress_for_message(led.db, row, model=llm.MODEL)["state"] == "attention"


@pytest.mark.parametrize("recorded", [False, True])
def test_all_cached_leaves_keep_coverage_without_meta_out(source, monkeypatch, recorded):
    _, row = source
    def forbidden(*a, **k):
        raise AssertionError("paid cached leaf was dispatched again")
    _synthetic_inference(monkeypatch, forbidden)
    cached = {"summary": "合成の経過報告"}
    if recorded:
        cached["_quality_result"] = {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
    checkpoints = {}
    result = llm.llm_extract(row["body_text"], chunks_in={0: cached}, chunks_out=checkpoints)
    assert result is not None and result is not llm._DEFERRED
    assert checkpoints[0]["_quality_result"] == {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}


@pytest.mark.parametrize("state", ["unresolved", "dispatch_unknown", "skipped_budget"])
def test_cached_unfinished_quality_is_never_complemented_as_resolved(source, monkeypatch, state):
    body = "本人の体温は38.0度です。"
    _synthetic_inference(monkeypatch, lambda *a, **k: pytest.fail("terminal cached candidate redispatched"))
    meta, checkpoints = {}, {}
    record = {"state": state, "remaining_hint_candidates": 1}
    llm.llm_extract(body, chunks_in={0: {"summary": "合成", "_quality_result": record}},
        chunks_out=checkpoints, meta_out=meta)
    assert meta["completeness_chunks"] == 1 and meta["completeness"]["0"] == record
    assert not llm.extract_quality.completeness_resolved({inference.VERSION_KEY: inference.CURRENT_VERSION,
        "integrity": meta})
    assert checkpoints[0]["_quality_result"] == record


def test_partial_chunk_never_has_a_completed_coverage_denominator(source, monkeypatch):
    _, row = source
    body = "合成の経過共有です。合成の連絡です。"
    monkeypatch.setattr(llm, "plan_chunks", lambda text, size: [text[:10], text[10:]])
    monkeypatch.setattr(extract_llm_engine, 'plan_chunks', llm.plan_chunks)
    monkeypatch.setattr(extract_llm_prepare, 'plan_chunks', llm.plan_chunks)
    monkeypatch.setattr(extract_llm_progress, 'plan_chunks', llm.plan_chunks)
    monkeypatch.setattr(extract_llm_proofs, 'plan_chunks', llm.plan_chunks)
    calls = []
    _synthetic_inference(monkeypatch, lambda *a, **k: calls.append(1) or
        ({"symptoms": [], "summary": "合成"} if len(calls) == 1 else None))
    meta = {}
    assert llm.llm_extract(body, meta_out=meta) is None
    assert meta["completeness"]["0"]["state"] == "hint_gaps_resolved"
    assert meta["completeness"]["1"]["state"] == "dispatch_unknown"
    assert "completeness_chunks" not in meta
    assert not llm.extract_quality.completeness_resolved({inference.VERSION_KEY: inference.CURRENT_VERSION,
        "integrity": meta})


def test_unfinished_hint_repair_preserves_unknown_quality(source, monkeypatch):
    body = "本人に発熱があります。本人の血圧120/80です。"
    first = {"symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing",
        "negated": False, "evidence": "本人に発熱があります。"}]}
    calls = []
    _synthetic_inference(monkeypatch, lambda *a, **k: calls.append(1) or
        (first if len(calls) == 1 else llm._DEFERRED))
    meta = {}
    result = llm.llm_extract(body, meta_out=meta)
    assert result is not None and len(calls) == 2
    assert meta["completeness"]["0"]["state"] == "dispatch_unknown"
    assert meta["completeness"]["0"]["remaining_hint_candidates"] > 0
    assert not llm.extract_quality.completeness_resolved({inference.VERSION_KEY: inference.CURRENT_VERSION,
        "integrity": meta})


def test_prefilter_completes_only_an_explicitly_checked_greeting(source):
    led, row = source
    assert not llm._mark_prefiltered(led, row)
    led.save_messages([_message(body="こんにちは。")])
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(led.db,row['project_id'],row['message_id'])
    assert llm._mark_prefiltered(led, row)
    meta = json.loads(led.artifacts(llm.KIND)[0]["meta"])
    assert meta["integrity"]["completeness_chunks"] == 1
    assert meta["integrity"]["completeness"]["0"] == {
        "state": "hint_gaps_resolved", "remaining_hint_candidates": 0, "checked_hint_candidates": 0}
    assert llm.progress_for_message(led.db, row, model=llm.MODEL)["state"] == "complete"


@pytest.mark.parametrize("mutation", ["posting_time", "sender_aba"])
def test_selection_fence_rejects_stale_prefilter_input(source, monkeypatch, mutation):
    from dataclasses import replace

    led, _ = source
    original = _message(body="こんにちは。")
    led.save_messages([original])
    rows, _ = llm._select_pending(led, 1, {1}, None, False)
    assert len(rows) == 1
    selected = rows[0]
    binding = dependency_loss.source_binding_from_row(selected)
    assert binding == dependency_loss.source_binding(led.db, 1, 1)
    if mutation == "posting_time":
        led.save_messages([replace(original, posted_at="2026-09-20T00:00:00+09:00")])
    else:
        led.save_messages([replace(original, sender_name="別の合成投稿者")])
        led.save_messages([original])
    assert selected["content_hash"] == led.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    assert binding != dependency_loss.source_binding(led.db, 1, 1)
    monkeypatch.setattr(llm, "load_config", lambda: {})
    monkeypatch.setattr(extract_llm_context, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(llm, "_ensure_v1", lambda *_args: pytest.fail("stale rule write"))
    monkeypatch.setattr(extract_llm_prepare, '_ensure_v1', llm._ensure_v1)
    monkeypatch.setattr(extract_llm_runner, '_ensure_v1', llm._ensure_v1)
    monkeypatch.setattr(llm, "_mark_prefiltered", lambda *_args: pytest.fail("stale prefilter write"))
    monkeypatch.setattr(extract_llm_prepare, '_mark_prefiltered', llm._mark_prefiltered)
    jobs, skipped, blocked = llm._prepare_jobs(led, rows, llm._resolved_llm(), True)
    assert jobs == [] and skipped == 0 and blocked == 1


def test_unbound_old_frame_is_not_reused_for_current_attempt(source):
    import extraction_context

    led, row = source
    args = dict(model="synthetic", contract="synthetic-contract", attempt={},
                enabled=False, base_context=None, version=llm.EXTRACT_VERSION)
    first = extraction_context.prepare(led, row, **args)
    stored = led.db.execute("SELECT content,meta FROM artifacts WHERE artifact_id=?",
                            (first["artifact_id"],)).fetchone()
    meta = json.loads(stored["meta"])
    meta.pop("source_binding")
    document = json.loads(stored["content"])
    document["binding"] = extraction_context._digest({
        "target": extraction_context._source(row), "model": args["model"],
        "contract": args["contract"], "attempt": args["attempt"], "enabled": args["enabled"]})
    document["digest"] = extraction_context._digest({key: document[key] for key in (
        "binding", "source", "text", "refs", "enabled")})
    with led.db:
        led.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                       (json.dumps(document), json.dumps(meta), first["artifact_id"]))
    assert extraction_context.load(led.db, first["artifact_id"], row) is None
    second = extraction_context.prepare(led, row, **args)
    assert second["artifact_id"] != first["artifact_id"]
    assert extraction_context.load(led.db, second["artifact_id"], row) == second


def test_context_prepare_rejects_target_occurrence_aba(source):
    from dataclasses import replace
    import extraction_context

    led, row = source
    original = _message(body=row["body_text"])
    led.save_messages([replace(original, sender_name="別の合成投稿者")])
    led.save_messages([original])
    with pytest.raises(ValueError, match="context_source_changed"):
        extraction_context.prepare(led, row, model="synthetic", contract="synthetic-contract",
            attempt={}, enabled=False, base_context=None, version=llm.EXTRACT_VERSION)
    assert led.artifacts(extraction_context.KIND) == []


def test_context_load_rejects_frame_after_target_occurrence_aba(source):
    from dataclasses import replace
    import extraction_context

    led, row = source
    first = extraction_context.prepare(led, row, model="synthetic", contract="synthetic-contract",
        attempt={}, enabled=False, base_context=None, version=llm.EXTRACT_VERSION)
    original = _message(body=row["body_text"])
    led.save_messages([replace(original, sender_name="別の合成投稿者")])
    led.save_messages([original])
    current = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    assert current["content_hash"] == row["content_hash"]
    assert extraction_context.load(led.db, first["artifact_id"], current) is None


def test_context_prepare_does_not_finish_a_borrowed_transaction(source):
    import extraction_context

    led, row = source
    args = dict(model="synthetic", contract="synthetic-contract", attempt={},
                enabled=False, base_context=None, version=llm.EXTRACT_VERSION)
    first = extraction_context.prepare(led, row, **args)
    old_unread = led.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0]
    pending_unread = 0 if old_unread else 1
    led.db.execute("BEGIN")
    try:
        led.db.execute("UPDATE messages SET is_unread=? WHERE message_id=1", (pending_unread,))
        assert extraction_context.load(led.db, first["artifact_id"], row) == first
        with pytest.raises(ValueError, match="context_transaction_owned"):
            extraction_context.prepare(led, row, **args)
        assert led.db.in_transaction
        assert led.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == pending_unread
    finally:
        led.db.rollback()
    assert led.db.execute("SELECT is_unread FROM messages WHERE message_id=1").fetchone()[0] == old_unread


def test_event_record_and_legacy_projection_count_as_one_thin_fact(source):
    led, _ = source
    led.save_messages([_message(body="本人は退院しました。" + "合成の経過共有です。" * 40)])
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(led.db,row['project_id'],row['message_id'])
    aid = add(led, row, version=inference.CURRENT_VERSION, content={"events": ["discharge"],
        "event_records": [{"kind": "discharge", "subject": "patient", "status": "current",
            "negated": False, "evidence": "本人は退院しました。"}]})
    assert led.db.execute("SELECT (" + llm._thin_pending_sql(led.db) + ") FROM messages m").fetchone()[0] == aid


@pytest.mark.parametrize('writer', ['success','checkpoint'])
@pytest.mark.parametrize('binding', [None, '', 123])
def test_positive_writers_refuse_unprepared_occurrences_without_rebinding_them(source, writer, binding):
    led, original = source
    row = {key:value for key,value in original.items() if key!='_source_binding'}
    if binding is not None:
        row['_source_binding'] = binding
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    accepted = (llm._replace_current(led,row,'{}') if writer=='success'
                else llm._persist_chunks(led,row,{0:{'summary':'合成の候補'}}))
    assert not accepted
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('change', ['aba','same_html_new_text'])
def test_effective_source_change_never_revives_an_old_result_or_checkpoint(source,monkeypatch,change):
    import ledger as ledger_module
    led, row = source
    assert llm._replace_current(led,row,'{"summary":"合成の旧結果"}')
    assert llm._persist_chunks(led,row,{0:{'summary':'合成の旧区間'}})
    assert llm._saved_chunk_state(led.db,row)[1]
    old_ids = {r[0] for r in led.db.execute('SELECT artifact_id FROM artifacts')}
    if change=='aba':
        led.save_messages([_message(body='本人の別の合成記録です。')])
        led.save_messages([_message(body=row['body_html'])])
    else:
        monkeypatch.setattr(ledger_module,'html_to_text',lambda html: '同じHTMLの別解釈による合成原文。')
        led.save_messages([_message(body=row['body_html'])])
    fresh = dict(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    fresh['_source_binding'] = dependency_loss.source_binding(led.db,1,1)
    assert fresh['content_hash']==row['content_hash']
    assert fresh['_source_binding']!=row['_source_binding']
    assert not llm._current(led,1,fresh['content_hash'])
    assert llm._prior_extract(led.db,fresh) is None
    assert llm._saved_chunk_state(led.db,fresh)==({}, {})
    assert not llm._replace_current(led,row,'{}')
    assert not llm._persist_chunks(led,row,{0:{'summary':'古いoccurrenceの候補'}})
    assert llm.progress_for_message(led.db,fresh,model=llm.MODEL)['state']!='complete'
    assert led.db.execute('SELECT 1 FROM messages m WHERE '+llm.pending_pred(led.db)).fetchone()
    assert old_ids <= {r[0] for r in led.db.execute('SELECT artifact_id FROM artifacts')}
    assert llm._replace_current(led,fresh,'{}')  # A new valid empty result stays empty; no old-result fallback.
    current = led.db.execute('SELECT a.content FROM artifacts a JOIN messages m USING(message_id) '
        'WHERE a.kind=? '+mcs_queries.current_extract_pred(),(llm.KIND,)).fetchall()
    assert [json.loads(r[0]) for r in current]==[{}]


@pytest.mark.parametrize('change', ['body', 'sender_aba', 'unrelated'])
def test_a_prepared_frame_cannot_publish_after_its_other_source_reference_changes(source, change):
    import extraction_context
    from test_pharmacy_state import context
    led, target = source
    body = '本人の合成薬Aの残薬は3錠。'
    led.save_messages([_message(mid=2,body=body,posted_at='2026-09-01T10:00:00+09:00')])
    refhash = led.db.execute('SELECT content_hash FROM messages WHERE message_id=2').fetchone()[0]
    led.artifact_add(llm.KIND,json.dumps({'patient_context':[context(body)]}),project_id=1,message_id=2,
        model='synthetic',meta={inference.VERSION_KEY:inference.CURRENT_VERSION,'hash':refhash,
        'extract_version':llm.EXTRACT_VERSION,'actual_model':'synthetic',
        'integrity':{'validation':{'items_dropped':0,'evidence_dropped':0},
                     'completeness_chunks':1,
                     'completeness':{'0':{'state':'hint_gaps_resolved','remaining_hint_candidates':0}}}})
    frame = extraction_context.prepare(led,target,model='synthetic',contract=llm._context_contract(),
        attempt={'human':None,'qc':None,'thin':None},enabled=True,base_context=None,version=llm.EXTRACT_VERSION)
    assert len(frame['refs'])==1 and frame['refs']['S1']['value']=='3錠'
    # Frozen storage carries the reference token as its map key, not in its value.
    assert 'token' not in frame['refs']['S1']
    prepared = {**target,'_frame':frame,'_context':frame['text'],'_input_context':None,'_target':('synthetic','synthetic')}
    assert llm._replace_current(led,prepared,'{"summary":"合成の初回結果"}')
    if change == 'body':
        led.save_messages([_message(mid=2,body='本人の合成薬Aの残薬は4錠。',posted_at='2026-09-01T10:00:00+09:00')])
    elif change == 'sender_aba':
        from dataclasses import replace
        antecedent = _message(mid=2,body=body,posted_at='2026-09-01T10:00:00+09:00')
        led.save_messages([replace(antecedent, sender_name='合成別投稿者')])
        led.save_messages([antecedent])
    else:
        led.save_messages([_message(mid=3,body='無関係な合成の記録です。')])
    assert dependency_loss.source_binding(led.db,1,1)==target['_source_binding']
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    if change == 'unrelated':
        assert llm._replace_current(led,prepared,'{"summary":"依然有効な参照を使った候補"}')
    else:
        assert not llm._replace_current(led,prepared,'{"summary":"変更前の参照を使った候補"}')
        assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]==before


@pytest.mark.parametrize('noise', ['invalidated','old_version','old_hash','wrong_project','malformed','null_version','nonobject'])
def test_only_current_bound_errors_consume_retry_budget_and_old_noise_does_not_poison_it(source,noise):
    led, row = source
    aid = add(led,row,version=inference.CURRENT_VERSION,error=True,attempts=5,auto_retry=30,next_try=10)
    record = led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?',(aid,)).fetchone()[0]
    meta = json.loads(record)
    if noise=='invalidated':
        meta['invalidated']=True
    elif noise=='old_version':
        meta[inference.VERSION_KEY]='0.1'
    elif noise=='old_hash':
        meta['hash']='old-source'
    elif noise=='null_version':
        meta[inference.VERSION_KEY]=None
    with led.db:
        if noise=='wrong_project':
            led.db.execute('DROP TRIGGER g1_artifacts_msg_upd')  # Corrupt snapshot; keep reader scope checks active.
        led.db.execute('UPDATE artifacts SET meta=?,project_id=? WHERE artifact_id=?',
            ('{broken' if noise=='malformed' else '[]' if noise=='nonobject' else json.dumps(meta),
             2 if noise=='wrong_project' else 1,aid))
    assert llm._error_attempts(led,1,row['content_hash'])==0
    assert llm._error_auto_retry(led,1,row['content_hash'])==0
    assert llm._next_retry(led) is None

    assert led.db.execute('SELECT 1 FROM messages m WHERE '+llm._retry_budget_sql()).fetchone()
    add(led,row,version=inference.CURRENT_VERSION,error=True,attempts=5,auto_retry=2,next_try=10)
    assert llm._error_attempts(led,1,row['content_hash'])==5
    assert llm._error_auto_retry(led,1,row['content_hash'])==2
    assert not led.db.execute('SELECT 1 FROM messages m WHERE '+llm._retry_budget_sql()).fetchone()
    assert llm._next_retry(led) is None


def test_invalidated_error_prefix_does_not_occupy_revive_limit_or_revived_counter(source,monkeypatch):
    led, row = source
    monkeypatch.setattr(llm,'REVIVE_MAX',1)
    monkeypatch.setattr(extract_llm_errors, 'REVIVE_MAX', llm.REVIVE_MAX)
    invalid = [add(led,row,version=inference.CURRENT_VERSION,error=True,attempts=5,auto_retry=0,
                   invalidated=True,next_try=0) for _ in range(5)]
    current = add(led,row,version=inference.CURRENT_VERSION,error=True,attempts=5,auto_retry=0,next_try=0)
    with led.db:
        led.db.execute('UPDATE artifacts SET created_at=artifact_id')
    before = [led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?',(aid,)).fetchone()[0] for aid in invalid]
    assert llm.revive_failed(led,now=llm.REVIVE_COOLDOWN_S+current+10)=={'revived':1,'skipped_cap':0}
    assert [led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?',(aid,)).fetchone()[0] for aid in invalid]==before
    meta = json.loads(led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?',(current,)).fetchone()[0])
    assert meta['attempts']==4 and meta['auto_retry']==1


@pytest.mark.parametrize('fault', ['invalidated','version_null','version_text','version_number'])
def test_unknown_or_lost_source_cannot_be_reassigned_as_a_protection_baseline(source,fault):
    led, row = source
    aid = add(led,row,version='0.1',content={'symptoms':[{'text':'発熱','negated':True,
        'subject':'patient','status':'ongoing','evidence':'本人は発熱なし。'}]})
    meta = json.loads(led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?',(aid,)).fetchone()[0])
    if fault=='invalidated':
        meta['invalidated']=True
    else:
        meta[inference.VERSION_KEY]={'version_null':None,'version_text':'unknown','version_number':1.0}[fault]
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?',(json.dumps(meta),aid))
    assert llm._prior_extract(led.db,row) is None
    assert llm._replace_current(led,row,'{}')
    assert not led.artifacts(llm.UPGRADE_KIND)
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?',(aid,)).fetchone()


@pytest.mark.parametrize('writer', ['rules', 'llm'])
def test_v1_zero_intervention_candidates_is_observed_without_claiming_full_history(source, writer):
    led, row = source
    result = _write_intervention_v1(led, row, writer)
    # A compact marker distinguishes verified zero from an absent key on an
    # unprocessed, legacy, incomplete or damaged artifact.
    assert result is None
    stored = led.db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_v1' "
                            "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert 'intervention_schedule' not in json.loads(stored['content'])
    meta = json.loads(stored['meta'])
    assert meta['rule_version'] == extract.RULE_VERSION
    assert meta['intervention_zero'] == 1
    assert meta['source_binding'] == dependency_loss.source_binding(led.db, 1, 1)


@pytest.mark.parametrize('pid,mid', [(0, 2), (1, 0), (1, -1)])
def test_invalid_legacy_identity_does_not_block_following_rule_source(source, pid, mid):
    led, _ = source
    with led.db:
        if pid == 0:
            led.db.execute("INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES(0,'synthetic')")
        led.db.execute('INSERT INTO messages(message_id,project_id,body_html,body_text,body_state,'
                       'content_hash,posted_at,posted_at_ts) '
                       'SELECT ?,?,body_html,body_text,body_state,content_hash,posted_at,posted_at_ts '
                       'FROM messages WHERE message_id=1', (mid, pid))
    rows = led.db.execute('SELECT m.message_id,m.project_id,' + dependency_loss.SOURCE_BINDING_COLUMNS +
                          ' FROM messages m WHERE m.message_id IN (?,1) ORDER BY m.message_id', (mid,)).fetchall()
    assert extract._extract_rows(led, rows) == (2, {pid, 1})
    artifacts = led.db.execute("SELECT message_id,content FROM artifacts WHERE kind='extract_v1'").fetchall()
    contents = {r['message_id']: json.loads(r['content']) for r in artifacts}
    invalid = contents[mid]['intervention_schedule']
    assert invalid['records'] == [] and invalid['coverage']['state'] == 'invalid_source'
    assert invalid['coverage']['source_validation'] == 'unverified'
    assert 'intervention_schedule' not in contents[1]


def test_fail_clears_only_matching_and_null_current_error_rows(source):
    led, row = source
    owned, null_scope, foreign, legacy = _scoped_error_rows(led, row)
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    llm._fail(led, row, 0)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (?,?)', (owned, null_scope)).fetchone()
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (legacy,)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE kind=? AND project_id=1 AND artifact_id NOT IN (?,?,?,?)',
                          (llm.KIND, owned, null_scope, foreign, legacy)).fetchone()


def test_revive_skips_foreign_before_limit_and_keeps_null_compatible(source):
    led, row = source
    owned, null_scope, foreign, legacy = _scoped_error_rows(led, row)
    # A foreign prefix exceeding the scan limit cannot starve valid errors.
    foreign_ids = [foreign]
    for _ in range(llm.REVIVE_MAX * 4):
        foreign_ids.append(add(led, row, version=inference.CURRENT_VERSION,
                               error=True, attempts=5, auto_retry=0))
    orphan = add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5, auto_retry=0)
    with led.db:
        led.db.execute('UPDATE artifacts SET message_id=999,created_at=0 WHERE artifact_id=?', (orphan,))
        led.db.executemany('UPDATE artifacts SET project_id=2,created_at=0 WHERE artifact_id=?',
                           [(aid,) for aid in foreign_ids])
        led.db.execute('UPDATE artifacts SET created_at=1 WHERE artifact_id IN (?,?)', (owned, null_scope))
    foreign_before = [tuple(a) for a in led.db.execute(
        'SELECT * FROM artifacts WHERE project_id=2 ORDER BY artifact_id')]
    orphan_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (orphan,)).fetchone())
    result = llm.revive_failed(led, now=llm.REVIVE_COOLDOWN_S + 1)
    assert result == {'revived': 2, 'skipped_cap': 0}
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts WHERE project_id=2 ORDER BY artifact_id')] == foreign_before
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (orphan,)).fetchone()) == orphan_before
    for aid in (owned, null_scope):
        meta = json.loads(led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()[0])
        assert meta['attempts'] == 4 and meta['auto_retry'] == 1
    assert json.loads(led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (legacy,)).fetchone()[0])['attempts'] == 5


def test_revive_rechecks_scope_at_compare_and_swap(source):
    led, row = source
    led.ensure_patient(2)
    owned = add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5, auto_retry=0)
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute('UPDATE artifacts SET created_at=0 WHERE artifact_id=?', (owned,))
    before_meta = led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (owned,)).fetchone()[0]
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    injected = []
    with closing(sqlite3.connect(db_path)) as other:
        def race(statement):
            if statement.startswith('UPDATE artifacts SET meta=') and not injected:
                other.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (owned,))
                other.commit()
                injected.append(True)
        led.db.set_trace_callback(race)
        try:
            assert llm.revive_failed(led, now=llm.REVIVE_COOLDOWN_S + 1) == {'revived': 0, 'skipped_cap': 0}
        finally:
            led.db.set_trace_callback(None)
    assert injected == [True]
    actual = led.db.execute('SELECT project_id,meta FROM artifacts WHERE artifact_id=?', (owned,)).fetchone()
    assert tuple(actual) == (2, before_meta)


def test_fail_source_changed_by_error_delete_rolls_back_existing_error(source):
    led, row = source
    old = add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_source = tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    led.db.execute(f'CREATE TRIGGER synthetic_fail_race AFTER DELETE ON artifacts WHEN OLD.artifact_id={old} BEGIN '
                   "UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_source_changed_during_write'):
        llm._fail(led, row, 0)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()) == before_source


@pytest.mark.parametrize('column,value', [
    ('kind', 'other_synthetic'), ('message_id', 2), ('project_id', None), ('created_at', 1),
    ('content', '{"_error":false}'), ('model', 'other-synthetic-model'),
])
def test_revive_rejects_changed_selection_identity(source, column, value):
    led, row = source
    led.save_messages([_message(mid=2, body='別の合成記録。')])
    target = add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5, auto_retry=0)
    with led.db:
        led.db.execute('UPDATE artifacts SET created_at=0 WHERE artifact_id=?', (target,))
    before_meta = led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (target,)).fetchone()[0]
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    injected = []
    with closing(sqlite3.connect(db_path)) as other:
        def race(statement):
            if statement.startswith('UPDATE artifacts SET meta=') and not injected:
                other.execute('UPDATE artifacts SET ' + column + '=? WHERE artifact_id=?', (value, target))
                other.commit()
                injected.append(True)
        led.db.set_trace_callback(race)
        try:
            assert llm.revive_failed(led, now=llm.REVIVE_COOLDOWN_S + 1) == {'revived': 0, 'skipped_cap': 0}
        finally:
            led.db.set_trace_callback(None)
    assert injected == [True]
    assert led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (target,)).fetchone()[0] == before_meta


@pytest.mark.parametrize('history_scope', ['foreign', 'null', 'own'])
def test_fresh_priority_ignores_foreign_but_keeps_own_legacy_history(source, history_scope):
    led, row = source
    led.ensure_patient(2)
    old = add(led, row)  # old public generation is intentional historical evidence
    with led.db:
        if history_scope == 'foreign':
            led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
            led.db.execute('UPDATE artifacts SET project_id=2,content=? WHERE artifact_id=?', ('x' * 262145, old))
        elif history_scope == 'null':
            led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (old,))
    fresh = led.db.execute('SELECT ' + llm._FRESH_SQL + ' FROM messages m WHERE m.message_id=1').fetchone()[0]
    assert bool(fresh) is (history_scope == 'foreign')


@pytest.mark.parametrize('corruption', ['ignore', 'delete', 'content', 'model', 'meta', 'created_at'])
def test_fail_error_insert_is_verified_and_rolls_back_existing_error(source, corruption):
    led, row = source
    add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    if corruption == 'ignore':
        event = 'BEFORE'
        action = 'SELECT RAISE(IGNORE);'
    else:
        event = 'AFTER'
        if corruption == 'delete':
            action = 'DELETE FROM artifacts WHERE artifact_id=NEW.artifact_id;'
        else:
            value = '12345' if corruption == 'created_at' else "'synthetic_modified'"
            action = f'UPDATE artifacts SET {corruption}={value} WHERE artifact_id=NEW.artifact_id;'
    led.db.execute(f"CREATE TRIGGER synthetic_error_insert {event} INSERT ON artifacts "
                   "WHEN NEW.kind='extract_llm' BEGIN " + action + ' END')
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        llm._fail(led, row, 0)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('caller', ['replace', 'fail'])
@pytest.mark.parametrize('corruption', ['ignore', 'delete', 'content', 'model', 'meta', 'created_at', 'prior'])
def test_hold_receipt_or_prior_corruption_rolls_back_all_writes(source, caller, corruption):
    led, row = source
    prior = add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
        'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    if caller == 'fail':
        add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    if corruption == 'ignore':
        event, action = 'BEFORE', 'SELECT RAISE(IGNORE);'
    elif corruption == 'prior':
        event, action = 'AFTER', f"UPDATE artifacts SET content='{{}}' WHERE artifact_id={prior};"
    elif corruption == 'delete':
        event, action = 'AFTER', 'DELETE FROM artifacts WHERE artifact_id=NEW.artifact_id;'
    else:
        event = 'AFTER'
        value = '12345' if corruption == 'created_at' else "'synthetic_modified'"
        action = f'UPDATE artifacts SET {corruption}={value} WHERE artifact_id=NEW.artifact_id;'
    led.db.execute(f"CREATE TRIGGER synthetic_hold_race {event} INSERT ON artifacts "
                   "WHEN NEW.kind='extract_upgrade_v1' BEGIN " + action + ' END')
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_(hold|insert)_unverified'):
        if caller == 'replace':
            llm._replace_current(led, row, '{"summary":"合成候補"}')
        else:
            llm._fail(led, row, 5)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
