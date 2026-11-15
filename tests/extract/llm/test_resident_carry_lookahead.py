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
import cloud_policy
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


def _real_run_lock(monkeypatch, tmp_path, *, free_for_resident=False):
    """Hold a real run lock on another fd; returns the release callback.

    ``free_for_resident`` releases it when the resident loop itself (not a
    result write) first asks for the lock, i.e. at its semantic turn."""
    path = str(tmp_path / 'synthetic-run.lock')
    holder = [worker.writer_lock.acquire_run_lock(path)]
    assert holder[0] is not None

    def release():
        if holder[0] is not None:
            os.close(holder[0])
            holder[0] = None

    def acquire():
        return worker.writer_lock.acquire_run_lock(path)

    def resident_acquire():
        if free_for_resident:
            release()
        return acquire()
    monkeypatch.setattr(worker.writer_lock, 'RUN_LOCK', path)
    for module in (worker, extract_llm_errors):
        monkeypatch.setattr(module, 'acquire_run_lock', acquire)
    monkeypatch.setattr(extract_llm_resident, 'acquire_run_lock', resident_acquire)
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


def _real_loop(tmp_path, monkeypatch, messages, release_at, *, free_for_resident=False, stop_after=60):
    led = _ledger(tmp_path)
    led.save_messages([_message(mid=mid, body=BODY) for mid in messages])
    close = led.close
    monkeypatch.setattr(led, 'close', lambda: None)
    release = _real_run_lock(monkeypatch, tmp_path, free_for_resident=free_for_resident)

    def on_sleep(now):
        if release_at is not None and now >= release_at:
            release()
    clock = _resident_env(monkeypatch, tmp_path, led,
                          ['--lane', '2', '--semantic', '--stop-after', str(stop_after)],
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


def _semantic_jobs(monkeypatch, check=None):
    """Stub the drain behind the real _background_semantic; records each job's lock fd."""
    import semantic_drain
    jobs = []

    def run_due(ledger, cfg, result, deadline, **kwargs):
        jobs.append((kwargs['run_lock_fd'], check() if check else None))
        return {'done': 0}
    monkeypatch.setattr(semantic_drain, 'run_due', run_due)
    return jobs


def _semantic_records(out):
    return [row for row in _records(out) if 'semantic' in row]


def test_semantic_turn_saves_the_carry_first_then_runs_one_job(tmp_path, monkeypatch, capsys):
    led, close, release, clock = _real_loop(tmp_path, monkeypatch, (1,), release_at=None,
                                            free_for_resident=True, stop_after=20)
    try:
        calls = []
        monkeypatch.setattr(worker, 'llm_extract', lambda body, **kw: calls.append(clock[0]) or {
            'summary': '合成', 'vitals': {'bt': 37.0}, 'symptoms': [{'text': '咳'}]})
        monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
        jobs = _semantic_jobs(monkeypatch, lambda: len(led.artifacts('extract_llm', message_id=1)))
        assert worker.main() == 0
        rows = _records(capsys.readouterr().out)
        first_pass = next(row for row in rows if 'turn' in row)
        assert len(calls) == 1 and first_pass['lock_lost'] == 1
        assert jobs and jobs[0][0] is not None and jobs[0][1] == 1
        assert {'done': 1, 'held_prior': 0, 'deferred': 0} in [
            row['carry_settled'] for row in rows if 'carry_settled' in row]
        assert _claims(led) == []
    finally:
        release()
        close()


def test_semantic_turn_keeps_the_carry_when_the_lock_stays_busy(tmp_path, monkeypatch, capsys):
    led, close, release, clock = _real_loop(tmp_path, monkeypatch, (1,), release_at=None, stop_after=20)
    try:
        monkeypatch.setattr(worker, 'llm_extract', lambda body, **kw: {
            'summary': '合成', 'vitals': {'bt': 37.0}, 'symptoms': [{'text': '咳'}]})
        monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
        jobs = _semantic_jobs(monkeypatch)
        assert worker.main() == 0
        out = capsys.readouterr().out
        semantic = _semantic_records(out)
        assert semantic and all(row['semantic'] == {'skipped': 'lock_busy'} for row in semantic)
        # The fake clock sums 0.2 s polls, so float drift can add one poll to the 3 s window.
        assert semantic[0]['elapsed_s'] == pytest.approx(3.0, abs=0.2 + 1e-6)
        assert any(row.get('turn') == 'carry' and row.get('carry_waiting') for row in _records(out))
        assert jobs == [] and led.artifacts('extract_llm') == [] and _claims(led) == []
    finally:
        release()
        close()


def test_semantic_turn_waits_out_a_short_hold(tmp_path, monkeypatch, capsys):
    release = _real_run_lock(monkeypatch, tmp_path)

    def on_sleep(now):
        if now >= 3:
            release()
    clock = _resident_env(monkeypatch, tmp_path, _StubLedger(),
                          ['--lane', '2', '--semantic', '--stop-after', '10'], on_sleep=on_sleep)
    try:
        def run(led, **kwargs):
            clock[0] += 1
            return {'done': 1, 'failed': 0, 'left': 1, 'selected': 1, 'carry': []}
        monkeypatch.setattr(worker, 'run_pending', run)
        monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
        jobs = _semantic_jobs(monkeypatch)
        assert worker.main() == 0
        semantic = _semantic_records(capsys.readouterr().out)
        assert semantic[0]['semantic'].get('skipped') is None
        assert semantic[0]['elapsed_s'] == pytest.approx(2.0)
        assert jobs and jobs[0][0] is not None
    finally:
        release()


def test_semantic_turn_leaves_the_carry_when_the_update_fence_moved(tmp_path, monkeypatch, capsys):
    clock = _resident_env(monkeypatch, tmp_path, _StubLedger(),
                          ['--lane', '2', '--semantic', '--stop-after', '10'])
    release = _real_run_lock(monkeypatch, tmp_path, free_for_resident=True)
    state = [(False, 1)]
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: state[0])
    row = ({'project_id': 1}, time.time() + 480,
           lambda **kw: pytest.fail('old code must not write after an update'))
    calls = []

    def run(led, **kwargs):
        calls.append(list(kwargs['carry']))
        clock[0] += 1
        state[0] = (False, 2)
        carry = kwargs['carry'] or [row]
        return {'done': 0, 'failed': 0, 'left': 1, 'selected': 1, 'carry': carry}
    monkeypatch.setattr(worker, 'run_pending', run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', worker.run_pending)
    real_settle, kept = extract_llm_resident._settle_carry, []

    def settle(led, carry, **kwargs):
        if kwargs.get('lock_context') is not None:
            real_settle(led, carry, **kwargs)
            kept.append(list(carry))
        elif kwargs.get('final', True):
            carry.clear()
        return {'done': 0, 'held_prior': 0, 'deferred': 0}, 0, set()
    monkeypatch.setattr(extract_llm_resident, '_settle_carry', settle)
    jobs = _semantic_jobs(monkeypatch)
    try:
        assert worker.main() == 0
        semantic = _semantic_records(capsys.readouterr().out)
        assert semantic and all(r['semantic'] == {'skipped': 'carry_unsettled'} for r in semantic)
        assert kept and all(carry == [row] for carry in kept)
        assert jobs == []
    finally:
        release()


def test_local_only_pass_skips_a_row_a_cloud_reservation_owns(tmp_path, monkeypatch):
    """The lookahead pass (allow_cloud=False) must not infer an owned row locally."""
    led = _ledger(tmp_path)
    led.save_messages([_message(mid=1, body=BODY, posted_at='2026-09-18T00:00:00+09:00'),
                       _message(mid=2, body=BODY + '二')])
    # synthetic unresolved paid reservation on message 1 (no route: conservative hold)
    led.artifact_add(cloud_policy.KINDS[0], '{}', project_id=1, message_id=1, model='synthetic-model',
                     meta={'reservation_id': 'synthetic-reservation'})
    _resident_env(monkeypatch, tmp_path, led, [])
    bodies = []

    def extract(body, **_kw):
        bodies.append(body)
        return {'summary': '合成', 'vitals': {'bt': 37.0}, 'symptoms': [{'text': '咳'}]}
    monkeypatch.setattr(worker, 'llm_extract', extract)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', worker.llm_extract)
    try:
        res = extract_llm_runner.run_pending(led, limit=1, budget_s=600, oldest_first=True,
                                             allow_cloud=False, background=True, carry=[])
        assert led.artifacts('extract_llm', message_id=1) == []
        assert len(bodies) == 1 and (BODY + '二') in bodies[0]
        assert len(led.artifacts('extract_llm', message_id=2)) == 1
        assert not res.get('carry')
    finally:
        led.close()
