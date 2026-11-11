"""Chunk records written under a retired contract generation classify as
superseded — never resumed, never silently unverifiable (fully synthetic)."""
import json
from unittest.mock import patch

import pytest

import anthropic_client
import cloud_policy
import extract_llm as worker
import extract_llm_context
import extract_llm_engine
import extract_llm_historical
import extraction_context as frames
import mcs_queries
from anthropic_runtime_testkit import _use_real_cloud_accounting
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY
from test_anthropic_runtime import old, routed

__all__ = ['routed']

ATTEMPT = {'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0}


def _frozen(ledger):
    old(ledger)
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row.update(_source_binding=source_binding(ledger.db, 1, 1),
               _target=('', 'synthetic-model'), _cloud_mode='off')
    frame = frames.prepare(ledger, row, model='synthetic-model',
        contract=worker._context_contract(), attempt=ATTEMPT, enabled=False,
        base_context=None, version=worker.EXTRACT_VERSION)
    return row, frame


def _persist(ledger, row, frame, context, model='synthetic-model'):
    return ledger.artifact_add('extract_llm_chunk', '{}',
        project_id=1, message_id=1, model=model,
        meta={VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
              'ver': worker.EXTRACT_VERSION, 'chunk': 0, 'context': context,
              'chunk_size': worker._CHUNK_SIZE, 'split_size': worker._CHUNK_SIZE // 3,
              'piece_sha256': 'a' * 64, 'actual_model': model,
              'plan_version': worker.PLAN_VERSION})


def _context_for(row, frame, model, context):
    bound = {**row, '_target': ('', model), '_frame': frame}
    if context == 'current':
        return worker._chunk_context(bound, frame['text'], checkpoint=True)
    _gen_id, prompt_head, schema = extract_llm_historical.known_old_contracts()[context]
    return worker._chunk_context(bound, frame['text'], checkpoint=True,
                                 prompt_head=prompt_head, schema=schema)


def _current_context(row, frame, model='synthetic-model'):
    return _context_for(row, frame, model, 'current')


def _old_context(row, frame, index=0, model='synthetic-model'):
    return _context_for(row, frame, model, index)


def test_retired_generation_checkpoint_is_superseded(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    with pytest.raises(ValueError, match='pending_context_superseded'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_second_retired_generation_checkpoint_is_superseded(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame, index=1))
    with pytest.raises(ValueError, match='pending_context_superseded'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_unknown_checkpoint_stays_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, 'f' * 64)
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_partially_recognized_records_stay_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    _persist(ledger, row, frame, 'f' * 64)
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def test_current_checkpoint_still_resumes(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _current_context(row, frame))
    picked = worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)
    assert picked is not None and picked['artifact_id'] == frame['artifact_id']


def test_mixed_current_and_retired_records_stay_unverifiable(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _current_context(row, frame))
    _persist(ledger, row, frame, _old_context(row, frame))
    with pytest.raises(ValueError, match='pending_context_unverifiable'):
        worker._pending_context_frame(ledger.db, row, attempt=ATTEMPT, enabled=False)


def _scan_row(ledger, row):
    return {**row, '_source_binding': source_binding(ledger.db, row['project_id'], row['message_id'])}


def test_scan_flags_fully_retired_pending_set(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    assert extract_llm_context._pending_superseded_scan(ledger.db, _scan_row(ledger, row)) is True


def test_scan_sees_retired_records_under_a_different_model(routed):
    ledger, *_ = routed
    row, _frame = _frozen(ledger)
    cloud_row = dict(row)
    cloud_row['_target'] = ('', 'retired-cloud-model')
    cloud_frame = frames.prepare(ledger, cloud_row, model='retired-cloud-model',
        contract=worker._context_contract(), attempt=ATTEMPT, enabled=False,
        base_context=None, version=worker.EXTRACT_VERSION)
    _persist(ledger, row, cloud_frame, _old_context(row, cloud_frame, model='retired-cloud-model'),
             model='retired-cloud-model')
    scan_row = _scan_row(ledger, row)
    scan_row['_target'] = ('', 'synthetic-model')
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is True


def test_scan_rejects_current_or_mixed_or_unknown(routed):
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    scan_row = _scan_row(ledger, row)
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is False
    _persist(ledger, row, frame, _current_context(row, frame))
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is False
    _persist(ledger, row, frame, _old_context(row, frame))
    assert extract_llm_context._pending_superseded_scan(ledger.db, scan_row) is False


def _prepare(ledger, rows):
    reasons = {}
    jobs, skipped, blocked = worker._prepare_jobs(
        ledger, rows, ('', 'synthetic-model'), False, load_checkpoints=False,
        blocked_reasons=reasons)
    return jobs, skipped, blocked, reasons


def _pending_rows(ledger):
    rows, _fresh = worker._select_pending(ledger, 1, None, None, False)
    return rows


def test_prepare_migrates_fully_retired_pending_set(routed):
    """Clean paid history -> one current-contract local job, cloud state absent."""
    ledger, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    jobs, _skipped, blocked, reasons = _prepare(ledger, _pending_rows(ledger))
    assert blocked == 0 and reasons == {}
    assert len(jobs) == 1
    r, ctx, _saved, _hints, _qc = jobs[0]
    assert r["_superseded_local"] is True and r["_cloud_mode"] == "off"
    assert "_cloud_selection" not in r and "_provider_contract" not in r
    # a fresh current-contract frame is persisted once; a second prepare
    # reuses the same immutable artifact instead of duplicating it
    assert r["_frame"]["artifact_id"] != frame["artifact_id"]
    assert ctx == r["_frame"]["text"]
    jobs2, _s2, _b2, _r2 = _prepare(ledger, _pending_rows(ledger))
    assert jobs2[0][0]["_frame"]["artifact_id"] == r["_frame"]["artifact_id"]


def _real_reserve(ledger, cfg, monkeypatch):
    _use_real_cloud_accounting(monkeypatch)
    cfg["cloud_llm"]["provider"] = "anthropic"
    selection, errors = cloud_policy.resolve_profile(
        cfg, route="reextract", purpose="backlog", root=worker.HOME)
    assert not errors and selection is not None
    reserve = cloud_policy.usage_reserver(
        ledger, cfg, route="reextract", project_id=1, message_id=1,
        unit_key="1", owner={}, selection=selection)
    # reextract sends are retired in the live policy; only the historical
    # reservation seed runs under the reopened route, like legacy_cloud_routes.
    with patch.object(cloud_policy, "send_route", lambda route: True):
        return reserve(anthropic_client.AnthropicClient("synthetic-unused").body(
            "完全合成の予約確認。", 256, None), 30)


def test_owned_paid_history_keeps_the_cohort_held(routed, monkeypatch):
    """A bound reservation is paid work: the guard fails closed, no local job."""
    ledger, cfg, *_ = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    _real_reserve(ledger, cfg, monkeypatch)
    assert cloud_policy.unbound_unit_history(
        ledger, route="reextract", project_id=1,
        message_id=1, unit_key="1") is False
    jobs, _skipped, blocked, reasons = _prepare(ledger, _pending_rows(ledger))
    assert jobs == [] and blocked == 1
    assert reasons == {'prepare_context_load_superseded': 1}


def _artifact_rows(ledger, kinds):
    marks = ",".join("?" * len(kinds))
    return {row["artifact_id"]: tuple(row) for row in ledger.db.execute(
        f"SELECT * FROM artifacts WHERE kind IN ({marks})", kinds)}


def test_retired_cohort_runs_local_and_is_adopted(routed, monkeypatch):
    """E2E: superseded cohort -> local-only execution -> readable current artifact."""
    ledger, _cfg_, calls, reserved, results = routed
    row, frame = _frozen(ledger)
    _persist(ledger, row, frame, _old_context(row, frame))
    before = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context"))
    prompts = []

    def fake_call(prompt, **_kw):
        prompts.append(prompt)
        return {"symptoms": [], "summary": "合成の要約"}

    monkeypatch.setattr(extract_llm_engine, "_llm_call", fake_call)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report["done"] == 1 and prompts
    # local-only: no cloud unit construction, no reservation/result accounting,
    # no provider HTTP — the routed cloud client saw zero calls.
    assert calls == [] and reserved == [] and results == []
    assert not ledger.artifacts("cloud_usage_reserve")
    assert not ledger.artifacts("cloud_usage_result")
    # the executed prompt is the current-contract clinical prompt
    assert worker._PROMPT_HEAD in prompts[0]
    # independent reader: exactly one current extract artifact, adopted content
    current = ledger.db.execute(
        "SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id "
        "WHERE a.kind=? " + mcs_queries.current_extract_pred()
        + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()
    assert len(current) == 1
    assert json.loads(current[0]["content"])["summary"] == "合成の要約"
    # every pre-existing checkpoint/frame row is byte-for-byte unchanged
    after = _artifact_rows(ledger, ("extract_llm_chunk", "extraction_context"))
    for artifact_id, snapshot in before.items():
        assert after[artifact_id] == snapshot
