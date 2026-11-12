"""Resident pass records and the bounded local lookahead behind a save wait."""
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
import json
import os
import time

import pytest

import cloud_inference
import extract_llm as worker
from extract_testkit import _ledger, _message
from test_lane_slot_and_lock import _claims


class _StubLedger:
    def close(self):
        pass


class _Pool:
    def __init__(self, root, *, context_only):
        assert context_only is True

    def sync(self, *args, **kwargs):
        pass

    def close(self):
        pass


def _resident_env(monkeypatch, tmp_path, ledger, argv, *, stub_fence=True, on_sleep=None):
    """Patch the resident loop's collaborators; returns the fake monotonic clock."""
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds
        if on_sleep is not None:
            on_sleep(clock[0])
    for module in (worker, extract_llm_engine, extract_llm_prepare, extract_llm_provider,
                   extract_llm_resident, extract_llm_selection, extract_llm_source_contract,
                   extract_llm_store):
        monkeypatch.setattr(module, 'HOME', str(tmp_path))
    monkeypatch.setattr(worker, '_open_current_ledger', lambda **_: ledger)
    monkeypatch.setattr(extract_llm_resident, '_open_current_ledger', worker._open_current_ledger)
    if stub_fence:
        monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
        for module in (extract_llm_carry, extract_llm_errors, extract_llm_resident, extract_llm_runner,
                       extract_llm_selection, extract_llm_source_contract):
            monkeypatch.setattr(module, '_writer_current', worker._writer_current)
        monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker, 'load_config', lambda: {})
    for module in (extract_llm_context, extract_llm_prepare, extract_llm_provider, extract_llm_qc,
                   extract_llm_resident, extract_llm_runner, extract_llm_selection, extract_llm_store):
        monkeypatch.setattr(module, 'load_config', worker.load_config)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings',
                        lambda cfg: {'mode': 'off', 'max_concurrency': 1})
    monkeypatch.setattr(worker.cloud_backlog, 'CloudBacklogPool', _Pool)
    monkeypatch.setattr(worker, 'legacy_admissions', lambda *args: None)
    monkeypatch.setattr(extract_llm_admission, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(extract_llm_resident, 'legacy_admissions', worker.legacy_admissions)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker.time, 'sleep', sleep)
    monkeypatch.setattr(worker.sys, 'argv', ['extract_llm', '--all', *argv])
    return clock


def _records(out):
    return [json.loads(line) for line in out.splitlines() if line.startswith('{')]


def test_pass_records_carry_lane_pid_elapsed_and_turn(tmp_path, monkeypatch, capsys):
    clock = _resident_env(monkeypatch, tmp_path, _StubLedger(),
                          ['--lane', '2', '--stop-after', '10'])

    def run(led, **kwargs):
        clock[0] += 4
        return {'done': 1, 'failed': 0, 'left': 1, 'selected': 1, 'carry': []}
    monkeypatch.setattr(worker, 'run_pending', run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
    assert worker.main() == 0
    passes = [row for row in _records(capsys.readouterr().out) if 'turn' in row]
    assert len(passes) == 2
    assert all(row['lane'] == 2 and row['pid'] == os.getpid() and row['turn'] == 'normal'
               and row['elapsed_s'] == 4.0 for row in passes)


BODY = '合成の本文です。' * 20


def _real_run_lock(monkeypatch, tmp_path):
    """Hold a real run lock on another fd; returns the release callback."""
    path = str(tmp_path / 'synthetic-run.lock')
    holder = [worker.writer_lock.acquire_run_lock(path)]
    assert holder[0] is not None

    def acquire():
        return worker.writer_lock.acquire_run_lock(path)
    for module in (worker, extract_llm_errors, extract_llm_resident):
        monkeypatch.setattr(module, 'acquire_run_lock', acquire)

    def release():
        if holder[0] is not None:
            os.close(holder[0])
            holder[0] = None
    return release


def _local_llm(monkeypatch, clock):
    calls = []

    def extract(body, **_kw):
        calls.append(clock[0])
        return {'summary': '合成', 'vitals': {'bt': 37.0}, 'symptoms': [{'text': '咳'}]}
    monkeypatch.setattr(worker, 'llm_extract', extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    monkeypatch.setattr(worker, '_background_semantic', lambda *a, **k: {'done': 0, 'errors': []})
    monkeypatch.setattr(extract_llm_resident, '_background_semantic', worker._background_semantic)
    return calls


def _real_loop(tmp_path, monkeypatch, messages, release_at):
    led = _ledger(tmp_path)
    led.save_messages([_message(mid=mid, body=BODY) for mid in messages])
    close = led.close
    monkeypatch.setattr(led, 'close', lambda: None)
    release = _real_run_lock(monkeypatch, tmp_path)

    def on_sleep(now):
        if release_at is not None and now >= release_at:
            release()
    clock = _resident_env(monkeypatch, tmp_path, led,
                          ['--lane', '2', '--semantic', '--stop-after', '60'],
                          stub_fence=False, on_sleep=on_sleep)
    return led, close, release, clock


def test_lookahead_infers_a_second_result_while_the_first_waits(tmp_path, monkeypatch, capsys):
    led, close, release, clock = _real_loop(tmp_path, monkeypatch, (1, 2), release_at=40)
    try:
        calls = _local_llm(monkeypatch, clock)
        assert worker.main() == 0
        assert len(calls) == 2 and all(at < 40 for at in calls)
        rows = _records(capsys.readouterr().out)
        assert [row['turn'] for row in rows if 'turn' in row][:2] == ['normal', 'carry']
        assert any(row.get('turn') == 'lookahead' and row['lock_lost'] == 1 for row in rows)
        for mid in (1, 2):
            assert len(led.artifacts('extract_llm', message_id=mid)) == 1
        assert _claims(led) == []
        assert BODY not in json.dumps(rows, ensure_ascii=False)
    finally:
        release()
        close()


def test_two_carried_results_admit_no_third_inference(tmp_path, monkeypatch):
    led, close, release, clock = _real_loop(tmp_path, monkeypatch, (1, 2, 3), release_at=None)
    try:
        calls = _local_llm(monkeypatch, clock)
        assert worker.main() == 0
        assert len(calls) == 2
        assert led.artifacts('extract_llm') == [] and _claims(led) == []
    finally:
        release()
        close()


def _scripted(monkeypatch, clock, first_carry, lookahead=None):
    """run_pending stub: the first pass carries ``first_carry``; resumes keep waiting."""
    calls = []

    def run(led, **kwargs):
        calls.append({'carry': list(kwargs['carry']), 'limit': kwargs['limit'],
                      'allow_cloud': kwargs.get('allow_cloud', True)})
        clock[0] += 1
        if kwargs['carry']:
            return {'done': 0, 'failed': 0, 'left': None, 'selected': 0,
                    'carry': kwargs['carry'], 'carry_waiting': True}
        if len(calls) == 1:
            return {'done': 1, 'failed': 0, 'left': 1, 'selected': 1, 'carry': list(first_carry)}
        return dict(lookahead or {'done': 1, 'failed': 0, 'left': 1, 'selected': 1, 'carry': []})
    monkeypatch.setattr(worker, 'run_pending', run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
    settles = []

    def settle(led, carry, **kwargs):
        settles.append((list(carry), kwargs))
        if kwargs.get('final', True):
            carry.clear()
        return {'done': 0, 'held_prior': 0, 'deferred': 0}, 0, set()
    monkeypatch.setattr(worker, '_settle_carry', settle)
    monkeypatch.setattr(extract_llm_resident, '_settle_carry', worker._settle_carry)
    monkeypatch.setattr(extract_llm_runner, '_settle_carry', worker._settle_carry)
    return calls, settles


def _fresh_passes(calls):
    return [call for call in calls[1:] if not call['carry']]


@pytest.mark.parametrize('row,lease_s,expected', [
    ({'project_id': 1}, 1230, True),
    ({'project_id': 1, '_cloud_owned': True}, 1230, False),
    ({'project_id': 1, '_cloud_guard': lambda: None}, 1230, False),
    ({'project_id': 1, '_cloud_discard': lambda: None}, 1230, False),
    ({'project_id': 1}, 480, False),
])
def test_lookahead_only_for_local_results_with_lease_to_spare(tmp_path, monkeypatch, row, lease_s, expected):
    clock = _resident_env(monkeypatch, tmp_path, _StubLedger(), ['--lane', '2', '--stop-after', '20'])
    carried = [(row, time.time() + lease_s, lambda **kw: None)]
    calls, _settles = _scripted(monkeypatch, clock, carried)
    assert worker.main() == 0
    fresh = _fresh_passes(calls)
    assert bool(fresh) is expected
    assert all(call['limit'] == 1 and call['allow_cloud'] is False for call in fresh)


def test_updated_during_lookahead_releases_parked_results_unwritten(tmp_path, monkeypatch):
    clock = _resident_env(monkeypatch, tmp_path, _StubLedger(), ['--lane', '2', '--stop-after', '20'])
    first = ({'project_id': 1}, time.time() + 1230, lambda **kw: None)
    second = ({'project_id': 1}, time.time() + 1230, lambda **kw: None)
    calls, settles = _scripted(monkeypatch, clock, [first],
                               lookahead={'done': 0, 'failed': 0, 'left': None,
                                          'updated': True, 'carry': [second]})
    assert worker.main() == 0
    assert len(_fresh_passes(calls)) == 1
    assert settles == [([first, second], {'write': False})]


def test_lane_three_lookahead_skips_patient_context(tmp_path, monkeypatch, capsys):
    clock = _resident_env(monkeypatch, tmp_path, _StubLedger(), ['--lane', '3', '--stop-after', '20'])
    secret = {'project_id': 1, 'body_text': 'SYNTH-NEVER-LOG-LOOKAHEAD'}
    calls, settles = _scripted(monkeypatch, clock, [(secret, time.time() + 1230, lambda **kw: None)])
    context_calls = []

    def context(*args):
        context_calls.append(True)
        return {'status': 'off', 'done': 0}
    monkeypatch.setattr(worker, '_background_context_refresh', context)
    monkeypatch.setattr(extract_llm_resident, '_background_context_refresh', worker._background_context_refresh)
    assert worker.main() == 0
    fresh = _fresh_passes(calls)
    assert fresh and all(call['limit'] == 1 and call['allow_cloud'] is False for call in fresh)
    assert len(context_calls) == 1
    assert 'SYNTH-NEVER-LOG-LOOKAHEAD' not in capsys.readouterr().out
