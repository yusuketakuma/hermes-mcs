"""Synthetic public-generation boundaries for both legacy extraction writers."""
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

import extract
import extract_llm as llm
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
    assert led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
    assert llm._replace_current(led, row, '{"summary":"合成の経過報告"}')
    assert led.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (old,)).fetchone()
    assert llm._current(led, 1, row["content_hash"])
    assert not led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
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
    assert led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()


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
    assert not led.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()


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
    llm._gc_stale_artifacts(led, lambda *a: nullcontext(True), False)
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
    _synthetic_inference(monkeypatch, lambda *a, **k: {"summary": "合成の経過報告"})
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
        ({"summary": "合成"} if len(calls) == 1 else None))
    meta = {}
    assert llm.llm_extract(body, meta_out=meta) is None
    assert len(meta["completeness"]) == 1 and "completeness_chunks" not in meta
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


def test_event_record_and_legacy_projection_count_as_one_thin_fact(source):
    led, _ = source
    led.save_messages([_message(body="本人は退院しました。" + "合成の経過共有です。" * 40)])
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(led.db,row['project_id'],row['message_id'])
    aid = add(led, row, version=inference.CURRENT_VERSION, content={"events": ["discharge"],
        "event_records": [{"kind": "discharge", "subject": "patient", "status": "current",
            "negated": False, "evidence": "本人は退院しました。"}]})
    assert led.db.execute("SELECT (" + llm._thin_pending_sql() + ") FROM messages m").fetchone()[0] == aid


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
    with led.db:
        led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
            (json.dumps(dict(reversed(list(doc.items()))), ensure_ascii=True, indent=3),
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
    assert led.db.execute('SELECT 1 FROM messages m WHERE '+llm.pending_pred()).fetchone()
    assert old_ids <= {r[0] for r in led.db.execute('SELECT artifact_id FROM artifacts')}
    assert llm._replace_current(led,fresh,'{}')  # A new valid empty result stays empty; no old-result fallback.
    current = led.db.execute('SELECT a.content FROM artifacts a JOIN messages m USING(message_id) '
        'WHERE a.kind=? '+mcs_queries.current_extract_pred(),(llm.KIND,)).fetchall()
    assert [json.loads(r[0]) for r in current]==[{}]


def test_a_prepared_frame_cannot_publish_after_its_other_source_reference_changes(source):
    import patient_state_context
    from test_pharmacy_state import context
    led, target = source
    body = '本人の合成薬Aの残薬は3錠。'
    led.save_messages([_message(mid=2,body=body,posted_at='2026-09-01T10:00:00+09:00')])
    refhash = led.db.execute('SELECT content_hash FROM messages WHERE message_id=2').fetchone()[0]
    led.artifact_add(llm.KIND,json.dumps({'patient_context':[context(body)]}),project_id=1,message_id=2,
        model='synthetic',meta={inference.VERSION_KEY:inference.CURRENT_VERSION,'hash':refhash,
        'extract_version':llm.EXTRACT_VERSION,'actual_model':'synthetic',
        'integrity':{'validation':{'items_dropped':0,'evidence_dropped':0}}})
    frame = patient_state_context.prepare(led,target,model='synthetic',contract=llm._context_contract(),
        attempt={'human':None,'qc':None,'thin':None},enabled=True,base_context=None,version=llm.EXTRACT_VERSION)
    assert len(frame['refs'])==1 and frame['refs']['S1']['value']=='3錠'
    prepared = {**target,'_frame':frame,'_context':frame['text'],'_input_context':None,'_target':('synthetic','synthetic')}
    assert llm._replace_current(led,prepared,'{"summary":"合成の初回結果"}')
    led.save_messages([_message(mid=2,body='本人の合成薬Aの残薬は4錠。',posted_at='2026-09-01T10:00:00+09:00')])
    assert dependency_loss.source_binding(led.db,1,1)==target['_source_binding']
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
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
