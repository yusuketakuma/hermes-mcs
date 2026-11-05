"""Synthetic cloud selection progress carry contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
from contextlib import contextmanager
import json
import sqlite3
import time
from types import SimpleNamespace
import pytest
import cloud_policy
import extract_llm
import cloud_inference
from anthropic_client import AnthropicClient
from test_anthropic_runtime import _assert_legacy_retained as _assert_legacy_retained, _current_artifacts as _current_artifacts, old as old, routed as routed
from cloud_selection_progress_testkit import _REAL_BLOCK as _REAL_BLOCK, _REAL_RESERVER as _REAL_RESERVER, _REAL_RESULT as _REAL_RESULT, _claim_count as _claim_count, _make_paid_carry as _make_paid_carry, blocked_prefix as blocked_prefix, cloud_candidates as cloud_candidates


def test_empty_carry_still_reports_incomplete_selection(cloud_candidates, monkeypatch):
    led, _, calls, reservations, results = cloud_candidates
    carry = []
    observed = []

    def incomplete(*args, **kwargs):
        observed.append(kwargs["deadline"])
        raise extract_llm._CloudSelectionIncomplete("deadline")

    monkeypatch.setattr(extract_llm, "_select_pending", incomplete)
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    monkeypatch.setattr(extract_llm, "_prepare_jobs",
                        lambda *a, **kw: pytest.fail("incomplete selection prepared work"))
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', extract_llm._prepare_jobs)
    report = extract_llm.run_pending(led, limit=1, budget_s=180, cloud_only=True,
                                    per_write_lock=True, carry=carry)
    assert len(observed) == 1 and report["carry"] is carry
    assert report["cloud_selection_incomplete"] is True and report["cloud_selection_reason"] == "deadline"
    assert report["selected"] is report["fresh_selected"] is report["left"] is report["left_estimate"] is None
    assert report["left_estimate_age_s"] is None and report["queue_ages_s"] == {"oldest": None, "newest": None}
    assert report["done"] == 0 and calls == reservations == results == []
    assert _claim_count(led) == 0


def test_incomplete_selection_renews_then_saves_carried_paid_result(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    wall = [time.time()]
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=time.monotonic, time=lambda: wall[0], perf_counter=time.perf_counter))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    def incomplete(*args, **kwargs):
        raise extract_llm._CloudSelectionIncomplete('deadline')
    monkeypatch.setattr(extract_llm, '_select_pending', incomplete)
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    for _ in range(2):
        wall[0] += 300
        result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
        assert 'cloud_selection_incomplete' not in result and result['done'] == 0
        assert result['selected'] == result['fresh_selected'] == 0
        assert result['left'] is result['left_estimate'] is None
        assert result['carry'] is carry and len(carry) == 1 and carry[0][1] > wall[0] + 120
        assert len(calls) == len(reservations) == len(results) == 1
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    assert 'cloud_selection_incomplete' not in result and result['done'] == result['carried'] == 1
    assert result['selected'] == result['fresh_selected'] == 0
    assert carry == [] and len(_current_artifacts(led)) == 1 and _claim_count(led) == 0
    assert len(calls) == len(reservations) == len(results) == 1


def test_short_incomplete_ticks_preserve_carry_but_expired_lease_is_never_committed(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    # Persist a real synthetic accounting pair for this request; the LLM
    # transport remains stubbed and no external endpoint is contacted.
    cfg = cloud_candidates[1]
    mid = carry[0][0]['message_id']
    body = AnthropicClient('synthetic').body(calls[0], extract_llm.MAX_TOKENS, None)
    reserve = _REAL_RESERVER(led, cfg, route='reextract', project_id=1, message_id=mid, unit_key=str(mid))
    receipt = reserve(body, 2)
    _REAL_RESULT(led, receipt, {'model': receipt['model'], 'request_fp': receipt['request_fp'],
        'usage': {'input_tokens': 10, 'output_tokens': 5}, 'http_class': 'http_200',
        'request_id': 'synthetic', 'error_kind': None})
    wall = [time.time()]
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=time.monotonic, time=lambda: wall[0], perf_counter=time.perf_counter))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    def incomplete(*args, **kwargs):
        raise extract_llm._CloudSelectionIncomplete('deadline')
    monkeypatch.setattr(extract_llm, '_select_pending', incomplete)
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    lease = carry[0][1]
    for now in (lease - 10, lease + 1):
        wall[0] = now
        result = extract_llm.run_pending(led, limit=1, budget_s=0.5, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
        assert 'cloud_selection_incomplete' not in result and result['carry'] is carry and len(carry) == 1
        assert result['selected'] == result['fresh_selected'] == 0
        assert result['left'] is result['left_estimate'] is None
        assert result['done'] == 0 and len(calls) == len(reservations) == len(results) == 1
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    assert 'cloud_selection_incomplete' not in result and result['done'] == 0
    assert result['selected'] == result['fresh_selected'] == 0
    assert result['deferred'] == 1 and carry == [] and not _current_artifacts(led)
    assert result['defer_reasons'] == {'currentness_unverified': 1}
    assert len(calls) == len(reservations) == len(results) == 1
    from anthropic_client import AnthropicError
    with pytest.raises(AnthropicError, match='budget_exceeded'):
        reserve(body, 2)
    assert len(led.artifacts('cloud_usage_reserve')) == len(led.artifacts('cloud_usage_result')) == 1


@pytest.mark.parametrize('budget', [0.5, 180])
def test_successful_selection_retains_unsettled_carry_until_the_next_pass(cloud_candidates, monkeypatch, budget):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    monkeypatch.setattr(extract_llm, '_select_pending', lambda *args, **kwargs: ([], 0))
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    before = carry[0][1]
    result = extract_llm.run_pending(led, limit=1, budget_s=budget, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    retained = result['carry']
    assert result['done'] == 0 and len(retained) == 1 and retained[0][1] >= before
    row = led.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    assert row is not None and row[0] == retained[0][1]
    assert len(calls) == len(reservations) == len(results) == 1
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=retained)
    assert result['done'] == result['carried'] == 1 and result['carry'] == []
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 0
    assert len(calls) == len(reservations) == len(results) == 1


def test_paid_carry_scope_revocation_is_discarded_and_counted(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    prior = [dict(row) for row in led.artifacts(extract_llm.KIND)]
    cloud_candidates[1]['cloud_llm']['exclude_project_ids'] = [1]
    monkeypatch.setattr(extract_llm, '_select_pending',
        lambda *a, **kw: (_ for _ in ()).throw(extract_llm._CloudSelectionIncomplete('deadline')))
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    lock['held'] = True
    result = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
    assert 'cloud_selection_incomplete' not in result
    assert result['selected'] == result['fresh_selected'] == 0
    assert result['done'] == 0 and result['carried'] == 1 and result['deferred'] == 1
    assert result['defer_reasons'] == {'cloud_guard': 1}
    assert carry == [] and _claim_count(led) == 0
    _assert_legacy_retained(led, prior)
    assert len(led.artifacts('cloud_usage_disposition')) == 1
    assert len(calls) == len(reservations) == len(results) == 1


def test_carry_deadline_keeps_unprocessed_paid_result_and_lease(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    second = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(second['carry']) == 1 and len(calls) == 2
    carry.extend(second['carry'])
    row, lease, commit = carry[0]
    expected_second = carry[1]
    clock = [100.0]
    def first_commit(**kwargs):
        result = commit(**kwargs)
        clock[0] = 102.0
        return result
    carry[0] = (row, lease, first_commit)
    lock['held'] = True
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time, perf_counter=time.perf_counter))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    settled, count, _ = extract_llm._settle_carry(led, carry, final=False,
        fence=extract_llm._WRITER_STATE, preserve_on_fence_loss=True, deadline=101.0,
        lock_context=extract_llm._write_lock(True), report=False)
    assert settled['done'] == count == 1 and carry == [expected_second]
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 1
    assert len(calls) == len(reservations) == len(results) == 2


def test_carry_partial_success_then_sqlite_error_retries_only_remaining(cloud_candidates, monkeypatch):
    led, carry, lock, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    second = extract_llm.run_pending(led, limit=1, budget_s=180, background=True,
        cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(second['carry']) == 1
    row, lease, commit = second['carry'][0]
    attempts = []
    def transient(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite3.OperationalError('synthetic locked')
        return commit(**kwargs)
    carry.extend([(row, lease, transient)])
    lock['held'] = True
    def settle():
        return extract_llm._settle_carry(led, carry, final=False,
            fence=extract_llm._WRITER_STATE, preserve_on_fence_loss=True,
            lock_context=extract_llm._write_lock(True), report=False)
    with pytest.raises(sqlite3.OperationalError, match='synthetic locked'):
        settle()
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 1
    assert len(carry) == 1 and carry[0][0] is row
    assert led.artifacts('cloud_usage_disposition') == []
    settled, count, _ = settle()
    assert settled['done'] == count == 1 and carry == []
    assert len(_current_artifacts(led)) == 2 and _claim_count(led) == 0
    assert led.artifacts('cloud_usage_disposition') == []
    assert len(calls) == len(reservations) == len(results) == 2


def test_retained_short_budget_skips_lock_wait_and_preserves_paid_result(
        cloud_candidates, tmp_path, monkeypatch):
    import os
    import writer_lock
    real_write_lock = extract_llm._write_lock
    led, carry, _, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    before = carry[0]
    path = str(tmp_path / 'synthetic-carry-run.lock')
    owner = writer_lock.acquire_run_lock(path)
    assert owner is not None
    clock = [100.0]
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(extract_llm, '_write_lock', real_write_lock)
    monkeypatch.setattr(extract_llm_carry, '_write_lock', extract_llm._write_lock)
    monkeypatch.setattr(extract_llm_resident, '_write_lock', extract_llm._write_lock)
    monkeypatch.setattr(extract_llm_runner, '_write_lock', extract_llm._write_lock)
    monkeypatch.setattr(extract_llm, 'acquire_run_lock', lambda: writer_lock.acquire_run_lock(path))
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm, '_select_pending', lambda *a, **kw:
                        pytest.fail('retained pass must not select new work'))
    monkeypatch.setattr(extract_llm_runner, '_select_pending', extract_llm._select_pending)
    monkeypatch.setattr(extract_llm, '_gc_stale_artifacts', lambda *a, **kw: None)
    monkeypatch.setattr(extract_llm_runner, '_gc_stale_artifacts', extract_llm._gc_stale_artifacts)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=sleep, time=time.time, perf_counter=time.perf_counter))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    try:
        report = extract_llm.run_pending(led, limit=1, budget_s=0.5, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=carry)
        assert clock[0] == 100.0
        assert report['selected'] == report['fresh_selected'] == 0
        assert report['left'] is report['left_estimate'] is None
        assert report['done'] == 0 and report['carry'] == [before]
        assert _claim_count(led) == 1 and not _current_artifacts(led)
        assert len(calls) == len(reservations) == len(results) == 1
    finally:
        os.close(owner)


def test_paid_checkpoint_replays_after_later_candidate_database_error(cloud_candidates, monkeypatch):
    led, cfg, _, _, _ = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    sent = []
    monkeypatch.setattr(cloud_policy, 'usage_reserver', _REAL_RESERVER)
    monkeypatch.setattr(cloud_policy, 'record_result', _REAL_RESULT)
    monkeypatch.setattr(cloud_policy, 'block_reason', _REAL_BLOCK)
    monkeypatch.setattr(extract_llm.anthropic_client, 'AnthropicClient', AnthropicClient)
    def response(self, method, body, timeout, **kwargs):
        sent.append(body)
        return 200, {}, json.dumps({'id': 'synthetic', 'model': extract_llm.anthropic_client.MODEL,
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"symptoms":[]}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode()
    monkeypatch.setattr(AnthropicClient, 'request', response)
    lock = {'held': False}
    @contextmanager
    def write_lock(enabled, wait_s=60, **kwargs):
        yield lock['held'] or not enabled
    monkeypatch.setattr(extract_llm, '_write_lock', write_lock)
    monkeypatch.setattr(extract_llm_carry, '_write_lock', extract_llm._write_lock)
    monkeypatch.setattr(extract_llm_resident, '_write_lock', extract_llm._write_lock)
    monkeypatch.setattr(extract_llm_runner, '_write_lock', extract_llm._write_lock)
    actual_extract = extract_llm.llm_extract
    invocations = []
    def fail_second(body, **kwargs):
        invocations.append(True)
        if len(invocations) == 2:
            led.db.execute('SELECT * FROM synthetic_missing_table')
        return actual_extract(body, **kwargs)
    monkeypatch.setattr(extract_llm, 'llm_extract', fail_second)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    with pytest.raises(sqlite3.OperationalError, match='synthetic_missing_table'):
        extract_llm.run_pending(led, limit=2, workers=1, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(sent) == 1 and _claim_count(led) == 0 and not _current_artifacts(led)
    assert len(led.artifacts('cloud_usage_reserve')) == len(led.artifacts('cloud_usage_result')) == 1
    assert led.artifacts('extract_llm_chunk')
    invocations.clear()
    lock['held'] = True
    with pytest.raises(sqlite3.OperationalError, match='synthetic_missing_table'):
        extract_llm.run_pending(led, limit=2, workers=1, budget_s=180, background=True,
            cloud_only=True, prioritize_patients=True, per_write_lock=True, carry=[])
    assert len(_current_artifacts(led)) == 1 and _claim_count(led) == 0
    assert len(sent) == 1
    assert len(led.artifacts('cloud_usage_reserve')) == len(led.artifacts('cloud_usage_result')) == 1


@pytest.mark.parametrize('updater_changed', [False, True])
@pytest.mark.parametrize('blocked_before_tail', [False, True])
def test_cloud_tail_lock_contention_respects_observed_update_fence(
        cloud_candidates, tmp_path, monkeypatch, updater_changed, blocked_before_tail):
    import os
    import writer_lock
    led, _, calls, reservations, results = cloud_candidates
    path = str(tmp_path / 'synthetic-update-run.lock')
    owner = writer_lock.acquire_run_lock(path)
    assert owner is not None
    state, attempts, clock = [(False, 1)], [], [100.0]
    prepare, prepared = extract_llm._prepare_jobs, []
    def prepare_one(*args, **kwargs):
        prepared.append(True)
        if blocked_before_tail and len(prepared) == 1:
            return [], 0, 1
        return prepare(*args, **kwargs)
    def acquire():
        attempts.append(True)
        if updater_changed and len(attempts) == 2:
            state[0] = (False, 2)
        return writer_lock.acquire_run_lock(path)
    def sleep(seconds):
        clock[0] += seconds
    original_renew, original_release = extract_llm._renew, extract_llm._release
    renewals, releases = [], []
    def renew(*args):
        renewals.append(True)
        return original_renew(*args)
    def release(*args):
        releases.append(True)
        return original_release(*args)
    monkeypatch.setattr(writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_prepare_jobs', prepare_one)
    monkeypatch.setattr(extract_llm_runner, '_prepare_jobs', extract_llm._prepare_jobs)
    monkeypatch.setattr(extract_llm, 'acquire_run_lock', acquire)
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', extract_llm.acquire_run_lock)
    monkeypatch.setattr(extract_llm, '_renew', renew)
    monkeypatch.setattr(extract_llm_carry, '_renew', extract_llm._renew)
    monkeypatch.setattr(extract_llm_runner, '_renew', extract_llm._renew)
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm_carry, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm_runner, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], sleep=sleep, time=time.time, perf_counter=time.perf_counter))
    monkeypatch.setattr(extract_llm_carry, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_context, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_engine, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_errors, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_prepare, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_provider, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_resident, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_runner, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_selection, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_source_contract, 'time', extract_llm.time)
    monkeypatch.setattr(extract_llm_store, 'time', extract_llm.time)
    monkeypatch.setattr(cloud_inference, 'time', extract_llm.time)
    try:
        report = extract_llm.run_pending(led, limit=1, budget_s=180, workers=1,
            background=True, cloud_only=True, prioritize_patients=True,
            per_write_lock=True, write_wait_s=1, carry=[])
        assert len(attempts) >= 2 and len(calls) == len(reservations) == len(results) == 1
        assert report['done'] == 0 and len(report['carry']) == 1
        assert report['deferred'] == int(blocked_before_tail)
        assert report['defer_reasons'] == ({'prepare_blocked': 1} if blocked_before_tail else {})
        row, lease, _ = report['carry'][0]
        current = led.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim' "
                                 "AND project_id=? AND message_id=?",
                                 (row['project_id'], row['message_id'])).fetchone()
        assert current is not None and current[0] == lease
        assert not _current_artifacts(led) and led.artifacts('cloud_usage_disposition') == []
        if updater_changed:
            assert report['updated'] is True and renewals == [] and releases == []
        else:
            assert not report.get('updated') and renewals == [True]
    finally:
        os.close(owner)


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_extract_exception_preserves_claim_only_after_observed_update(
        cloud_candidates, monkeypatch, updater_changed):
    led, cfg, calls, reservations, results = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    state, releases = [(False, 1)], []
    original_release = extract_llm._release
    def release(*args):
        releases.append(True)
        return original_release(*args)
    def fail(*args, **kwargs):
        if updater_changed:
            state[0] = (False, 2)
        raise sqlite3.OperationalError('synthetic extract exception')
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm_carry, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm_runner, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm, 'llm_extract', fail)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    with pytest.raises(sqlite3.OperationalError, match='synthetic extract exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == int(not updater_changed)
    assert not _current_artifacts(led) and calls == reservations == results == []


@pytest.mark.parametrize('updater_changed', [False, True])
def test_carried_settle_exception_keeps_original_claim_without_new_selection(
        cloud_candidates, monkeypatch, updater_changed):
    led, carry, _, calls, reservations, results = _make_paid_carry(cloud_candidates, monkeypatch)
    cloud_candidates[1]['cloud_llm']['max_concurrency'] = 1
    original_release = extract_llm._release
    state, passes, releases = [(False, 1)], [], []
    def settle(*args, **kwargs):
        passes.append(True)
        if updater_changed:
            state[0] = (False, 2)
        raise sqlite3.OperationalError('synthetic settle exception')
    def release(*args):
        releases.append(True)
        return original_release(*args)
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_settle_carry', settle)
    monkeypatch.setattr(extract_llm_resident, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(extract_llm_runner, '_settle_carry', extract_llm._settle_carry)
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm_carry, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm_runner, '_release', extract_llm._release)
    with pytest.raises(sqlite3.OperationalError, match='synthetic settle exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True,
            per_write_lock=True, carry=carry)
    assert len(passes) == 1 and len(carry) == 1
    assert _claim_count(led) == 1 and releases == []
    assert len(calls) == len(reservations) == len(results) == 1
    assert not _current_artifacts(led)


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_flush_exception_preserves_claim_only_after_observed_update(
        cloud_candidates, monkeypatch, updater_changed):
    led, cfg, calls, reservations, results = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    state, releases = [(False, 1)], []
    original_release = extract_llm._release
    def release(*args):
        releases.append(True)
        return original_release(*args)
    def fail_extract(*args, **kwargs):
        raise RuntimeError('synthetic before flush')
    class FailingQueue:
        def get_nowait(self):
            if updater_changed:
                state[0] = (False, 2)
            raise sqlite3.OperationalError('synthetic flush exception')
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm_carry, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm_runner, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm, 'llm_extract', fail_extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm.queue, 'SimpleQueue', FailingQueue)
    with pytest.raises(sqlite3.OperationalError, match='synthetic flush exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == int(not updater_changed)
    assert not _current_artifacts(led) and calls == reservations == results == []


@pytest.mark.parametrize('updater_changed', [False, True])
def test_cloud_handle_exception_preserves_claim_only_after_observed_update(
        cloud_candidates, monkeypatch, updater_changed):
    led, cfg, calls, reservations, results = cloud_candidates
    cfg['cloud_llm']['max_concurrency'] = 1
    state, releases, after_extract = [(False, 1)], [], []
    original_release = extract_llm._release
    original_owns = extract_llm._owns_current_source
    def release(*args):
        releases.append(True)
        return original_release(*args)
    def extract(*args, **kwargs):
        after_extract.append(True)
        return {}
    def owns(*args):
        if after_extract:
            after_extract.append(True)
            if len(after_extract) == 3:
                if updater_changed:
                    state[0] = (False, 2)
                raise sqlite3.OperationalError('synthetic handle exception')
        return original_owns(*args)
    monkeypatch.setattr(extract_llm.writer_lock, 'updater_state', lambda: state[0])
    monkeypatch.setattr(extract_llm, '_release', release)
    monkeypatch.setattr(extract_llm_carry, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm_runner, '_release', extract_llm._release)
    monkeypatch.setattr(extract_llm, 'llm_extract', extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, '_owns_current_source', owns)
    monkeypatch.setattr(extract_llm_carry, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_context, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_errors, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_runner, '_owns_current_source', extract_llm._owns_current_source)
    monkeypatch.setattr(extract_llm_store, '_owns_current_source', extract_llm._owns_current_source)
    with pytest.raises(sqlite3.OperationalError, match='synthetic handle exception'):
        extract_llm.run_pending(led, limit=1, workers=1, budget_s=180,
            background=True, cloud_only=True, prioritize_patients=True, carry=[])
    assert _claim_count(led) == int(updater_changed)
    assert len(releases) == (0 if updater_changed else 2)
    assert not _current_artifacts(led) and calls == reservations == results == []
