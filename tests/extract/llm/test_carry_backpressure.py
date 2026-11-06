"""Retain one completed pass and resume saving before any new inference."""
import extract_llm_admission
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import gc
import json
import os
import weakref

import pytest

import cloud_inference
import extract_llm as worker
from extract_testkit import _ledger, _message
from test_lane_slot_and_lock import _carry_one, _claims
from test_anthropic_runtime import (
    routed as routed, old, _use_real_cloud_accounting, _current_artifacts,
)
from test_cloud_response_recovery import legacy_cloud_routes


def forbid_new_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('a retained pass must only save or renew existing results')
    for name in ('_select_pending', '_prepare_jobs', '_claim', 'llm_extract',
                 '_gc_stale_artifacts', '_backlog_report'):
        monkeypatch.setattr(worker, name, forbidden)


def test_repeated_local_save_wait_never_accumulates_new_results(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        lock, carry = _carry_one(led, monkeypatch, tmp_path)
        unit = weakref.ref(carry[0][2])
        led.save_messages([_message(mid=2, body='完全合成の後続記録。')])
        forbid_new_work(monkeypatch)
        for _ in range(3):
            result = worker.run_pending(led, limit=8, budget_s=30,
                per_write_lock=True, write_wait_s=1, carry=carry)
            assert result['carry'] is carry and len(carry) == 1
            assert result['carry_waiting'] is True and result['selected'] == 0
            assert result['done'] == result['failed'] == result['deferred'] == 0
            assert result['left'] is result['left_estimate'] is result['llm_calls'] is None
            assert result['queue_ages_s'] == {'oldest': None, 'newest': None}
            assert _claims(led) == [carry[0][1]] and carry[0][0]['message_id'] == 1
            assert not led.artifacts('extract_llm') and not led.db.in_transaction
        lock['blocked'] = 0
        result = worker.run_pending(led, limit=8, budget_s=30,
            per_write_lock=True, write_wait_s=1, carry=carry)
        assert result['done'] == result['carried'] == 1
        assert result['carry_waiting'] is False and result['carry'] == []
        assert result['selected'] == 0 and result['left'] is None
        assert len(led.artifacts('extract_llm', message_id=1)) == 1
        assert led.artifacts('extract_llm', message_id=2) == [] and _claims(led) == []
        gc.collect()
        assert unit() is None
    finally:
        led.close()


def test_zero_budget_carry_is_untouched_and_zero_limit_still_rejected(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        _lock, carry = _carry_one(led, monkeypatch, tmp_path)
        original = list(carry)
        forbid_new_work(monkeypatch)
        monkeypatch.setattr(worker, '_settle_carry',
            lambda *a, **k: pytest.fail('zero budget cannot begin settlement'))
        monkeypatch.setattr(extract_llm_resident, '_settle_carry', worker._settle_carry)
        monkeypatch.setattr(extract_llm_runner, '_settle_carry', worker._settle_carry)
        before = led.db.total_changes
        result = worker.run_pending(led, limit=1, budget_s=0, carry=carry)
        assert result['carry'] is carry and carry == original and result['carry_waiting']
        with pytest.raises(ValueError, match='^extract_limit_invalid$'):
            worker.run_pending(led, limit=0, budget_s=30, carry=carry)
        assert led.db.total_changes == before and carry == original
    finally:
        led.close()


def test_paid_save_wait_keeps_receipt_and_never_posts_again(routed, monkeypatch, tmp_path):
    led, cfg, _, _, _ = routed
    _use_real_cloud_accounting(monkeypatch)
    monkeypatch.setattr(worker, 'HOME', str(tmp_path))
    monkeypatch.setattr(extract_llm_engine, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', worker.HOME)
    cfg['cloud_llm']['daily_request_budget'] = 5
    old(led)
    posts, blocked = [], [False]
    def request(self, method, body, timeout, **kwargs):
        posts.append(method)
        return 200, {}, json.dumps({'model': worker.anthropic_client.MODEL,
            'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"symptoms":[]}'}],
            'usage': {'input_tokens': 10, 'output_tokens': 5}}).encode()
    monkeypatch.setattr(worker.anthropic_client.AnthropicClient, 'request', request)
    monkeypatch.setattr(worker, 'acquire_run_lock', lambda: None if blocked[0] else
                        os.open(tmp_path / 'writer', os.O_CREAT | os.O_RDWR, 0o600))
    monkeypatch.setattr(extract_llm_errors, 'acquire_run_lock', worker.acquire_run_lock)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', worker.acquire_run_lock)
    original = worker.llm_extract
    def extract(*args, **kwargs):
        result = original(*args, **kwargs)
        blocked[0] = True
        return result
    monkeypatch.setattr(worker, 'llm_extract', extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: None)
    with legacy_cloud_routes():
        first = worker.run_pending(led, limit=1, budget_s=180, per_write_lock=True,
                                   write_wait_s=1, carry=[])
    carry = first['carry']
    assert len(carry) == len(posts) == 1
    receipt = [(row['artifact_id'], row['meta']) for row in led.artifacts('cloud_usage_result')]
    assert len(receipt) == len(led.artifacts('cloud_usage_reserve')) == 1
    old(led, mid=2)
    forbid_new_work(monkeypatch)
    for _ in range(3):
        resumed = worker.run_pending(led, limit=8, budget_s=30, per_write_lock=True,
                                     write_wait_s=1, carry=carry)
        assert resumed['carry_waiting'] and resumed['selected'] == 0
        assert len(carry) == len(posts) == len(led.artifacts('cloud_usage_reserve')) == 1
        assert [(row['artifact_id'], row['meta']) for row in led.artifacts('cloud_usage_result')] == receipt
        assert not led.artifacts('cloud_usage_disposition') and not led.db.in_transaction
    blocked[0] = False
    resumed = worker.run_pending(led, limit=8, budget_s=30, per_write_lock=True,
                                 write_wait_s=1, carry=carry)
    assert resumed['done'] == resumed['carried'] == 1 and not carry
    assert len(_current_artifacts(led)) == len(posts) == 1 and _claims(led) == []
    assert [(row['artifact_id'], row['meta']) for row in led.artifacts('cloud_usage_result')] == receipt
    assert not led.artifacts('cloud_usage_disposition')


@pytest.mark.parametrize('lane', [2, 3])
def test_resident_waits_for_save_before_context_or_semantic(tmp_path, monkeypatch, capsys, lane):
    clock, active, calls, context_calls = [0.0], [False], [], []
    pending = [({'project_id': 1, 'body_text': 'SYNTH-NEVER-LOG-CARRY'}, 100.0, lambda: None)]
    closed = []
    class Ledger:
        def close(self):
            closed.append(True)
    class Pool:
        def __init__(self, root):
            pass
        def sync(self, *args, **kwargs):
            assert not active[0]
        def close(self):
            pass
    monkeypatch.setattr(worker, 'HOME', str(tmp_path))
    monkeypatch.setattr(extract_llm_engine, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', worker.HOME)
    monkeypatch.setattr(worker, '_open_current_ledger', lambda **_: Ledger())
    monkeypatch.setattr(extract_llm_resident, '_open_current_ledger', worker._open_current_ledger)
    monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', worker._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', worker._writer_current)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker, 'load_config', lambda: {})
    monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda cfg: {'mode': 'off', 'max_concurrency': 1})
    monkeypatch.setattr(worker.cloud_backlog, 'CloudBacklogPool', Pool)
    monkeypatch.setattr(worker, 'legacy_admissions', lambda *args: None)
    monkeypatch.setattr(extract_llm_admission, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(extract_llm_resident, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(worker.sys, 'argv', ['extract_llm', '--all', '--lane', str(lane),
                                          '--semantic', '--stop-after', '10'])
    def run(*args, **kwargs):
        calls.append(bool(kwargs['carry']))
        if kwargs['carry']:
            return {'done': 0, 'failed': 0, 'left': None, 'selected': 0,
                    'carry': pending, 'carry_waiting': True}
        active[0] = True
        clock[0] += 1
        return {'done': 0, 'failed': 0, 'left': 1, 'selected': 1, 'carry': pending}
    def settle(ledger, carry, **kwargs):
        if kwargs.get('final', True):
            carry.clear()
        return {'done': 0, 'held_prior': 0, 'deferred': 0}, 0, set()
    def context(*args):
        assert not active[0]
        context_calls.append(True)
        return {'status': 'off', 'done': 0}
    monkeypatch.setattr(worker, 'run_pending', run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
    monkeypatch.setattr(worker, '_settle_carry', settle)
    monkeypatch.setattr(extract_llm_resident, '_settle_carry', worker._settle_carry)
    monkeypatch.setattr(extract_llm_runner, '_settle_carry', worker._settle_carry)
    monkeypatch.setattr(worker, '_background_context_refresh', context)
    monkeypatch.setattr(extract_llm_resident, '_background_context_refresh', worker._background_context_refresh)
    monkeypatch.setattr(worker, '_background_semantic',
                        lambda *args: pytest.fail('save wait must not admit semantic inference'))
    monkeypatch.setattr(extract_llm_resident, '_background_semantic', worker._background_semantic)
    assert worker.main() == 0
    assert calls[0] is False and all(calls[1:]) and closed == [True]
    assert len(context_calls) == int(lane == 3)
    assert clock[0] == 10 and not pending
    assert 'SYNTH-NEVER-LOG-CARRY' not in capsys.readouterr().out
