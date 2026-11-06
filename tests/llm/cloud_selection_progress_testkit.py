"""Synthetic cloud selection progress testkit contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_context
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_store
import semantic_due_selection
import semantic_job_processor
from contextlib import contextmanager
import time
import pytest
import cloud_policy
import extract_llm
import cloud_inference
import inference_dependency_loss
import semantic_drain
from semantic_testkit import _cfg, _ledger, _message, _patient
from test_anthropic_runtime import _assert_legacy_retained as _assert_legacy_retained, _current_artifacts as _current_artifacts, old as old, routed as routed


__all__ = ["routed"]


_REAL_RESERVER = cloud_policy.usage_reserver


_REAL_RESULT = cloud_policy.record_result


_REAL_BLOCK = cloud_policy.block_reason


@pytest.fixture
def blocked_prefix(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    cfg = _cfg("assist", fact_source="shadow", summary_mode="shadow", loop_mode="shadow")
    cfg["cloud_llm"] = {"mode": "on", "project_ids": None}
    monkeypatch.setattr(extract_llm, "load_config", lambda *a: cfg)
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda *a: cfg)
    monkeypatch.setattr(semantic_drain, "load_config", lambda *a: cfg)
    monkeypatch.setattr(semantic_due_selection, 'load_config', semantic_drain.load_config)
    monkeypatch.setattr(semantic_job_processor, 'load_config', semantic_drain.load_config)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *a: "synthetic-key")
    for pid in range(1, 514):
        _patient(led, pid)
        led.save_messages([_message(pid, pid=pid, body="完全合成の過去記録。")])
        led.semantic_seed(pid, [pid], {"source": "history_import"})
    with led.db:
        led.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=513")
        led.db.execute("UPDATE messages SET first_seen=?,posted_at_ts=message_id", (time.time() - 1200,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, "synthetic-selection-scope", time.time())
    # Roots still occur in the receipt, but their new reply does not. A root-only
    # membership test cannot authorize the expanded thread as a cloud prompt.
    for pid in range(1, 513):
        led.save_messages([_message(1000 + pid, pid=pid, parent=pid,
                                   body="完全合成の承認範囲外の新しい返信。")])
    yield led, cfg
    led.close()


def _select(led, lane, *, deadline=None):
    # The reextract/semantic lanes are dormant send routes; selection
    # mechanics are still exercised under the legacy-open gate only.
    from test_cloud_response_recovery import legacy_cloud_routes
    with legacy_cloud_routes():
        if lane == "semantic":
            return semantic_drain._select_cloud_due(led, "backlog", ("semantic",), False, 1,
                deadline=deadline, isolated=deadline is not None)[0]
        return extract_llm._select_pending(led, 64, None, None, False,
            cloud_only=True, prioritize_patients=True, deadline=deadline)[0]


@pytest.fixture
def cloud_candidates(routed, monkeypatch):
    led, cfg, *_ = routed
    cfg["cloud_llm"]["max_concurrency"] = 32
    for mid in range(1, 65):
        old(led, mid)
    with led.db:
        led.db.execute("UPDATE messages SET first_seen=?", (time.time() - 1200,))
        led.db.execute("UPDATE message_revisions SET observed_at=?", (time.time() - 1200,))
        cloud_policy.capture_history_tx(led.db, "synthetic-claim-refill", time.time())
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: pytest.fail("cloud probed local backend"))
    monkeypatch.setattr(extract_llm_provider, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm_runner, '_probe_format', extract_llm._probe_format)
    monkeypatch.setattr(extract_llm, "_run_batch_call", lambda *a: pytest.fail("cloud used local batch"))
    monkeypatch.setattr(extract_llm_runner, '_run_batch_call', extract_llm._run_batch_call)
    return routed


def _cloud_pass(led, *, limit=1):
    from test_cloud_response_recovery import legacy_cloud_routes
    with legacy_cloud_routes():
        return extract_llm.run_pending(led, limit=limit, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, batch_k=4)


def _claim_count(led):
    return led.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]


def _complete_cloud_tx(ledger, source):
    """Simulate another worker's source-bound, completed cloud artifact."""
    assert ledger.db.in_transaction
    selection, errors = cloud_policy.resolve_profile(
        extract_llm.load_config(), route='reextract', purpose='backlog')
    assert not errors and selection is not None
    contract = cloud_inference.cloud_provider_contract(selection=selection)
    row = dict(source)
    row.update(_source_binding=inference_dependency_loss.source_binding(
        ledger.db, row['project_id'], row['message_id']),
        _target=('', contract['model']), _provider_contract=contract,
        _provider_evidence=extract_llm._provider_evidence([contract['backend']], True))
    integrity = {'completeness_chunks': 1, 'completeness': {
        '0': {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}}
    assert extract_llm._replace_current_tx(ledger, row, '{"symptoms":[]}', integrity=integrity) == 'written'
    assert extract_llm.cloud_completion(ledger.db, row, contract) == 'complete'


def _complete_local_tx(ledger, source):
    """Simulate a separate worker finishing the current source locally."""
    assert ledger.db.in_transaction
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?',
                                 (source['project_id'], source['message_id'])).fetchone())
    target = extract_llm._resolved_llm()
    row.update(_source_binding=inference_dependency_loss.source_binding(
        ledger.db, row['project_id'], row['message_id']), _target=target,
        _provider_evidence=extract_llm._provider_evidence(['local'], True))
    integrity = {'completeness_chunks': 1, 'completeness': {
        '0': {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}}
    assert extract_llm._replace_current_tx(ledger, row, '{"symptoms":[]}', integrity=integrity) == 'written'
    assert extract_llm.progress_for_message(ledger.db, row, model=target[1])['state'] == 'complete'
    selection, errors = cloud_policy.resolve_profile(
        extract_llm.load_config(), route='reextract', purpose='backlog')
    assert not errors and selection is not None
    contract = cloud_inference.cloud_provider_contract(selection=selection)
    assert extract_llm.cloud_completion(ledger.db, row, contract) != 'complete'


def _make_paid_carry(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    lock = {'held': False}
    @contextmanager
    def write_lock(enabled, wait_s=60, **kwargs):
        yield lock['held'] or not enabled
    monkeypatch.setattr(extract_llm, '_write_lock', write_lock)
    monkeypatch.setattr(extract_llm_carry, '_write_lock', extract_llm._write_lock)
    monkeypatch.setattr(extract_llm_resident, '_write_lock', extract_llm._write_lock)
    monkeypatch.setattr(extract_llm_runner, '_write_lock', extract_llm._write_lock)
    from test_cloud_response_recovery import legacy_cloud_routes
    with legacy_cloud_routes():
        first = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert first['done'] == 0 and len(first['carry']) == 1
    assert len(calls) == len(reservations) == len(results) == 1
    return led, first['carry'], lock, calls, reservations, results
