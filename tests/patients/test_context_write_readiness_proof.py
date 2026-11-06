"""Synthetic write-fence replay never replaces source or non-DB readiness checks."""

import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import copy
import json
import sqlite3
import time

import pytest

import drug_map
import semantic_jev
from test_patient_context_refresh import CFG, runtime as runtime, seed, step
from test_anthropic_context_refresh import cloud as cloud


def capture(runtime, *, cfg=CFG, complete=None, write_scope=None):
    led, clock, _ = runtime
    ready, proof, snapshot = patient_context_refresh_readiness._capture_readiness(led, 1, cfg, deadline=clock.mono+600,
        complete=complete, allow_dependency_repair=True, write_scope=write_scope)
    assert ready and proof is not None and snapshot is not None
    return proof


def replay(runtime, proof, *, cfg=CFG, complete=None):
    led, clock, _ = runtime
    led.db.execute('BEGIN IMMEDIATE')
    try:
        return patient_context_refresh_readiness._readiness_for_write(led, 1, cfg, proof, deadline=clock.mono+600,
            complete=complete, allow_dependency_repair=True)
    finally:
        led.db.rollback()


def test_expensive_proof_runs_without_blocking_other_local_saves(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=2, proof=False)
    second = sqlite3.connect(led.db.execute('PRAGMA database_list').fetchone()[2], timeout=0)
    original = patient_context_refresh_readiness._inference_ready
    calls = []

    def ready(reader, *args, **kwargs):
        assert not led.db.in_transaction, 'clinical proof must run before either writer fence'
        started = time.perf_counter()
        second.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
                       "VALUES('extract_llm',2,2,'{}','{}',0)")
        second.commit()
        calls.append(round((time.perf_counter() - started) * 1000, 3))
        time.sleep(0.02)
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', ready)
    try:
        assert step(runtime)['status'] == 'progressed'
        assert len(calls) >= 2 and not led.db.in_transaction
        print(json.dumps({'local_saves_during_ro_proof_ms': calls, 'artificial_proof_delay_ms': 20,
                          'clinical_proof_calls_in_writer': 0}, sort_keys=True))
    finally:
        second.close()


def test_valid_sql_replay_does_not_repeat_python_proof(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    proof = capture(runtime)
    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', lambda *args, **kwargs: pytest.fail('unexpected full proof'))
    assert replay(runtime, proof)[0] == 'verified'


def test_uncommitted_source_change_falls_back_to_writer_snapshot(runtime):
    led, clock, _ = runtime
    seed(led)
    proof = capture(runtime)
    led.db.execute('BEGIN IMMEDIATE')
    try:
        led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=1", ('f'*64,))
        status, snapshot, _ = patient_context_refresh_readiness._readiness_for_write(led, 1, CFG, proof, deadline=clock.mono+600,
            complete=None, allow_dependency_repair=True)
        assert status == 'changed' and snapshot['source_sig'] != proof['snapshot']['source_sig']
    finally:
        led.db.rollback()


def test_custom_callback_is_rechecked_in_the_main_transaction(runtime):
    led, _, _ = runtime
    seed(led)
    calls, allowed = [], [True]

    def complete(pid):
        calls.append((pid, led.db.in_transaction))
        return allowed[0]

    proof = capture(runtime, complete=complete)
    allowed[0] = False
    assert replay(runtime, proof, complete=complete)[0] == 'unknown'
    assert calls[0] == (1, False) and calls[-1] == (1, True)


@pytest.mark.parametrize('change', ['key', 'cfg', 'cloud_contract'])
def test_non_db_environment_change_never_adopts_stale_proof(runtime, monkeypatch, change):
    led, _, _ = runtime
    seed(led)
    key = [None]
    contract = [None]
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *args: key[0])
    monkeypatch.setattr(patient_context_refresh_readiness, '_required_cloud_extraction_contract', lambda *args: contract[0])
    proof = capture(runtime)
    cfg = copy.deepcopy(CFG)
    if change == 'key':
        key[0] = 'synthetic-key'
    elif change == 'cfg':
        cfg['semantic']['summary_mode'] = 'off'
    else:
        contract[0] = {'backend': 'openai', 'model': 'synthetic', 'prompt': 'a'*16}
    called = []

    def not_verified(*args, **kwargs):
        called.append(True)
        return False

    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', not_verified)
    assert replay(runtime, proof, cfg=cfg)[0] == 'unknown'
    assert called


def test_private_false_does_not_load_dictionary(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    monkeypatch.setattr(drug_map, 'active_dictionary', lambda *args: pytest.fail('private input not requested'))
    proof = capture(runtime)
    assert proof['dictionary'] is False
    assert replay(runtime, proof)[0] == 'verified'


def test_qc_clock_rollback_rechecks_the_current_cohort(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    proof = capture(runtime)
    proof['qc_cohort'], proof['qc_at'] = (), clock.wall
    clock.wall -= 86400
    called = []
    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', lambda *args, **kwargs: called.append(True) or False)
    assert replay(runtime, proof)[0] == 'unknown'
    assert called


def test_timeout_is_unknown_and_cannot_be_used_as_changed_evidence(runtime):
    led, clock, _ = runtime
    seed(led)
    proof = capture(runtime)
    clock.mono += 601
    led.db.execute('BEGIN IMMEDIATE')
    try:
        assert patient_context_refresh_readiness._readiness_for_write(led, 1, CFG, proof, deadline=clock.mono,
            complete=None, allow_dependency_repair=True) == ('unknown', None, None)
    finally:
        led.db.rollback()


@pytest.mark.parametrize('damage', ['mode', 'missing', 'bytes', 'cursor', 'generation'])
def test_private_dictionary_full_validation_and_generation_are_preserved(runtime, tmp_path, monkeypatch, damage):
    from medications_testkit import _dictionary
    led, _, _ = runtime
    seed(led)
    dictionary = _dictionary(tmp_path)
    path = tmp_path / 'fictional-drug-map.json'
    cfg = {**CFG, 'drug_map': {'path': str(path), 'sha256': dictionary.sha256}}
    generation = [dictionary.dict_id, dictionary.sha256, False, drug_map.RESOLVER_VERSION]
    led.artifact_add(drug_map.PROGRESS_KIND, json.dumps({'dictionary': generation, 'cursor': 0}))
    original = patient_context_refresh_readiness._inference_ready

    def private_ready(reader, pid, config, **kwargs):
        # Model the same pure currency gate used by private extraction frames.
        active, state = drug_map.active_dictionary(reader.db, config)
        if state != 'active' or active is None:
            return False
        return original(reader, pid, config, **kwargs)

    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', private_ready)
    proof = capture(runtime, cfg=cfg)
    assert proof['dictionary'] is True
    if damage == 'mode':
        path.chmod(0o644)
    elif damage == 'missing':
        path.unlink()
    elif damage == 'bytes':
        path.write_text('{}')
    else:
        value = {'dictionary': generation, 'cursor': -1} if damage == 'cursor' else {'dictionary': None, 'cursor': 0}
        second = sqlite3.connect(led.db.execute('PRAGMA database_list').fetchone()[2])
        try:
            second.execute('UPDATE artifacts SET content=? WHERE kind=?', (json.dumps(value), drug_map.PROGRESS_KIND))
            second.commit()
        finally:
            second.close()
    assert replay(runtime, proof, cfg=cfg)[0] == 'unknown'


def test_dictionary_cursor_only_progress_replays_without_full_clinical_proof(runtime, tmp_path, monkeypatch):
    from medications_testkit import _dictionary
    led, _, _ = runtime
    seed(led)
    dictionary = _dictionary(tmp_path)
    cfg = {**CFG, 'drug_map': {'path': str(tmp_path / 'fictional-drug-map.json'), 'sha256': dictionary.sha256}}
    generation = [dictionary.dict_id, dictionary.sha256, False, drug_map.RESOLVER_VERSION]
    led.artifact_add(drug_map.PROGRESS_KIND, json.dumps({'dictionary': generation, 'cursor': 0}))
    original = patient_context_refresh_readiness._inference_ready

    def private_ready(reader, *args, **kwargs):
        assert drug_map.active_dictionary(reader.db, cfg)[1] == 'active'
        return original(reader, *args, **kwargs)

    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', private_ready)
    proof = capture(runtime, cfg=cfg)
    second = sqlite3.connect(led.db.execute('PRAGMA database_list').fetchone()[2])
    try:
        second.execute('UPDATE artifacts SET content=? WHERE kind=?',
                       (json.dumps({'dictionary': generation, 'cursor': 7}), drug_map.PROGRESS_KIND))
        second.commit()
    finally:
        second.close()
    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', lambda *a, **k: pytest.fail('cursor-only conflict'))
    assert replay(runtime, proof, cfg=cfg)[0] == 'verified'


def test_operating_limits_do_not_invalidate_saved_ready_proof(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    proof = capture(runtime)
    cfg = {**CFG, 'cloud_llm': {'daily_cost_limit_usd': 0}}
    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', lambda *a, **k: pytest.fail('operating limit conflict'))
    assert replay(runtime, proof, cfg=cfg)[0] == 'verified'


def test_unknown_paid_final_replay_keeps_receipts_and_reuses_answer_without_post(runtime, cloud, monkeypatch):
    import cloud_policy
    from test_context_completed_carry import _completed, _receipts

    original_config = cloud_policy.cloud_config

    def context_only(cfg, *args, **kwargs):
        settings, errors = original_config(cfg, *args, **kwargs)
        settings['routes'] = ['patient_context']
        return settings, errors

    monkeypatch.setattr(cloud_policy, 'cloud_config', context_only)
    pending = _completed(runtime, cloud, monkeypatch, 'B')
    count = len(pending['posts'])
    pending['blocked'][0] = False
    original = patient_context_refresh_readiness._readiness_for_write
    monkeypatch.setattr(patient_context_refresh_readiness, '_readiness_for_write', lambda *a, **k: ('unknown', None, None))
    outcome = step(runtime, cfg=pending['cfg'], local_admit=lambda: False)
    assert outcome['status'] == 'deferred' and outcome['reason'] == 'patient_context_readiness_unverified'
    assert len(pending['posts']) == count and _receipts(pending['led']) == pending['receipts']
    assert not pending['led'].artifacts('cloud_usage_disposition')
    monkeypatch.setattr(patient_context_refresh_readiness, '_readiness_for_write', original)
    outcome = step(runtime, cfg=pending['cfg'], local_admit=lambda: False)
    assert outcome['status'] == 'progressed' and outcome['done'] == 1
    assert len(pending['posts']) == count


@pytest.mark.parametrize('phase', ['capture', 'replay'])
def test_writing_complete_callback_cannot_seal_or_adopt_stale_ro_snapshot(runtime, phase):
    led, clock, _ = runtime
    seed(led)
    writing = [phase == 'capture']

    def complete(pid):
        assert pid == 1
        if writing[0]:
            led.db.execute('UPDATE messages SET content_hash=? WHERE message_id=1', ('f'*64,))
        return True

    if phase == 'capture':
        try:
            _, proof, _ = patient_context_refresh_readiness._capture_readiness(led, 1, CFG, deadline=clock.mono+600,
                complete=complete, allow_dependency_repair=True)
            assert proof is None
        finally:
            led.db.rollback()
    else:
        proof = capture(runtime, complete=complete)
        writing[0] = True
        assert replay(runtime, proof, complete=complete)[0] == 'unknown'


def test_final_paid_guard_uses_captured_source_scan_occurrence_and_cloud_input(runtime, cloud, monkeypatch):
    import cloud_policy
    from test_context_completed_carry import _completed

    original_config = cloud_policy.cloud_config

    def context_only(cfg, *args, **kwargs):
        settings, errors = original_config(cfg, *args, **kwargs)
        settings['routes'] = ['patient_context']
        return settings, errors

    monkeypatch.setattr(cloud_policy, 'cloud_config', context_only)
    pending = _completed(runtime, cloud, monkeypatch, 'B')
    led = pending['led']
    before = len(pending['posts'])
    baseline = [led.db.total_changes]
    original_capture = patient_context_refresh_readiness._capture_readiness

    def captured(*args, **kwargs):
        value = original_capture(*args, **kwargs)
        baseline[0] = led.db.total_changes
        return value

    monkeypatch.setattr(patient_context_refresh_readiness, '_capture_readiness', captured)
    violations, writing = [], [False]

    def transaction(sql):
        command = sql.strip().upper()
        if command.startswith('BEGIN IMMEDIATE'):
            writing[0] = True
        elif command in ('COMMIT', 'ROLLBACK', 'END'):
            writing[0] = False

    led.db.set_trace_callback(transaction)
    for module, name in ((patient_context_refresh_sources, '_snapshot'),
                         (patient_context_refresh_state, '_scan_sig'),
                         (patient_context_refresh_sources, '_source_occurrence'),
                         (patient_context_refresh_state, '_cloud_input_binding')):
        original = getattr(module, name)

        def checked(*args, _name=name, _original=original, **kwargs):
            if writing[0] and led.db.total_changes == baseline[0]:
                violations.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, checked)
    pending['blocked'][0] = False
    outcome = step(runtime, cfg=pending['cfg'], local_admit=lambda: False)
    assert outcome['status'] == 'progressed' and len(pending['posts']) == before, outcome
    assert violations == []
    led.db.set_trace_callback(None)


@pytest.mark.parametrize("observation", ["false", "error", "changed"])
def test_unknown_immediately_after_response_keeps_real_finance_and_recovers_without_post(runtime, cloud, monkeypatch, observation):
    import anthropic_client
    import cloud_policy
    from test_context_completed_carry import _completed

    original_config = cloud_policy.cloud_config

    def context_only(cfg, *args, **kwargs):
        settings, errors = original_config(cfg, *args, **kwargs)
        settings['routes'] = ['patient_context']
        return settings, errors

    monkeypatch.setattr(cloud_policy, 'cloud_config', context_only)
    pending = _completed(runtime, cloud, monkeypatch, 'B')
    pending['blocked'][0] = False
    assert step(runtime, cfg=pending['cfg'], local_admit=lambda: False)['status'] == 'progressed'
    led, _, local_calls = runtime
    unknown = [False]
    original_ready = patient_context_refresh_readiness._inference_ready
    original_request = anthropic_client.AnthropicClient.request

    def not_observed(*args, **kwargs):
        if unknown[0] and observation == 'error':
            raise RuntimeError('synthetic observation unavailable')
        return False if unknown[0] else original_ready(*args, **kwargs)

    def response(*args, **kwargs):
        value = original_request(*args, **kwargs)
        unknown[0] = True
        if observation == 'changed':
            led.db.execute('UPDATE messages SET content_hash=? WHERE message_id=1', ('f'*64,))
            led.db.commit()
        return value

    monkeypatch.setattr(patient_context_refresh_readiness, '_inference_ready', not_observed)
    monkeypatch.setattr(anthropic_client.AnthropicClient, 'request', response)
    for _ in range(6):
        outcome = step(runtime, cfg=pending['cfg'], local_admit=lambda: False)
        if unknown[0]:
            break
    assert unknown[0]
    posts = len(pending['posts'])
    if observation == 'changed':
        results = led.artifacts('cloud_usage_result', project_id=1)
        assert len(results) == posts
        assert any(json.loads(row['meta']).get('disposition') == 'discarded_scope' for row in results)
        assert not led.artifacts(patient_context_refresh_state.KIND) and not local_calls
        return
    assert outcome['status'] == 'deferred'
    assert not local_calls and not led.artifacts('cloud_usage_disposition')
    reserves = led.artifacts('cloud_usage_reserve', project_id=1)
    results = led.artifacts('cloud_usage_result', project_id=1)
    assert len(reserves) == len(results) == posts
    for reserve in reserves:
        evidence = cloud_policy.reservation_evidence(led.db, reserve['artifact_id'])
        assert evidence['result']['http_class'] == 'http_200'
        assert evidence['result'].get('disposition') is None
        assert evidence['result']['answer_text_sha256_v1']
    assert all(json.loads(row['content'])['text'] for row in results)
    unknown[0] = False
    for _ in range(5):
        outcome = step(runtime, cfg=pending['cfg'], local_admit=lambda: False)
        if outcome['status'] == 'progressed' and outcome.get('lane') == 'A':
            break
    assert outcome['status'] == 'progressed' and outcome['lane'] == 'A'
    assert len(pending['posts']) == posts and not local_calls
