"""Exercise overview generation limits through synthetic selection and dispatch."""
import json

import pytest

import cloud_inference
import patient_context_plan as plan
import patient_context_quality_controller as quality
import patient_context_quota as quota
import patient_context_refresh as refresh
import patient_context_refresh_sources as sources
import patient_context_refresh_state as state
from test_patient_context_refresh import CFG, document, runtime as runtime, seed, stamp, step
from anthropic_context_testkit import CFG as CLOUD_CFG, cloud as cloud, drain as drain_cloud, step as step_cloud


def _records(led, pid=1):
    return [(row['artifact_id'], json.loads(row['content'])) for row in led.db.execute(
        'SELECT artifact_id,content FROM artifacts WHERE kind=? AND project_id=? ORDER BY artifact_id',
        (quota.KIND, pid))]


def _only_a(monkeypatch):
    original = plan.lane_rotation
    monkeypatch.setattr(plan, 'lane_rotation',
        lambda best, rotation: original({'A': best['A']} if 'A' in best else {}, rotation))


def _claim(record):
    aid, doc = record
    return {'artifact_id': aid, 'input_key': doc['input_key'], 'token': doc['token']}


def test_generation_is_committed_before_dispatch_and_finished_with_overview(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    original, observed = refresh.local_llm.admitted_chat, []
    def chat(*args, **kwargs):
        assert not led.db.in_transaction
        record, = _records(led)
        assert record[1]['state'] == 'started' and quota.owns(led.db, 1, _claim(record), now=clock.wall)
        assert document(led) is None
        observed.append(_claim(record))
        return original(*args, **kwargs)
    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', chat)
    result = step(runtime)
    assert result['lane'] == 'A' and result['status'] == 'progressed'
    record, = _records(led)
    assert observed and record[1]['state'] == 'complete' and document(led) is not None
    assert not quota.owns(led.db, 1, observed[0], now=clock.wall)


def test_identical_read_only_republication_does_not_reserve_again(runtime, monkeypatch):
    led, _, calls = runtime
    seed(led)
    _only_a(monkeypatch)
    assert step(runtime)['status'] == 'progressed'
    original = _records(led)
    called = len(calls)
    cfg = {**CFG, 'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    assert step(runtime, cfg=cfg)['status'] == 'progressed'
    assert _records(led) == original and len(calls) == called


def test_interval_waiting_patient_keeps_other_patients_and_source_lanes_runnable(runtime):
    led, _, _ = runtime
    seed(led)
    assert step(runtime)['lane'] == 'A'
    seed(led, mid=2, body='生活環境: 合成の追加情報。')
    seed(led, mid=3, pid=2)
    results = [step(runtime) for _ in range(8)]
    assert document(led, pid=2) is not None and any(row.get('lane') == 'B' for row in results)
    assert len(_records(led, 1)) == len(_records(led, 2)) == 1


def test_four_new_overviews_per_jst_day_and_new_day_reopens_new_input(runtime, monkeypatch):
    led, clock, calls = runtime
    _only_a(monkeypatch)
    start = stamp('2026-11-07T08:00:00')
    for index in range(4):
        clock.wall = start + index * quota.INTERVAL
        seed(led, body=f'生活環境: 合成の更新記録 {index}。')
        assert step(runtime)['status'] == 'progressed'
    clock.wall = start + 4 * quota.INTERVAL
    seed(led, body='生活環境: 合成の翌日待ち更新。')
    before = len(calls)
    assert step(runtime)['status'] == 'deferred'
    assert len(calls) == before and len(_records(led)) == 4
    clock.wall = start + 86400
    assert step(runtime)['status'] == 'progressed'
    assert len(_records(led)) == 5


@pytest.mark.parametrize('refusal', ['capacity', 'admission', 'turn', 'local_capacity'])
def test_proved_unsent_summary_refusals_return_the_slot(runtime, monkeypatch, refusal):
    led, clock, _ = runtime
    seed(led)
    def refused(*args, **kwargs):
        if refusal == 'turn':
            kwargs['error_out']['kind'] = 'busy'
            return None
        if refusal == 'admission':
            return {'status': None, 'admission': 'held'}
        pytest.fail('capacity refusal must occur before the model call')
    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', refused)
    opts = {'seconds': state.CALL_SECONDS + state.COMMIT_MARGIN - 1} if refusal == 'capacity' else {}
    if refusal == 'local_capacity':
        opts['local_admit'] = lambda: False
    outcome = step(runtime, **opts)
    assert outcome['status'] == 'deferred'
    record, = _records(led)
    assert record[1]['state'] == 'released' and document(led) is None
    assert quota.inspect(led.db, 1, record[1]['input_key'], now=clock.wall)['used'] == 0


def test_unsent_recovery_does_not_release_a_preexisting_unknown_generation(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    snapshot = sources._snapshot(led.db, 1, CFG, hydrate=False, source_keys=[1])
    key = refresh._generation_plan(led.db, 1, CFG, snapshot, clock.wall)['input_key']
    with led.db:
        led.db.execute('BEGIN IMMEDIATE')
        initial = quota.reserve_tx(led.db, 1, key, now=clock.wall, lease_until=clock.wall + 600)
        assert quota.begin_tx(led.db, 1, initial['claim'], now=clock.wall)
        assert quota.finish_tx(led.db, 1, initial['claim'], now=clock.wall, outcome='unknown')
    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', lambda *a, **k: {'status': None, 'admission': 'held'})
    assert step(runtime)['status'] == 'deferred'
    record, = _records(led)
    assert record[0] == initial['claim']['artifact_id']
    assert record[1]['state'] == 'unknown' and record[1]['resumes'] == 1
    assert quota.inspect(led.db, 1, key, now=clock.wall)['used'] == 1
    assert not quota.owns(led.db, 1, initial['claim'], now=clock.wall)


def test_late_generation_owner_cannot_publish_after_token_replacement(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    original, newer = refresh.local_llm.admitted_chat, []
    def change_owner(*args, **kwargs):
        _, previous = _records(led)[0]
        clock.wall = previous['lease_until'] + 1
        with led.db:
            led.db.execute('BEGIN IMMEDIATE')
            next_owner = quota.reserve_tx(led.db, 1, previous['input_key'], now=clock.wall,
                lease_until=clock.wall + 600)
            assert next_owner['allowed'] and next_owner['resume']
            newer.append(next_owner['claim'])
        return original(*args, **kwargs)
    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', change_owner)
    assert step(runtime)['status'] == 'source_changed'
    assert document(led) is None and quota.owns(led.db, 1, newer[0], now=clock.wall)


def test_first_full_catchup_is_identified_once_for_the_patient(runtime):
    led, clock, _ = runtime
    seed(led)
    assert step(runtime)['lane'] == 'A'
    for _ in range(8):
        snapshot = sources._snapshot(led.db, 1, CFG, hydrate=False, source_keys=[1])
        scans = sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)
        if sources._scan_ready(scans.get(1, {})):
            break
        assert step(runtime)['status'] == 'progressed'
    assert sources._scan_ready(scans.get(1, {}))
    row = led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=? AND project_id=1', (state.KIND,)).fetchone()
    meta = json.loads(row['meta'])
    meta['source_coverage'] = {'included': 1, 'observed': 2, 'unreflected': 1, 'acquisition_complete': True}
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    snapshot = sources._snapshot(led.db, 1, CFG, hydrate=False, source_keys=[1])
    first = refresh._generation_plan(led.db, 1, CFG, snapshot, clock.wall)
    assert first['catchup']
    progress = document(led, state.PROGRESS)
    with led.db:
        state._replace(led, state.PROGRESS, 1, {**progress, 'catchup_generation': 'f' * 64}, {})
    assert not refresh._generation_plan(led.db, 1, CFG, snapshot, clock.wall)['catchup']


def test_finite_quality_pages_resume_one_generation_without_waiting_for_lease(runtime, cloud, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    drain_cloud(runtime)
    initial = _records(led)
    assert len(initial) == 1
    progress = document(led, state.PROGRESS)
    with led.db:
        state._replace(led, state.PROGRESS, 1, {**progress, 'summary_pending': True}, {})
    monkeypatch.setattr(quality, '_quality_step', lambda *a, **k: ('pending', None))
    for _ in range(2):
        outcome = step_cloud(runtime)
        assert outcome['status'] == 'deferred' and outcome['reason'] == 'summary_quality_pending'
    record, = _records(led)
    assert record[0] == initial[0][0] and record[1]['resumes'] == initial[0][1]['resumes'] + 2
    assert record[1]['state'] == 'unknown' and quota.inspect(led.db, 1, record[1]['input_key'], now=clock.wall)['used'] == 1


def test_shadow_uses_new_token_on_the_same_generation_after_local_publication(runtime, cloud, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    cloud[1][0] = 'shadow'
    original_chat, first, shadow = refresh.local_llm.admitted_chat, [], []
    def local(*args, **kwargs):
        if 'reports' in json.loads(args[1]):
            first.append(_claim(_records(led)[0]))
        return original_chat(*args, **kwargs)
    monkeypatch.setattr(refresh.local_llm, 'admitted_chat', local)
    provider = cloud_inference.CloudInferenceUnit
    class Observe(provider):
        def __call__(self, prompt, **kwargs):
            if 'reports' in json.loads(prompt):
                assert document(led) is not None and first
                record, = _records(led)
                assert record[1]['state'] == 'started' and record[1]['resumes'] == 1
                assert record[1]['token'] != first[0]['token']
                assert not quota.owns(led.db, 1, first[0], now=clock.wall)
                shadow.append(_claim(record))
            return super().__call__(prompt, **kwargs)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', Observe)
    _only_a(monkeypatch)
    assert step(runtime, cfg=CLOUD_CFG)['status'] == 'progressed'
    record, = _records(led)
    assert shadow and record[1]['state'] == 'complete' and record[1]['resumes'] == 1


def test_overview_and_quota_completion_roll_back_together(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    original = quota.finish_tx
    def fail_after_finish(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs['outcome'] == 'complete':
            assert led.db.in_transaction and document(led) is not None and result
            raise RuntimeError('synthetic adoption failure')
        return result
    monkeypatch.setattr(quota, 'finish_tx', fail_after_finish)
    with pytest.raises(RuntimeError, match='synthetic adoption failure'):
        step(runtime)
    assert document(led) is None
    record, = _records(led)
    assert record[1]['state'] == 'started' and record[1]['finished_at'] is None


def test_partial_overview_first_full_catchup_can_run_before_three_hours(runtime):
    led, clock, _ = runtime
    seed(led)
    seed(led, mid=2, proof=False, body='生活環境: 合成の未解析記録。')
    assert step(runtime)['status'] == 'progressed'
    first, = _records(led)
    before_meta = json.loads(led.db.execute('SELECT meta FROM artifacts WHERE kind=? AND project_id=1',
                                           (state.KIND,)).fetchone()[0])
    assert before_meta['source_coverage']['unreflected'] == 1
    seed(led, mid=2, body='生活環境: 合成の未解析記録。')
    for _ in range(20):
        outcome = step(runtime)
        if len(_records(led)) == 2:
            assert outcome['lane'] == 'A' and outcome['status'] == 'progressed'
            break
    records = _records(led)
    assert len(records) == 2 and records[1][1]['state'] == 'complete'
    assert clock.wall - first[1]['reserved_at'] < quota.INTERVAL
    assert document(led, state.PROGRESS)['catchup_generation'] == records[1][1]['input_key']
    meta = json.loads(led.db.execute('SELECT meta FROM artifacts WHERE kind=? AND project_id=1',
                                    (state.KIND,)).fetchone()[0])
    assert meta['source_coverage']['unreflected'] == 0
