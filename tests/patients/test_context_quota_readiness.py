"""Synthetic overview quota boundaries preserve first publication and read fences."""
import json

import pytest

import patient_context_quota as quota
import patient_context_refresh as refresh
import patient_context_refresh_readiness as readiness
import patient_context_refresh_state as state
from test_patient_context_generation_limits import _ready_sources
from test_patient_context_refresh import CFG, runtime as runtime, seed, step


@pytest.mark.parametrize('cfg', [{}, CFG])
def test_initial_context_does_not_wait_for_a_second_generation(runtime, cfg):
    led, _, calls = runtime
    seed(led)
    for _ in range(12):
        result = step(runtime, cfg=cfg)
        view = readiness.context_view_input(led.db, 1, cfg=cfg)
        if view is not None:
            break
        assert result['status'] == 'progressed', result
        assert result['lane'] in ('B', 'C')
        assert state._latest(led.db, state.KIND, 1)[0] == {}
        assert led.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?', (quota.KIND,)).fetchone()[0] == 0
    assert view is not None and view['summary']
    assert sum('reports' in call for call in calls) == 1
    assert led.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?', (quota.KIND,)).fetchone()[0] == 1


def test_empty_noop_overview_uses_no_generation_slot_or_model(runtime):
    led, _, calls = runtime
    seed(led, body='', proof=False)
    assert step(runtime)['status'] == 'progressed'
    assert readiness.context_view_input(led.db, 1, cfg=CFG) is not None
    assert state._latest(led.db, state.KIND, 1)[0]['summary'] == []
    assert calls == []
    assert led.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?', (quota.KIND,)).fetchone()[0] == 0
    assert step(runtime)['status'] == 'complete'


def test_quota_dispatch_proof_is_captured_outside_the_writer(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    _ready_sources(runtime)
    original, observed = readiness._inference_ready, []
    def ready(reader, *args, **kwargs):
        assert not led.db.in_transaction
        observed.append(reader)
        return original(reader, *args, **kwargs)
    monkeypatch.setattr(readiness, '_inference_ready', ready)
    outcome = step(runtime)
    assert outcome['status'] == 'progressed' and outcome['lane'] == 'A'
    assert observed and readiness.context_view_input(led.db, 1, cfg=CFG) is not None
    assert led.db.execute('SELECT COUNT(*) FROM artifacts WHERE kind=?', (quota.KIND,)).fetchone()[0] == 1


@pytest.mark.parametrize('cancel', [False, True])
def test_returned_quota_cleanup_preserves_outcome_and_propagates_control(runtime, monkeypatch, cancel):
    led, _, _ = runtime
    seed(led)
    _ready_sources(runtime)
    original_chat = refresh.local_llm.admitted_chat
    original_ready = readiness._inference_ready
    unverified, attempted = [], []
    def chat(*args, **kwargs):
        value = original_chat(*args, **kwargs)
        unverified.append(True)
        return value
    def ready(*args, **kwargs):
        return False if unverified else original_ready(*args, **kwargs)
    failure = KeyboardInterrupt('synthetic cleanup control') if cancel else RuntimeError('synthetic cleanup failure')
    def finish(*args, **kwargs):
        assert kwargs['outcome'] == 'unknown'
        attempted.append(True)
        raise failure
    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', chat)
    monkeypatch.setattr(readiness, '_inference_ready', ready)
    monkeypatch.setattr(quota, 'finish_tx', finish)
    if cancel:
        with pytest.raises(KeyboardInterrupt) as caught:
            step(runtime)
        assert caught.value is failure
    else:
        assert step(runtime)['status'] == 'deferred'
    assert attempted == [True] and not led.db.in_transaction
    assert state._latest(led.db, state.KIND, 1)[0] == {}
    record, = led.db.execute('SELECT content FROM artifacts WHERE kind=?', (quota.KIND,)).fetchall()
    assert json.loads(record[0])['state'] == 'started'
