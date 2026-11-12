"""Patient context units report fixed wait causes, hold unresolved sources and claim patients."""
import json
import os

import pytest

import cloud_client
import cloud_inference
import patient_context_refresh as refresh
import patient_context_refresh_state as state
from test_patient_context_refresh import finish, runtime, seed, step

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
    assert hold['cloud_unit_hold'] == {'until': clock.wall - 1 + 21600, 'reason': 'nightly_cloud_not_sent'}
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
