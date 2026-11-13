"""Patient context units report fixed wait causes, hold unresolved sources and claim patients."""
from contextlib import closing
import json
import os

import pytest

from ledger import Ledger
import cloud_client
import cloud_inference
import patient_context_refresh as refresh
import patient_context_refresh_state as state
from test_patient_context_refresh import CFG, finish, runtime, seed, step

__all__ = ['runtime']

FIRST, SECOND = '生活環境: 合成の第一記録。', '生活環境: 合成の第二記録。'


def _scan_row(led, mid):
    row = led.db.execute('SELECT content FROM artifacts WHERE kind=? AND project_id=1 AND message_id=? '
                         'ORDER BY artifact_id DESC LIMIT 1', (state.SCAN, mid)).fetchone()
    return json.loads(row[0]) if row else None


def _two_sources(led):
    seed(led, mid=1, pid=1, body=FIRST)
    seed(led, mid=2, pid=1, body=SECOND)


def _refuse_local(monkeypatch, mids=None, error=None):
    """Refuse local substitution for ``mids``, or for the first unit seen."""
    original = cloud_inference.guard_context_local
    refused = set(mids or ())

    def guard(ledger, cfg, prompt, *, message_id, **kwargs):
        if mids is None and not refused:
            refused.add(message_id)
        if message_id in refused:
            raise error or cloud_client.CloudError('budget_exceeded', 'cloud_unit_unresolved')
        return original(ledger, cfg, prompt, message_id=message_id, **kwargs)

    monkeypatch.setattr(cloud_inference, 'guard_context_local', guard)
    return refused


def test_refused_lane_b_unit_holds_only_its_source(runtime, monkeypatch):
    led, clock, calls = runtime
    _two_sources(led)
    refused = _refuse_local(monkeypatch)
    first = step(runtime)
    assert first['status'] == 'deferred' and first['reason'] == 'nightly_cloud_not_sent'
    (held,) = refused
    hold = _scan_row(led, held)
    assert hold['cloud_unit_hold'] == {'until': clock.wall - 1 + 21600, 'reason': 'nightly_cloud_not_sent',
                                       'released_before': 0.0}
    assert not hold.get('attempts') and not hold.get('failure') and not hold.get('complete')
    second = step(runtime)
    assert second['status'] == 'progressed' and second['lane'] == 'B'
    other = SECOND if held == 1 else FIRST
    assert [call.get('source') for call in calls] == [other]
    assert _scan_row(led, 3 - held)['complete']


def test_held_source_keeps_the_patient_unfinished_until_the_hold_expires(runtime, monkeypatch):
    led, clock, _ = runtime
    _two_sources(led)
    refused = _refuse_local(monkeypatch)
    results = [step(runtime) for _ in range(12)]
    (held,) = refused
    assert all(result['status'] != 'complete' for result in results)
    assert results[-1]['status'] == 'deferred' and results[-1]['reason'] == 'patient_context_waiting'
    assert results[-1]['wait_no_due_B_cloud_hold'] >= 1
    assert not _scan_row(led, held).get('complete')
    clock.wall = _scan_row(led, held)['cloud_unit_hold']['until'] + 1
    again = step(runtime)
    assert again['status'] == 'deferred' and again['reason'] == 'nightly_cloud_not_sent'
    assert _scan_row(led, held)['cloud_unit_hold']['until'] == clock.wall - 1 + 21600


def _apply_release(led, now):
    # A receipt-bound human release, as cloud-llm release-local records it.
    import cloud_policy
    meta = {'command_id': 'synthetic-release', 'actor': 'synthetic-operator',
            'reason': 'synthetic approval', 'through': 1,
            'before': now - cloud_policy.LOCAL_RELEASE_MIN_AGE_S}
    receipt = {'command_id': meta['command_id'], 'outcome': 'applied', 'control': 'local_release',
               'through': 1, 'actor': meta['actor'], 'reason': meta['reason'], 'processed_at': now}
    with led.db:
        led.db.execute("INSERT INTO artifacts(kind,content,model,meta,created_at) VALUES(?,?,?,?,?)",
                       (cloud_policy.LOCAL_RELEASE_KIND, '{}', 'synthetic', json.dumps(meta), now))
        led.db.execute("INSERT INTO command_receipts(command_id,payload_hash,outcome,receipt_json,processed_at)"
                       " VALUES(?,?,?,?,?)", (meta['command_id'], '0' * 64, 'applied', json.dumps(receipt), now))
    return meta['before']


def test_human_release_ends_the_hold_before_it_expires(runtime, monkeypatch):
    led, clock, _ = runtime
    _two_sources(led)
    refused = _refuse_local(monkeypatch)
    results = [step(runtime) for _ in range(12)]
    (held,) = refused
    assert results[-1]['reason'] == 'patient_context_waiting' and results[-1]['wait_no_due_B_cloud_hold'] >= 1
    until = _scan_row(led, held)['cloud_unit_hold']['until']
    before = _apply_release(led, clock.wall)
    # before the fix the released source still waited up to six hours
    again = step(runtime)
    assert clock.wall < until
    assert again['status'] == 'deferred' and again['reason'] == 'nightly_cloud_not_sent'
    # this synthetic guard still refuses, so the source is held again, bound to this release
    assert _scan_row(led, held)['cloud_unit_hold']['released_before'] == before
    assert step(runtime)['reason'] == 'patient_context_waiting'


@pytest.mark.parametrize('error', [ValueError('synthetic'), OSError('synthetic')])
def test_lane_b_deferral_without_writes_skips_the_run_lock(runtime, monkeypatch, error):
    led, _, _ = runtime
    _two_sources(led)
    _refuse_local(monkeypatch, {1, 2}, error)
    monkeypatch.setattr(state, 'acquire_run_lock', lambda: pytest.fail('nothing to save'))
    before = led.db.total_changes
    result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'nightly_cloud_not_sent'
    assert result['lane'] == 'B' and 'lock_wait_s' not in result
    # A transient guard error is not a refusal: no hold is written.
    assert _scan_row(led, 1) is None or 'cloud_unit_hold' not in _scan_row(led, 1)
    assert led.db.total_changes > before  # only the selection write


def test_hold_write_still_waits_for_the_run_lock(runtime, monkeypatch):
    led, _, _ = runtime
    _two_sources(led)
    _refuse_local(monkeypatch, {1, 2})
    attempts = []

    def busy():
        attempts.append(True)
        return None if len(attempts) < 3 else os.open(os.devnull, os.O_RDONLY)

    monkeypatch.setattr(state, 'acquire_run_lock', busy)
    monkeypatch.setattr(state.time, 'sleep', lambda _seconds: None, raising=False)
    result = step(runtime)
    assert result['reason'] == 'nightly_cloud_not_sent' and result['lock_wait_s'] == 2
    assert len(attempts) == 3


def _lane_sum(result, lane):
    prefix = f'wait_no_due_{lane}_'
    return sum(value for key, value in result.items() if key.startswith(prefix))


def test_no_due_lane_breakdown_matches_the_wait_counter(runtime):
    seed(runtime[0])
    finish(runtime)
    result = step(runtime)
    assert result['status'] == 'complete' and result['wait_no_due_lane'] >= 1
    assert set(refresh._NO_DUE_LANE_KEYS) <= set(result)
    # B and C always share a scope; every hit includes A or B.
    assert _lane_sum(result, 'B') == _lane_sum(result, 'C') == result['wait_no_due_B_none_incomplete']
    assert result['wait_no_due_C_none_unaudited'] == _lane_sum(result, 'C')
    assert max(_lane_sum(result, 'A'), _lane_sum(result, 'B')) <= result['wait_no_due_lane']
    assert result['wait_no_due_lane'] <= _lane_sum(result, 'A') + _lane_sum(result, 'B')


def test_other_source_scan_saved_during_inference_reports_scans(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led, mid=1, pid=1, body='生活環境: 合成の第一記録。')
    seed(led, mid=2, pid=1, body='生活環境: 合成の第二記録。')
    chat = refresh.local_llm.admitted_chat

    def chat_then_other_scan(owner, text, **kwargs):
        answer = chat(owner, text, **kwargs)
        if len(calls) == 1:
            other = 2 if '第一' in calls[0].get('source', '') else 1
            state._replace(led, state.SCAN, 1, {'complete': False}, {'source_key': other}, other)
            led.db.commit()
        return answer

    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', chat_then_other_scan)
    result = step(runtime)
    assert len(calls) == 1
    assert result['status'] == 'source_changed' and result['changed'] == 'scans'


def _progress(led, pid):
    return state._latest(led.db, state.PROGRESS, pid)[0]


@pytest.mark.parametrize('marked', [True, False])
def test_concurrent_worker_skips_a_patient_with_a_unit_in_flight(runtime, monkeypatch, marked):
    led, clock, calls = runtime
    seed(led, mid=1, pid=1)
    if marked:
        seed(led, mid=2, pid=2)
    else:
        # Control: without the mark the second worker takes the same unit.
        monkeypatch.setattr(refresh, '_unit_claimed', lambda progress, now: False)
    chat = refresh.local_llm.admitted_chat
    db_path = led.db.execute('PRAGMA database_list').fetchone()[2]
    nested = {}

    def chat_with_second_worker(owner, text, **kwargs):
        answer = chat(owner, text, **kwargs)
        if not nested:
            nested['pid'] = next(pid for pid in (1, 2) if _progress(led, pid).get('unit_claim'))
            nested['claim'] = _progress(led, nested['pid'])['unit_claim']
            nested['claimed_unit'] = _progress(led, nested['pid'])['last_attempt_unit']
            with closing(Ledger(db_path)) as other:
                nested['result'] = refresh.run_due(other, CFG, deadline=clock.mono + 600, admit=lambda: True)
            nested['unit_after'] = {pid: _progress(led, pid).get('last_attempt_unit') for pid in (1, 2)}
        return answer

    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', chat_with_second_worker)
    first = step(runtime)
    assert nested['claim']['until'] <= clock.wall - 1 + refresh._UNIT_CLAIM_SECONDS
    if marked:
        # The fair rotation may start the second scan on the other patient, so the
        # counter can stay 0; what must hold is that the marked patient is skipped.
        assert nested['result']['status'] == 'progressed'
        assert nested['unit_after'][nested['pid']] == nested['claimed_unit']
        assert nested['unit_after'][3 - nested['pid']] == 2
        assert first['status'] == 'progressed' and first['done'] == 1
        assert 'unit_claim' not in _progress(led, nested['pid'])
        assert all('unit_claim' not in _progress(led, pid) for pid in (1, 2))
    else:
        assert nested['result']['status'] == 'progressed'
        assert first['status'] == 'source_changed' and first['changed'] == 'scans'


def test_deferred_unit_releases_its_mark(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    _refuse_local(monkeypatch, {1}, ValueError('synthetic'))
    result = step(runtime)
    assert result['status'] == 'deferred' and result['reason'] == 'nightly_cloud_not_sent'
    assert 'unit_claim' not in _progress(led, 1)
    again = step(runtime)
    assert again['wait_unit_claimed'] == 0 and again['reason'] == 'nightly_cloud_not_sent'


@pytest.mark.parametrize('offset', [-1, 600])
def test_only_an_unexpired_mark_steers_selection(runtime, offset):
    led, clock, calls = runtime
    seed(led, mid=1, pid=1)
    content, meta = state._latest(led.db, state.PROGRESS, 1)
    state._replace(led, state.PROGRESS, 1, {**content, 'unit_claim': {
        'token': 'f' * 32, 'until': clock.wall + offset}}, meta)
    led.db.commit()
    result = step(runtime)
    if offset < 0:
        assert result['status'] == 'progressed' and result['wait_unit_claimed'] == 0
    else:
        assert result['status'] == 'deferred' and result['reason'] == 'patient_context_waiting'
        assert result['wait_unit_claimed'] == 1 and not calls
        # Another worker's mark is never removed by this worker.
        assert _progress(led, 1)['unit_claim']['token'] == 'f' * 32
