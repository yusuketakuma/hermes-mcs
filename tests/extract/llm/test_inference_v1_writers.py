"""Synthetic public-generation boundaries for both legacy extraction writers."""
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
from extract_testkit import _ledger, _message


@pytest.fixture
def source(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    led.save_messages([_message(body="本人は発熱なし。合成の経過報告です。")])
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(llm, "_rerender_cards", lambda *a, **k: None)
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_revision'] = led.db.execute(
        "SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1").fetchone()[0]
    row['_source_binding'] = dependency_loss.source_binding(led.db,row['project_id'],row['message_id'])
    try:
        yield led, row
    finally:
        led.close()


def add(led, row, *, kind=llm.KIND, version=None, content=None, **meta):
    metadata = {"hash": row["content_hash"], "extract_version": llm.EXTRACT_VERSION, **meta}
    if version is not None:
        metadata[inference.VERSION_KEY] = version
    return led.artifact_add(kind, json.dumps(content or {"summary": "合成の経過報告"}),
        project_id=row["project_id"], message_id=row["message_id"], meta=metadata)


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


def test_replace_keeps_other_generations_and_only_replaces_current(source):
    led, row = source
    old = add(led, row)
    prior_current = add(led, row, version=inference.CURRENT_VERSION)
    future = add(led, row, version="2.0")
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    ids = {a[0] for a in led.db.execute("SELECT artifact_id FROM artifacts WHERE kind=?", (llm.KIND,))}
    assert old in ids and future in ids and prior_current not in ids
    assert len(ids) == 3


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


def test_rule_writer_reprocesses_legacy_once_without_deleting_it(source):
    led, row = source
    old = add(led, row, kind="extract_v1", rule_version=extract.RULE_VERSION)
    assert extract.run_pending(led)["done"] == 1
    assert extract.run_pending(led)["done"] == 0
    assert led.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (old,)).fetchone()
    assert len(led.artifacts("extract_v1")) == 2


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


def test_gc_keeps_historical_generations_and_unrelated_receipts(source, monkeypatch):
    from contextlib import nullcontext
    led, row = source
    legacy = add(led, row)
    current = add(led, row, version=inference.CURRENT_VERSION)
    future = add(led, row, version="2.0")
    receipt = add(led, row, kind="cloud_usage_reserve")
    with led.db:
        led.db.execute("UPDATE messages SET content_hash='changed' WHERE message_id=1")
    monkeypatch.setattr(llm, "_stale_gc_at", 0)
    llm._gc_stale_artifacts(led, lambda *a, **kw: nullcontext(True), False)
    ids = {r[0] for r in led.db.execute("SELECT artifact_id FROM artifacts")}
    assert {legacy, future, receipt} <= ids and current not in ids


def test_hold_cleanup_keeps_old_checkpoints_and_financial_receipts(source):
    led, row = source
    add(led, row, content={"symptoms": [{"text": "発熱", "negated": True,
        "subject": "patient", "status": "ongoing", "evidence": "本人は発熱なし。"}]})
    old_chunk = add(led, row, kind="extract_llm_chunk")
    current_chunk = add(led, row, kind="extract_llm_chunk", version=inference.CURRENT_VERSION)
    receipt = add(led, row, kind="cloud_usage_result")
    assert not llm._replace_current(led, row, '{"summary":"合成の候補"}')
    ids = {r[0] for r in led.db.execute("SELECT artifact_id FROM artifacts")}
    assert {old_chunk, receipt} <= ids and current_chunk not in ids


def _synthetic_inference(monkeypatch, answer):
    monkeypatch.setattr(llm, "_resolved_llm", lambda: ("synthetic", "synthetic"))
    monkeypatch.setattr(llm, "_llm_call", answer)


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
    _, row = source
    _synthetic_inference(monkeypatch, lambda *a, **k: pytest.fail("terminal cached candidate redispatched"))
    meta, checkpoints = {}, {}
    record = {"state": state, "remaining_hint_candidates": 0}
    llm.llm_extract(row["body_text"], chunks_in={0: {"summary": "合成", "_quality_result": record}},
        chunks_out=checkpoints, meta_out=meta)
    assert meta["completeness_chunks"] == 1 and meta["completeness"]["0"] == record
    assert not llm.extract_quality.completeness_resolved({inference.VERSION_KEY: inference.CURRENT_VERSION,
        "integrity": meta})
    assert checkpoints[0]["_quality_result"] == record


def test_partial_chunk_never_has_a_completed_coverage_denominator(source, monkeypatch):
    _, row = source
    body = "合成の経過共有です。合成の連絡です。"
    monkeypatch.setattr(llm, "plan_chunks", lambda text, size: [text[:10], text[10:]])
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
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {})
    monkeypatch.setattr(llm, "_ensure_v1", lambda *_args: pytest.fail("stale rule write"))
    monkeypatch.setattr(llm, "_mark_prefiltered", lambda *_args: pytest.fail("stale prefilter write"))
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


def _adoption_witness(led):
    """A real scheduler row and two public generations of retained cache."""
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,parent_id,payload,state,next_try,created_at,updated_at) "
            "VALUES('semantic_sched',0,0,NULL,?,'done',0,1,1)",
            (json.dumps({'analysis_epoch':0, 'source_epoch':7, 'other_owner':{'cursor':13}}),))
    old = led.artifact_add('patient_rollup', '{"generation":"old"}', project_id=1,
                          meta={inference.VERSION_KEY:'0.1'})
    current = led.artifact_add('patient_rollup', '{"generation":"current"}', project_id=1,
                              meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    with led.db:
        led.db.execute("INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES(2,'synthetic-other')")
    other = led.artifact_add('patient_rollup', '{"generation":"other-patient"}', project_id=2,
                            meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    return old, current, other


def _scheduler_payload(db):
    return json.loads(db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic_sched' "
                                "AND project_id=0 AND message_id=0").fetchone()[0])


def _write_adopted(led, row, writer, document, monkeypatch, *, compact=False, config_generation=None):
    if writer == 'rules':
        monkeypatch.setattr(extract, 'extract_message', lambda *args: dict(document))
        assert extract._extract_rows(led, [row]) == (1, {row['project_id']})
    else:
        content = json.dumps(document, ensure_ascii=compact, sort_keys=compact,
                             separators=(',', ':') if compact else None)
        extra = {'config_generation':config_generation} if config_generation is not None else None
        assert llm._replace_current(led, row, content, extra_meta=extra)


@pytest.mark.parametrize('writer', ['llm', 'rules'])
def test_first_adoption_bumps_real_scheduler_and_only_invalidates_current_patient_cache(source, monkeypatch, writer):
    led, row = source
    kind = llm.KIND if writer == 'llm' else extract.KIND
    historical = add(led, row, kind=kind, version='0.1')
    old_cache, current_cache, other_cache = _adoption_witness(led)
    _write_adopted(led, row, writer, {'summary':'合成の採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    assert _scheduler_payload(led.db) == {'analysis_epoch':1, 'source_epoch':7, 'other_owner':{'cursor':13}}
    ids = {r[0] for r in led.db.execute('SELECT artifact_id FROM artifacts')}
    assert {historical, old_cache, other_cache} <= ids and current_cache not in ids
    assert not led.db.in_transaction
    # Observe the committed witness from another connection, not the writer's cache.
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(sqlite3.connect(path)) as other:
        assert _scheduler_payload(other)['analysis_epoch'] == 1
        assert other.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (old_cache,)).fetchone()
        assert not other.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (current_cache,)).fetchone()


@pytest.mark.parametrize('writer', ['llm', 'rules'])
def test_json_format_and_operational_generation_do_not_reopen_adopted_input_but_meaning_does(source, monkeypatch, writer):
    led, row = source
    _adoption_witness(led)
    doc = {'summary':'合成の採用内容', 'v':extract.RULE_VERSION}
    _write_adopted(led, row, writer, doc, monkeypatch, config_generation='first')
    cache = led.artifact_add('patient_rollup', '{"proof":"still-current"}', project_id=1,
                            meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    kind = llm.KIND if writer == 'llm' else extract.KIND
    current = led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=? ORDER BY artifact_id DESC LIMIT 1', (kind,)).fetchone()
    # Rules now persist a separately validated intervention coverage field; retain
    # it while changing only JSON formatting and operational metadata.
    stored_doc = json.loads(led.db.execute('SELECT content FROM artifacts WHERE artifact_id=?',
                                           (current['artifact_id'],)).fetchone()[0])
    with led.db:
        led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
            (json.dumps(dict(reversed(list(stored_doc.items()))), ensure_ascii=True, indent=3),
             json.dumps({**json.loads(current['meta']), 'config_generation':'operator-changed'}), current['artifact_id']))
    _write_adopted(led, row, writer, doc, monkeypatch, compact=True, config_generation='next')
    assert _scheduler_payload(led.db)['analysis_epoch'] == 1
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (cache,)).fetchone()
    _write_adopted(led, row, writer, {**doc, 'summary':'合成の変更された採用内容'}, monkeypatch)
    assert _scheduler_payload(led.db)['analysis_epoch'] == 2
    assert _scheduler_payload(led.db)['source_epoch'] == 7
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (cache,)).fetchone()


@pytest.mark.parametrize('writer', ['llm', 'rules'])
def test_adoption_failure_rolls_back_source_artifact_epoch_and_cached_rollup_together(source, monkeypatch, writer):
    led, row = source
    _adoption_witness(led)
    _write_adopted(led, row, writer, {'summary':'合成の旧採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    led.artifact_add('patient_rollup', '{"proof":"keep-on-rollback"}', project_id=1,
                     meta={inference.VERSION_KEY:inference.CURRENT_VERSION})
    before_artifacts = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_jobs = [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')]
    rendered = []
    monkeypatch.setattr(llm, '_rerender_cards', lambda *a, **k: rendered.append(1))
    if writer == 'llm':
        def reject_comparison(*args, **kwargs):
            assert led.db.in_transaction and _scheduler_payload(led.db)['analysis_epoch'] == 2
            assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='patient_rollup' AND project_id=1 "
                                     "AND json_extract(meta,?)=?", ('$.'+inference.VERSION_KEY, inference.CURRENT_VERSION)).fetchone()
            raise RuntimeError('synthetic_shadow_write_failed')
        monkeypatch.setattr(llm, '_cloud_compare_tx', reject_comparison)
        changed = {**row, '_cloud_mode':'shadow'}
        with pytest.raises(RuntimeError, match='synthetic_shadow_write_failed'):
            _write_adopted(led, changed, writer, {'summary':'合成の新採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    else:
        original = led.bump_inference_analysis_epoch_tx
        def reject_after_epoch(pid):
            original(pid)
            assert led.db.in_transaction and _scheduler_payload(led.db)['analysis_epoch'] == 2
            raise RuntimeError('synthetic_adoption_witness_failed')
        monkeypatch.setattr(led, 'bump_inference_analysis_epoch_tx', reject_after_epoch)
        with pytest.raises(RuntimeError, match='synthetic_adoption_witness_failed'):
            _write_adopted(led, row, writer, {'summary':'合成の新採用内容', 'v':extract.RULE_VERSION}, monkeypatch)
    assert not led.db.in_transaction and rendered == []
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before_artifacts
    assert [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')] == before_jobs
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(sqlite3.connect(path)) as other:
        assert other.execute('SELECT * FROM artifacts ORDER BY artifact_id').fetchall() == before_artifacts
        assert other.execute('SELECT * FROM fetch_jobs ORDER BY job_id').fetchall() == before_jobs


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


def _intervention_row(led):
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_revision'] = led.db.execute(
        'SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
    row['_source_binding'] = dependency_loss.source_binding(led.db, row['project_id'], row['message_id'])
    return row


def _write_intervention_v1(led, row, writer):
    if writer == 'rules':
        assert extract._extract_rows(led, [row]) == (1, {row['project_id']})
    else:
        hints = llm._rule_hints(row)
        assert hints is not None and 'intervention_schedule' not in hints
        llm._ensure_v1(led, row, hints)
    stored = led.db.execute("SELECT content,meta FROM artifacts WHERE kind='extract_v1' "
                            "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    assert stored is not None
    content, meta = map(json.loads, stored)
    assert meta['rule_version'] == extract.RULE_VERSION
    assert meta['source_binding'] == row['_source_binding']
    if 'intervention_schedule' in content:
        assert content['intervention_schedule']['version'] == 1
        assert 'intervention_zero' not in meta
    return content.get('intervention_schedule')


@pytest.mark.parametrize('writer', ['rules', 'llm'])
@pytest.mark.parametrize('body,state', [
    ('看護師が2026年9月21日に本人を訪問する予定です。', 'planned'),
    ('看護師が明日本人を訪問する予定です。', 'planned_date_unknown'),
    ('看護師が2026年9月21日の本人への訪問を中止しました。', 'cancelled'),
    ('家族の話です。看護師が2026年9月21日に本人を訪問する予定です。', 'unknown'),
])
def test_both_v1_writers_store_compact_source_bound_interventions(source, writer, body, state):
    led, _ = source
    led.save_messages([_message(body=body)])
    row = _intervention_row(led)
    result = _write_intervention_v1(led, row, writer)
    [record] = result['records']
    assert record['state'] == state
    start, end = record['span']
    assert row['body_text'][start:end] in body
    assert 'quote' not in record and 'date_text' not in record and 'role_text' not in record
    assert result['coverage']['state'] == 'observed'
    assert result['coverage']['source_validation'] == 'verified'
    assert result['coverage']['source_complete'] is None
    assert result['coverage']['unknown'] == int(state == 'unknown')
    assert body not in json.dumps(result, ensure_ascii=False)


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


@pytest.mark.parametrize('writer', ['rules', 'llm'])
@pytest.mark.parametrize('fault, expected', [
    ('html_hash', 'invalid_source'), ('text_mismatch', 'invalid_source'),
    ('html_nul', 'invalid_source'), ('body_missing', 'incomplete'),
    ('body_large', 'oversize'), ('html_large', 'oversize'),
])
def test_v1_intervention_input_failure_remains_explicitly_unverified(source, writer, fault, expected):
    led, _ = source
    body = '看護師が2026年9月21日に本人を訪問する予定です。'
    led.save_messages([_message(body=body)])
    with led.db:
        if fault == 'html_hash':
            led.db.execute("UPDATE messages SET body_html='別の合成HTML' WHERE message_id=1")
        elif fault == 'text_mismatch':
            led.db.execute("UPDATE messages SET body_text='別の合成本文' WHERE message_id=1")
        elif fault == 'html_nul':
            led.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', ('x\0y',))
        elif fault == 'body_missing':
            led.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=1')
        elif fault == 'body_large':
            led.db.execute('UPDATE messages SET body_text=? WHERE message_id=1', ('x' * 20001,))
        else:
            led.db.execute('UPDATE messages SET body_html=? WHERE message_id=1', ('x' * 80001,))
    row = _intervention_row(led)
    result = _write_intervention_v1(led, row, writer)
    assert result['records'] == []
    assert result['coverage']['state'] == expected
    assert result['coverage']['source_validation'] == 'unverified'
    assert result['coverage']['source_complete'] is None


@pytest.mark.parametrize('writer', ['rules', 'llm'])
def test_v1_intervention_candidate_limit_is_explicit_and_preserves_unknown(source, writer):
    led, _ = source
    sentence = '看護師が2026年9月21日に本人を訪問する予定です。'
    led.save_messages([_message(body=sentence * 201)])
    row = _intervention_row(led)
    result = _write_intervention_v1(led, row, writer)
    assert len(result['records']) == 200
    assert result['coverage']['records_truncated'] is True
    assert result['coverage']['unknown'] == 200
    assert all(record['state'] == 'unknown' for record in result['records'])
    assert result['coverage']['source_complete'] is None


def test_rule_version_reprocessing_replaces_current_old_rule_without_touching_legacy(source):
    led, row = source
    old_legacy = add(led, row, kind=extract.KIND, version='0.1', content={'old': True},
                     rule_version=extract.RULE_VERSION - 1)
    old_current = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                      content={'old': True}, rule_version=extract.RULE_VERSION - 1)
    assert extract.run_pending(led)['done'] == 1
    rows = led.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_v1'").fetchall()
    assert old_legacy in {record['artifact_id'] for record in rows}
    assert old_current not in {record['artifact_id'] for record in rows}
    current = [record for record in rows if record['artifact_id'] != old_legacy]
    assert len(current) == 1
    assert json.loads(current[0]['meta'])['rule_version'] == extract.RULE_VERSION
    assert 'intervention_schedule' not in json.loads(current[0]['content'])
    assert json.loads(current[0]['meta'])['source_binding'] == row['_source_binding']
    assert json.loads(current[0]['meta'])['intervention_zero'] == 1
    assert 'intervention_zero' not in json.loads(next(r['meta'] for r in rows if r['artifact_id'] == old_legacy))


def test_ensure_v1_captures_replaced_dependencies_and_invalidates_current_patient_cache(source, monkeypatch):
    led, row = source
    _adoption_witness(led)
    prior = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                rule_version=extract.RULE_VERSION - 1, content={'v': extract.RULE_VERSION - 1})
    cache = led.artifact_add('patient_rollup', '{"proof":"synthetic"}', project_id=1,
                             meta={inference.VERSION_KEY: inference.CURRENT_VERSION})
    captured = []
    original = dependency_loss.capture_tx

    def observe(db, pid, mid, **kwargs):
        assert db.in_transaction
        captured.append(kwargs.get('artifact_ids'))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', observe)
    llm._ensure_v1(led, row, llm._rule_hints(row))
    assert [prior] in captured
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (prior,)).fetchone()
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (cache,)).fetchone()
    assert _scheduler_payload(led.db)['analysis_epoch'] == 1
    captured.clear()
    llm._ensure_v1(led, row, llm._rule_hints(row))
    assert captured == [] and _scheduler_payload(led.db)['analysis_epoch'] == 1


def test_ensure_v1_dependency_failure_rolls_back_replacement_epoch_and_cache(source, monkeypatch):
    led, row = source
    _adoption_witness(led)
    add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
        rule_version=extract.RULE_VERSION - 1, content={'v': extract.RULE_VERSION - 1})
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    jobs = [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')]

    def fail(db, footprint, **kwargs):
        assert db.in_transaction and kwargs['replacement_id'] is not None
        raise RuntimeError('synthetic_dependency_write_failed')

    monkeypatch.setattr(dependency_loss, 'record_tx', fail)
    with pytest.raises(RuntimeError, match='synthetic_dependency_write_failed'):
        llm._ensure_v1(led, row, llm._rule_hints(row))
    assert not led.db.in_transaction
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')] == jobs


def test_ensure_v1_borrows_transaction_without_committing_caller_changes(source):
    led, row = source
    old = led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0]
    new = 0 if old else 1
    led.db.execute('BEGIN')
    try:
        led.db.execute('UPDATE messages SET is_unread=? WHERE message_id=1', (new,))
        llm._ensure_v1(led, row, llm._rule_hints(row))
        assert led.db.in_transaction
        assert led.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_v1'").fetchone()
        assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == new
    finally:
        led.db.rollback()
    assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_v1'").fetchone()
    assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == old


def test_ensure_v1_savepoint_failure_preserves_caller_transaction(source, monkeypatch):
    led, row = source
    old = led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0]
    new = 0 if old else 1
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic_v1_failure')
    monkeypatch.setattr(dependency_loss, 'record_tx', fail)
    led.db.execute('BEGIN')
    try:
        led.db.execute('UPDATE messages SET is_unread=? WHERE message_id=1', (new,))
        with pytest.raises(RuntimeError, match='synthetic_v1_failure'):
            llm._ensure_v1(led, row, llm._rule_hints(row))
        assert led.db.in_transaction
        assert not led.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_v1'").fetchone()
        assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == new
    finally:
        led.db.rollback()


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


@pytest.mark.parametrize('writer', ['rules', 'llm'])
def test_v1_replacement_preserves_foreign_corrupt_row_and_replaces_null_scope(source, writer, monkeypatch):
    led, row = source
    metadata = {'hash': row['content_hash'], inference.VERSION_KEY: inference.CURRENT_VERSION,
                'source_binding': row['_source_binding'], 'rule_version': extract.RULE_VERSION - 1}
    null_id = led.artifact_add(extract.KIND, '{"legacy":"synthetic"}', project_id=None,
                               message_id=1, meta=metadata)
    foreign = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                  rule_version=extract.RULE_VERSION, source_binding=row['_source_binding'],
                  content={'foreign': 'synthetic'})
    with led.db:
        # Simulate a corrupted legacy DB, without weakening the relation guard.
        led.db.execute('DROP TRIGGER g1_artifacts_msg_upd')
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    captured = []
    original = dependency_loss.capture_tx

    def observe(db, pid, mid, **kwargs):
        if kwargs.get('artifact_ids') is not None:
            captured.append(list(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', observe)
    if writer == 'rules':
        # Same-version foreign row must not stop the normal pending selection.
        assert extract.run_pending(led)['done'] == 1
    else:
        llm._ensure_v1(led, row, llm._rule_hints(row))
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (null_id,)).fetchone()
    assert [null_id] in captured and all(foreign not in ids for ids in captured)
    current = led.db.execute("SELECT a.artifact_id,a.project_id FROM artifacts a JOIN messages m USING(message_id) "
                             "WHERE a.kind=? " + mcs_queries.current_extract_pred(), (extract.KIND,)).fetchall()
    assert len(current) == 1 and current[0]['project_id'] == row['project_id']
    if writer == 'llm':
        before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        llm._ensure_v1(led, row, llm._rule_hints(row))
        assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_stale_ineligible_cleanup_does_not_delete_foreign_corrupt_artifact(source):
    led, row = source
    local = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                rule_version=extract.RULE_VERSION - 1)
    foreign = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                  rule_version=extract.RULE_VERSION - 1)
    with led.db:
        led.db.execute('DROP TRIGGER g1_artifacts_msg_upd')
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
        led.db.execute("UPDATE messages SET body_state='snippet' WHERE message_id=1")
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    extract._delete_stale(led)
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == before
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (local,)).fetchone()


@pytest.mark.parametrize('writer', ['rules', 'llm'])
@pytest.mark.parametrize('fault', ['ignore', 'content', 'meta', 'remove'])
@pytest.mark.parametrize('borrowed', [False, True])
def test_v1_insert_must_survive_trigger_before_superseding_prior(source, writer, fault, borrowed):
    led, row = source
    _adoption_witness(led)
    prior = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
                rule_version=extract.RULE_VERSION - 1, content={'old': 'synthetic'})
    if fault == 'ignore':
        trigger = ("CREATE TRIGGER synthetic_v1_insert_fault BEFORE INSERT ON artifacts "
                   "WHEN NEW.kind='extract_v1' BEGIN SELECT RAISE(IGNORE); END")
    else:
        mutation = {'content': "UPDATE artifacts SET content='{}' WHERE artifact_id=NEW.artifact_id;",
                    'meta': "UPDATE artifacts SET meta='{}' WHERE artifact_id=NEW.artifact_id;",
                    'remove': 'DELETE FROM artifacts WHERE artifact_id=NEW.artifact_id;'}[fault]
        trigger = ("CREATE TRIGGER synthetic_v1_insert_fault AFTER INSERT ON artifacts "
                   "WHEN NEW.kind='extract_v1' BEGIN " + mutation + ' END')
    led.db.execute(trigger)
    led.db.commit()
    artifacts_before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    jobs_before = [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')]
    old_unread = led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0]
    changed_unread = 0 if old_unread else 1
    if borrowed:
        led.db.execute('BEGIN')
        led.db.execute('UPDATE messages SET is_unread=? WHERE message_id=1', (changed_unread,))
    try:
        if writer == 'rules' and borrowed:
            # The rule writer explicitly rejects a caller-owned transaction.
            with pytest.raises(ValueError, match='extract_transaction_owned'):
                extract._extract_rows(led, [row])
        else:
            with pytest.raises(RuntimeError, match='extract_v1_insert_unverified'):
                if writer == 'rules':
                    extract._extract_rows(led, [row])
                else:
                    llm._ensure_v1(led, row, llm._rule_hints(row))
        assert led.db.in_transaction is borrowed
        assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == artifacts_before
        assert [tuple(r) for r in led.db.execute('SELECT * FROM fetch_jobs ORDER BY job_id')] == jobs_before
        assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (prior,)).fetchone()
        assert led.db.execute('SELECT is_unread FROM messages WHERE message_id=1').fetchone()[0] == (
            changed_unread if borrowed else old_unread)
    finally:
        if borrowed:
            led.db.rollback()


def test_v1_same_current_artifact_remains_noop_when_insert_would_be_ignored(source):
    led, row = source
    _write_intervention_v1(led, row, 'llm')
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_ignore_v1 BEFORE INSERT ON artifacts "
                   "WHEN NEW.kind='extract_v1' BEGIN SELECT RAISE(IGNORE); END")
    led.db.commit()
    llm._ensure_v1(led, row, llm._rule_hints(row))
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_llm_replace_preserves_foreign_current_artifact_without_capturing_it(source, monkeypatch):
    led, row = source
    led.ensure_patient(2)
    old = add(led, row)  # no public version is retained as legacy history
    future = add(led, row, version='2.0')
    current = add(led, row, version=inference.CURRENT_VERSION)
    foreign = add(led, row, version=inference.CURRENT_VERSION)
    # Simulate the supported existing-DB shadow state without weakening or
    # dropping the product trigger. This is a fully synthetic database.
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    captured = []
    original = dependency_loss.capture_tx

    def track(db, pid, mid, **kwargs):
        captured.append(tuple(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', track)
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (old,)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (future,)).fetchone()
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (current,)).fetchone()
    new_id = led.db.execute('SELECT MAX(artifact_id) FROM artifacts WHERE kind=?', (llm.KIND,)).fetchone()[0]
    assert captured == [(current,), (new_id,)]
    assert all(foreign not in ids for ids in captured)


def test_llm_replace_captures_and_replaces_legacy_null_scope(source, monkeypatch):
    led, row = source
    null_id = add(led, row, version=inference.CURRENT_VERSION)
    with led.db:
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (null_id,))
    captured = []
    original = dependency_loss.capture_tx

    def track(db, pid, mid, **kwargs):
        captured.append(tuple(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', track)
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    new_id = led.db.execute('SELECT MAX(artifact_id) FROM artifacts WHERE kind=?', (llm.KIND,)).fetchone()[0]
    assert captured == [(null_id,), (new_id,)]
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (null_id,)).fetchone()
    rows = led.db.execute('SELECT project_id,message_id FROM artifacts WHERE kind=?', (llm.KIND,)).fetchall()
    assert [tuple(item) for item in rows] == [(row['project_id'], row['message_id'])]


def test_llm_scoped_replacement_rolls_back_on_dependency_failure(source, monkeypatch):
    led, row = source
    old = add(led, row, version=inference.CURRENT_VERSION)
    with led.db:
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (old,))
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]

    def fail(*_args, **_kwargs):
        raise RuntimeError('synthetic dependency failure')

    monkeypatch.setattr(dependency_loss, 'record_tx', fail)
    with pytest.raises(RuntimeError, match='synthetic dependency failure'):
        llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('event', ['INSERT', 'DELETE'])
def test_llm_replacement_rolls_back_when_trigger_changes_source_occurrence(source, event):
    led, row = source
    add(led, row, version=inference.CURRENT_VERSION)
    before_artifacts = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_source = tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    ref = 'NEW' if event == 'INSERT' else 'OLD'
    led.db.execute(f"CREATE TRIGGER synthetic_source_race AFTER {event} ON artifacts "
                   f"WHEN {ref}.kind='extract_llm' AND {ref}.message_id=1 BEGIN "
                   "UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_source_changed_during_write'):
        llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()) == before_source
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before_artifacts


def test_llm_concurrent_source_writer_does_not_replace_stale_result(source):
    led, row = source
    old = add(led, row, version=inference.CURRENT_VERSION)
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone())
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    led.db.execute('PRAGMA busy_timeout=1')
    with closing(sqlite3.connect(db_path)) as other:
        try:
            other.execute('BEGIN IMMEDIATE')
            other.execute("UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1")
            with pytest.raises(sqlite3.OperationalError):
                llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
            assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone()) == before
            other.commit()
            assert not llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
            assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old,)).fetchone()) == before
        finally:
            led.db.execute('PRAGMA busy_timeout=30000')


def _scoped_error_rows(led, row):
    led.ensure_patient(2)
    meta = {'error': True, 'attempts': 5, 'auto_retry': 0}
    owned = add(led, row, version=inference.CURRENT_VERSION, **meta)
    null_scope = add(led, row, version=inference.CURRENT_VERSION, **meta)
    foreign = add(led, row, version=inference.CURRENT_VERSION, **meta)
    legacy = add(led, row, **meta)
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (null_scope,))
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
        led.db.execute('UPDATE artifacts SET created_at=0 WHERE artifact_id IN (?,?,?,?)',
                       (owned, null_scope, foreign, legacy))
    return owned, null_scope, foreign, legacy


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


def test_stale_gc_preserves_foreign_and_legacy_and_captures_null(source, monkeypatch):
    from contextlib import nullcontext
    led, row = source
    owned, null_scope, foreign, legacy = _scoped_error_rows(led, row)
    with led.db:
        for aid in (owned, null_scope, foreign, legacy):
            data = json.loads(led.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone()[0])
            data['hash'] = 'synthetic-stale'
            led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(data), aid))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    captured = []
    original = dependency_loss.capture_tx

    def track(db, pid, mid, **kwargs):
        captured.append(tuple(kwargs['artifact_ids']))
        return original(db, pid, mid, **kwargs)

    monkeypatch.setattr(dependency_loss, 'capture_tx', track)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (?,?)', (owned, null_scope)).fetchone()
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (legacy,)).fetchone()
    assert (owned,) in captured and (null_scope,) in captured
    assert all(foreign not in ids for ids in captured)


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


def test_hold_chunk_cleanup_retains_foreign_and_other_generation(source):
    led, row = source
    led.ensure_patient(2)
    prior = add(led, row, version=inference.CURRENT_VERSION)
    owned = add(led, row, kind='extract_llm_chunk', version=inference.CURRENT_VERSION)
    null_scope = add(led, row, kind='extract_llm_chunk', version=inference.CURRENT_VERSION)
    foreign = add(led, row, kind='extract_llm_chunk', version=inference.CURRENT_VERSION)
    legacy = add(led, row, kind='extract_llm_chunk')
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (null_scope,))
        led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (foreign,))
    foreign_before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone())
    with led.db:
        llm._record_hold_tx(led, row, {'artifact_id': prior}, 'qc_failed')
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (?,?)', (owned, null_scope)).fetchone()
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (foreign,)).fetchone()) == foreign_before
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (legacy,)).fetchone()


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


@pytest.mark.parametrize('writer', ['llm', 'rule'])
@pytest.mark.parametrize('phase', ['insert', 'delete'])
@pytest.mark.parametrize('corruption', ['delete', 'content', 'model', 'meta', 'created_at'])
def test_writer_rolls_back_when_trigger_destroys_or_changes_new_artifact(source, writer, phase, corruption):
    led, row = source
    kind = llm.KIND if writer == 'llm' else extract.KIND
    kwargs = {'rule_version': extract.RULE_VERSION - 1} if writer == 'rule' else {}
    old = add(led, row, kind=kind, version=inference.CURRENT_VERSION, **kwargs)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    event = 'INSERT' if phase == 'insert' else 'DELETE'
    target = ('NEW.artifact_id' if phase == 'insert' else
              f"(SELECT MAX(artifact_id) FROM artifacts WHERE kind='{kind}' AND message_id=1)")
    predicate = f"NEW.kind='{kind}' AND NEW.message_id=1" if phase == 'insert' else f'OLD.artifact_id={old}'
    if corruption == 'delete':
        mutation = f'DELETE FROM artifacts WHERE artifact_id={target};'
    else:
        value = '12345' if corruption == 'created_at' else "'synthetic_modified'"
        mutation = f'UPDATE artifacts SET {corruption}={value} WHERE artifact_id={target};'
    led.db.execute(f'CREATE TRIGGER synthetic_result_race AFTER {event} ON artifacts '
                   f'WHEN {predicate} BEGIN {mutation} END')
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_.*insert_unverified'):
        if writer == 'llm':
            llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
        else:
            llm._ensure_v1(led, row, llm._rule_hints(row))
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('phase', ['INSERT', 'DELETE'])
@pytest.mark.parametrize('borrowed', [False, True])
def test_rule_writer_source_trigger_rolls_back_without_destroying_borrowed_transaction(source, phase, borrowed):
    led, row = source
    led.ensure_patient(1)
    old = add(led, row, kind=extract.KIND, version=inference.CURRENT_VERSION,
              rule_version=extract.RULE_VERSION - 1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_source = tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    predicate = "NEW.kind='extract_v1' AND NEW.message_id=1" if phase == 'INSERT' else f'OLD.artifact_id={old}'
    led.db.execute(f'CREATE TRIGGER synthetic_v1_source_race AFTER {phase} ON artifacts WHEN {predicate} BEGIN '
                   "UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1; END")
    led.db.commit()
    if borrowed:
        led.db.execute('BEGIN IMMEDIATE')
        led.db.execute("UPDATE patients SET disease='合成の既存変更' WHERE project_id=1")
    with pytest.raises(RuntimeError, match='extract_source_changed_during_write'):
        llm._ensure_v1(led, row, llm._rule_hints(row))
    assert tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()) == before_source
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    if borrowed:
        assert led.db.in_transaction
        assert led.db.execute('SELECT disease FROM patients WHERE project_id=1').fetchone()[0] == '合成の既存変更'
        led.db.rollback()


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


@pytest.mark.parametrize('alter', ['content', 'meta', 'source'])
def test_gc_earlier_delete_trigger_cannot_delete_changed_later_candidate(source, monkeypatch, alter):
    from contextlib import nullcontext
    led, row = source
    led.save_messages([_message(mid=2, body='別の合成記録。')])
    row2 = dict(led.db.execute('SELECT * FROM messages WHERE message_id=2').fetchone())
    first = add(led, row, version=inference.CURRENT_VERSION, hash='stale1')
    second = add(led, row2, version=inference.CURRENT_VERSION, hash='stale2')
    if alter == 'content':
        action = f"UPDATE artifacts SET content='{{}}' WHERE artifact_id={second};"
    elif alter == 'meta':
        action = f"UPDATE artifacts SET meta='{{}}' WHERE artifact_id={second};"
    else:
        action = "UPDATE messages SET content_hash='new-synthetic-hash' WHERE message_id=2;"
    led.db.execute(f'CREATE TRIGGER synthetic_gc_race AFTER DELETE ON artifacts WHEN OLD.artifact_id={first} '
                   f'BEGIN {action} END')
    led.db.commit()
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (first,)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (second,)).fetchone()


def test_gc_dependency_change_after_capture_rolls_back_all_deletions(source, monkeypatch):
    from contextlib import nullcontext
    led, row = source
    target = add(led, row, version=inference.CURRENT_VERSION, hash='stale')
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    original = dependency_loss.record_tx

    def modify_after_record(db, footprint, **kwargs):
        result = original(db, footprint, **kwargs)
        db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (target,))
        return result

    monkeypatch.setattr(dependency_loss, 'record_tx', modify_after_record)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    with pytest.raises(RuntimeError, match='extract_stale_gc_unverified'):
        llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('outcome', ['written', 'held'])
@pytest.mark.parametrize('mutation', ['source', 'result'])
def test_checkpoint_cleanup_trigger_cannot_invalidate_result_or_hold(source, outcome, mutation):
    led, row = source
    led.ensure_patient(1)
    if outcome == 'held':
        add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
            'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    chunk = {'summary': '合成結果', '_quality_result': {
        'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
    assert llm._persist_chunks(led, row, {0: chunk})
    assert llm._claim(led, row) is not None
    llm._capture_checkpoint_state(led.db, row, None)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    before_source = tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    if mutation == 'source':
        action = "UPDATE messages SET sender_name='別の合成送信者' WHERE message_id=1;"
    elif outcome == 'written':
        action = "UPDATE artifacts SET content='{}' WHERE kind='extract_llm' AND message_id=1;"
    else:
        action = "UPDATE artifacts SET content='{}' WHERE kind='extract_upgrade_v1' AND message_id=1;"
    led.db.execute("CREATE TRIGGER synthetic_checkpoint_race AFTER DELETE ON artifacts "
                   "WHEN OLD.kind='extract_llm_chunk' AND OLD.message_id=1 BEGIN " + action + ' END')
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_(source_changed_during_write|insert_unverified)'):
        llm._commit_extraction_atomic(led, row, '{"summary":"合成候補"}', None, None, None, None,
                                      (0,), check_current=False)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert tuple(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()) == before_source


@pytest.mark.parametrize('field,limit', [('content', 262144), ('meta', 32768)])
def test_stale_gc_keeps_oversize_witness_without_blocking_other_rows(source, monkeypatch, field, limit):
    from contextlib import nullcontext
    led, row = source
    retained = add(led, row, version=inference.CURRENT_VERSION, hash='stale')
    removable = add(led, row, version=inference.CURRENT_VERSION, hash='stale')
    if field == 'content':
        replacement = json.dumps({'padding': 'x' * (limit + 1)})
    else:
        replacement = json.dumps({inference.VERSION_KEY: inference.CURRENT_VERSION,
                                  'hash': 'stale', 'padding': 'x' * (limit + 1)})
    with led.db:
        led.db.execute('UPDATE artifacts SET ' + field + '=? WHERE artifact_id=?', (replacement, retained))
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (retained,)).fetchone())
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False)
    assert tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (retained,)).fetchone()) == before
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (removable,)).fetchone()


@pytest.mark.parametrize('stage', ['old_delete', 'checkpoint_delete'])
def test_later_same_scope_insert_does_not_replace_exact_written_witness(source, stage):
    led, row = source
    old = add(led, row, version=inference.CURRENT_VERSION)
    if stage == 'checkpoint_delete':
        led.ensure_patient(1)
        chunk = {'summary': '合成結果', '_quality_result': {
            'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
        assert llm._persist_chunks(led, row, {0: chunk})
        assert llm._claim(led, row) is not None
        llm._capture_checkpoint_state(led.db, row, None)
        condition = "OLD.kind='extract_llm_chunk' AND OLD.message_id=1"
    else:
        condition = f'OLD.artifact_id={old}'
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_duplicate_result AFTER DELETE ON artifacts WHEN " + condition + " BEGIN "
                   "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                   "SELECT kind,project_id,message_id,'{}',model,meta,created_at FROM artifacts "
                   "WHERE kind='extract_llm' AND project_id=1 AND message_id=1 "
                   "ORDER BY artifact_id DESC LIMIT 1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        if stage == 'old_delete':
            llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
        else:
            llm._commit_extraction_atomic(led, row, '{"summary":"合成の経過報告"}',
                                          None, None, None, None, (0,), check_current=False)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def _add_stale_rows(led, row, count):
    return [add(led, row, version=inference.CURRENT_VERSION, hash='synthetic-stale') for _ in range(count)]


def _gc_tick(led, monkeypatch, **kwargs):
    from contextlib import nullcontext
    return llm._gc_stale_artifacts(led, lambda *_args, **_kwargs: nullcontext(True), False, **kwargs)


def test_gc_fixed_highwater_resumes_and_preserves_claims_until_pass_completed(source, monkeypatch):
    led, row = source
    led.ensure_patient(2)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_PAGE', 2)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    targets = _add_stale_rows(led, row, 5)
    now = llm.time.time()
    with led.db:
        led.db.execute("UPDATE ledger_relation_guards SET mode='shadow' WHERE relation='artifacts'")
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                       "VALUES('extract_claim',1,11,?,'done')", (now - 90000,))
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                       "VALUES('extract_claim',1,12,?,'done')", (now + 90000,))
    cursors = []
    newly_added = []
    for _ in range(5):
        _gc_tick(led, monkeypatch)
        state = json.loads(led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                          (llm._STALE_GC_KIND,)).fetchone()[0])
        if state['highwater'] == 0:
            break
        cursors.append(state['cursor'])
        assert led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=11").fetchone()
        noise = add(led, row, version=inference.CURRENT_VERSION, hash='synthetic-stale')
        with led.db:
            led.db.execute('UPDATE artifacts SET project_id=2 WHERE artifact_id=?', (noise,))
        newly_added.extend(_add_stale_rows(led, row, 1))
    assert len(cursors) >= 2 and all(x < y for x, y in zip(cursors, cursors[1:]))
    assert state == {'version': 1, 'cursor': 0, 'highwater': 0}
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (' + ','.join('?' for _ in targets) + ')', targets).fetchone()
    assert all(led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (aid,)).fetchone() for aid in newly_added)
    assert not led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=11").fetchone()
    assert led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=12").fetchone()
    assert llm._stale_gc_at > 0
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    _gc_tick(led, monkeypatch)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


@pytest.mark.parametrize('bad', [
    '{"version":1,"version":1,"cursor":0,"highwater":0}',
    '{"version":true,"cursor":0,"highwater":0}',
    '{"version":1,"cursor":1,"highwater":0}',
    '{"version":1,"cursor":0,"highwater":0,"extra":1}',
    'x' * 1025,
])
def test_gc_bad_cursor_fails_closed(source, monkeypatch, bad):
    led, row = source
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    _add_stale_rows(led, row, 1)
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state) VALUES(?,0,0,?,'done')",
                       (llm._STALE_GC_KIND, bad))
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    with pytest.raises(ValueError, match='extract_gc_cursor_invalid'):
        _gc_tick(led, monkeypatch)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone()[0] == bad


def test_gc_deadline_crossed_after_delete_rolls_back_page_and_cursor(source, monkeypatch):
    led, row = source
    _add_stale_rows(led, row, 1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    clock = {'value': 0.0}
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock['value'])
    original = dependency_loss.record_tx

    def exceed(db, footprint, **kwargs):
        result = original(db, footprint, **kwargs)
        clock['value'] = 2.0
        return result

    monkeypatch.setattr(dependency_loss, 'record_tx', exceed)
    _gc_tick(led, monkeypatch, deadline=1.0)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None
    assert llm._stale_gc_at == 0


def test_gc_cursor_insert_ignored_rolls_back_deletions(source, monkeypatch):
    led, row = source
    _add_stale_rows(led, row, 1)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    led.db.execute("CREATE TRIGGER synthetic_gc_cursor_ignore BEFORE INSERT ON fetch_jobs "
                   "WHEN NEW.kind='extract_stale_gc' BEGIN SELECT RAISE(IGNORE); END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_gc_cursor_unverified'):
        _gc_tick(led, monkeypatch)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None


def test_gc_writer_busy_preserves_page_and_resumes_after_release(source, monkeypatch):
    led, row = source
    target = _add_stale_rows(led, row, 1)[0]
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    led.db.execute('PRAGMA busy_timeout=1')
    with closing(sqlite3.connect(db_path)) as other:
        try:
            other.execute('BEGIN IMMEDIATE')
            with pytest.raises(sqlite3.OperationalError):
                _gc_tick(led, monkeypatch)
            assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
            assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None
            other.rollback()
            _gc_tick(led, monkeypatch)
            assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
        finally:
            led.db.execute('PRAGMA busy_timeout=30000')


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


def test_fail_checkpoint_cleanup_cannot_corrupt_new_error(source):
    led, row = source
    led.ensure_patient(1)
    add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
        'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5)
    chunk = {'summary': '合成結果', '_quality_result': {
        'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
    assert llm._persist_chunks(led, row, {0: chunk})
    lease = llm._claim(led, row)
    assert lease is not None
    llm._capture_checkpoint_state(led.db, row, None)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_fail_chunk_race AFTER DELETE ON artifacts "
                   "WHEN OLD.kind='extract_llm_chunk' AND OLD.message_id=1 BEGIN "
                   "UPDATE artifacts SET content='{}' WHERE kind='extract_llm' AND message_id=1 "
                   "AND json_valid(meta) AND json_extract(meta,'$.error')=1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        llm._fail(led, row, 5, lease=lease)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_gc_reserves_cursor_and_slow_candidate_is_revisited_after_later_rows(source, monkeypatch):
    led, row = source
    targets = _add_stale_rows(led, row, 4)
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_PAGE', 4)
    clock = {'mono': 0.0, 'wall': 1000.0}
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock['mono'])
    monkeypatch.setattr(llm.time, 'time', lambda: clock['wall'])
    original = dependency_loss.record_tx
    calls = []

    def slow(db, footprint, **kwargs):
        result = original(db, footprint, **kwargs)
        calls.append(footprint['message_id'])
        clock['mono'] += 0.07
        return result

    monkeypatch.setattr(dependency_loss, 'record_tx', slow)
    _gc_tick(led, monkeypatch)
    state = json.loads(led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                      (llm._STALE_GC_KIND,)).fetchone()[0])
    assert state['cursor'] == targets[2] and state['highwater'] == targets[3]
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id IN (?,?)', targets[:2]).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[2],)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[3],)).fetchone()
    clock['mono'] = 0.0
    _gc_tick(led, monkeypatch)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[3],)).fetchone()
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[2],)).fetchone()
    assert json.loads(led.db.execute('SELECT payload FROM fetch_jobs WHERE kind=?',
                                     (llm._STALE_GC_KIND,)).fetchone()[0])['highwater'] == 0
    clock['wall'] += llm._STALE_GC_S + 1
    clock['mono'] = 0.0
    _gc_tick(led, monkeypatch)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (targets[2],)).fetchone()
    assert len(calls) == 5


def test_gc_claim_cleanup_is_limited_per_completed_pass(source, monkeypatch):
    led, _ = source
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    clock = {'wall': 100000.0}
    monkeypatch.setattr(llm.time, 'time', lambda: clock['wall'])
    with led.db:
        led.db.executemany("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                           "VALUES('extract_claim',1,?,?,'done')",
                           [(mid, clock['wall'] - 90000) for mid in range(100, 250)])
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,next_try,state) "
                       "VALUES('extract_claim',1,300,?,'done')", (clock['wall'] + 90000,))
    for remaining in (86, 22, 0):
        _gc_tick(led, monkeypatch)
        actual = led.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim' "
                                "AND message_id BETWEEN 100 AND 249").fetchone()[0]
        assert actual == remaining
        assert led.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim' AND message_id=300").fetchone()
        clock['wall'] += llm._STALE_GC_S + 1


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


@pytest.mark.parametrize('caller', ['replace', 'fail'])
def test_hold_checkpoint_cleanup_cannot_corrupt_receipt(source, caller):
    led, row = source
    led.ensure_patient(1)
    add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
        'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    if caller == 'fail':
        add(led, row, version=inference.CURRENT_VERSION, error=True, attempts=5)
    chunk = {'summary': '合成結果', '_quality_result': {
        'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}
    assert llm._persist_chunks(led, row, {0: chunk})
    lease = llm._claim(led, row)
    assert lease is not None
    llm._capture_checkpoint_state(led.db, row, None)
    before = [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    led.db.execute("CREATE TRIGGER synthetic_hold_cleanup AFTER DELETE ON artifacts "
                   "WHEN OLD.kind='extract_llm_chunk' AND OLD.message_id=1 BEGIN "
                   "UPDATE artifacts SET content='{}' WHERE kind='extract_upgrade_v1' AND message_id=1; END")
    led.db.commit()
    with pytest.raises(RuntimeError, match='extract_insert_unverified'):
        if caller == 'replace':
            llm._commit_extraction_atomic(led, row, '{"summary":"合成候補"}', None, None, None, None,
                                          (0,), check_current=False)
        else:
            llm._fail(led, row, 5, lease=lease)
    assert [tuple(a) for a in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before


def test_hold_invalid_old_receipt_is_preserved_and_cannot_replace_current_receipt(source):
    led, row = source
    prior = add(led, row, content={'symptoms': [{'text': '発熱', 'negated': True,
        'subject': 'patient', 'status': 'ongoing', 'evidence': '本人は発熱なし。'}]})
    with led.db:
        llm._record_hold_tx(led, row, {'artifact_id': prior}, 'protected_loss')
    old = led.db.execute("SELECT * FROM artifacts WHERE kind='extract_upgrade_v1'").fetchone()
    meta = json.loads(old['meta'])
    meta['invalidated'] = True
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), old['artifact_id']))
    before = tuple(led.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (old['artifact_id'],)).fetchone())
    with led.db:
        llm._record_hold_tx(led, row, {'artifact_id': prior}, 'protected_loss')
    rows = led.db.execute("SELECT * FROM artifacts WHERE kind='extract_upgrade_v1' ORDER BY artifact_id").fetchall()
    assert len(rows) == 2 and tuple(rows[0]) == before
    assert not json.loads(rows[1]['meta']).get('invalidated')


def test_bad_gc_cursor_reports_fixed_error_and_new_extraction_can_continue(source, monkeypatch):
    led, _ = source
    led.ensure_patient(1)
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state) VALUES(?,0,0,?,'done')",
                       (llm._STALE_GC_KIND, '{bad'))
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    monkeypatch.setattr(llm, 'llm_extract', lambda *_args, **_kwargs: {'summary': '合成結果'})
    result = llm.run_pending(led, limit=1, budget_s=180)
    assert result.get('gc_error') == 'extract_stale_gc_failed'
    assert result['done'] == 1
    assert led.db.execute("SELECT payload FROM fetch_jobs WHERE kind=?", (llm._STALE_GC_KIND,)).fetchone()[0] == '{bad'
    with led.db:
        led.db.execute('DELETE FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,))
    result = llm.run_pending(led, limit=1, budget_s=180)
    assert 'gc_error' not in result


def test_gc_error_with_unclosed_transaction_is_not_hidden(source, monkeypatch):
    led, _ = source
    def broken(ledger, *_args, **_kwargs):
        ledger.db.execute('BEGIN IMMEDIATE')
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(llm, '_gc_stale_artifacts', broken)
    try:
        with pytest.raises(RuntimeError, match='synthetic failure'):
            llm.run_pending(led, limit=1, budget_s=180)
        assert led.db.in_transaction
    finally:
        led.db.rollback()


@pytest.mark.parametrize(("budget", "elapsed"), [(10, 0.25), (0.125, 0.125)])
def test_gc_busy_lock_obeys_local_deadline_and_selection_continues(source, monkeypatch, budget, elapsed):
    led, row = source
    target = _add_stale_rows(led, row, 1)[0]
    clock = {'value': 100.0}
    attempts = []
    monkeypatch.setattr(llm, '_stale_gc_at', 0)
    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 0.25)
    monkeypatch.setattr(llm, '_writer_current', lambda **_kwargs: True)
    monkeypatch.setattr(llm.time, 'monotonic', lambda: clock['value'])
    monkeypatch.setattr(llm.time, 'sleep', lambda seconds: clock.update(value=clock['value'] + seconds))
    monkeypatch.setattr(llm, 'acquire_run_lock', lambda: attempts.append(clock['value']))

    class SelectionReached(Exception):
        pass

    original_select = llm._select_pending
    selected_ids = []

    def selected(*args, **kwargs):
        rows, _fresh = original_select(*args, **kwargs)
        selected_ids.extend(row['message_id'] for row in rows)
        raise SelectionReached

    monkeypatch.setattr(llm, '_select_pending', selected)
    with pytest.raises(SelectionReached):
        llm.run_pending(led, limit=1, budget_s=budget, per_write_lock=True, write_wait_s=60)
    assert selected_ids == [row['message_id']]
    assert attempts and clock['value'] == pytest.approx(100 + elapsed)
    assert llm._stale_gc_at == 0 and not led.db.in_transaction
    assert led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
    assert led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (llm._STALE_GC_KIND,)).fetchone() is None

    monkeypatch.setattr(llm, '_STALE_GC_BUDGET_S', 60)
    _gc_tick(led, monkeypatch)
    assert not led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (target,)).fetchone()
    assert llm._stale_gc_at > 0
