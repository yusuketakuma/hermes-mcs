"""Synthetic acceptance of idle-night work, source validity, and resumable coverage."""
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare
import extract_llm_progress
import extract_llm_proofs
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_selection
import extract_llm_store

import patient_context_quality_controller
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import patient_context_plan as context_plan
import mcs_queries
from datetime import datetime
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from extract_testkit import _ledger, _message
from mcs_queries import JST
import cloud_inference
import patient_context_refresh as context_refresh


CFG = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': True}},
       'semantic': {'mode': 'assist', 'summary_mode': 'assist', 'project_ids': None}}


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=JST).timestamp()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    clock = SimpleNamespace(wall=stamp('2026-10-17T23:00:00'), mono=1000)
    monkeypatch.setattr(patient_context_refresh_state, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.mono))
    monkeypatch.setattr(patient_context_refresh_state, 'acquire_run_lock', lambda: os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(context_refresh.local_llm, 'resolve', lambda cfg: ('http://invalid.test', 'synthetic'))
    monkeypatch.setattr(context_refresh.local_llm, 'admission_enabled', lambda: True)
    monkeypatch.setattr(context_refresh.local_llm, 'chat', lambda *a, **k: pytest.fail('unstubbed chat'))
    calls = []
    def chat(_owner, text, **kwargs):
        assert not led.db.in_transaction, 'inference must not hold a write transaction'
        payload = json.loads(text)
        calls.append(payload)
        if 'reports' in payload:
            answer = {'fact_ids': [r['id'] for r in payload['reports'][:12]]}
        else:
            body = payload['source']
            answer = {'patient_context': [{'category': 'living', 'text': body,
                'evidence': body, 'subject': 'unspecified', 'details': []}]}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answer)}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', chat)
    yield led, clock, calls
    led.close()


def _raw_proof(led, pid, mid, *, cloud=False):
    import extract_llm
    import inference_dependency_loss
    from inference_version import CURRENT_VERSION, VERSION_KEY
    row = dict(led.db.execute('SELECT * FROM messages WHERE project_id=? AND message_id=?', (pid, mid)).fetchone())
    row['_source_binding'] = inference_dependency_loss.source_binding(led.db, pid, mid)
    contract = cloud_inference.cloud_provider_contract() if cloud else None
    model = contract['model'] if cloud else 'synthetic'
    row['_target'] = ('', model)
    if cloud:
        row['_provider_contract'] = contract
    context = extract_llm._thread_context(led.db, row)
    size = extract_llm._saved_chunk_size(led.db, row, context)
    chunks = extract_llm.plan_chunks(row['body_text'], size)
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
            'extract_version': extract_llm.EXTRACT_VERSION, 'actual_model': model,
            'plan_version': extract_llm.PLAN_VERSION,
            'source_binding': row['_source_binding'], 'thin_retried': True,
            'context_binding': extract_llm._chunk_context(row, context),
            'integrity': {'completeness_chunks': len(chunks), 'completeness': {
                str(i): {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}
                for i in range(len(chunks))}}}
    if cloud:
        meta.update(contract)
    led.artifact_add('extract_llm', json.dumps({'summary': '合成の経過報告'}),
                     project_id=pid, message_id=mid, model=model, meta=meta)


def _semantic_proof(led, pid, mid, cfg=CFG, *, observation_quote=None):
    import time
    import semantic_audit
    import semantic_drain
    import semantic_llm
    import semantic_policy
    import semantic_store
    policy, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    bundle = semantic_store.thread_bundle(led.db, pid, mid, [mid])
    member = next(m for m in bundle['members'] if m['message_id'] == mid)
    fp = bundle['source_fingerprint']
    policy_fp = semantic_policy.policy_fingerprint(policy)
    deadline = time.monotonic() + 60

    def extract(prompt):
        quote = prompt.split('<<<\n', 1)[1].split('\n>>>', 1)[0]
        if observation_quote is not None:
            assert observation_quote in member['body_original']
            if observation_quote not in quote:
                return json.dumps({'facts': []})
            quote = observation_quote
        return json.dumps({'facts': [{'statement': quote, 'evidence_quote': quote,
            'kind': 'observation', 'status': 'not_stated', 'polarity': 'affirmed'}]})

    early, facts = semantic_drain._fact_legacy(
        led, member, pid, mid, fp, policy_fp, extract, deadline)
    assert early is None and facts and all(f['_evidence'] for f in facts)
    summary = semantic_llm.summarize(lambda _prompt: json.dumps({'claims': [
        {'section': 'other', 'claim_kind': 'reported_fact', 'text': f['statement'],
         'fact_refs': [i]} for i, f in enumerate(facts)], 'limitations': []}),
        bundle, mid, facts, {}, deadline=deadline)
    assert summary is not None and summary['claims']
    summary['_facts'] = facts

    class QuotedSourceAudit:
        def evaluate(self, state, questions, _deadline):
            quotes = {entry['text'].strip() for entry in state['context']
                      if entry['role'] == 'evidence_quote'}
            claims = {claim['id']: claim['text'] for claim in state['target']['claims']}
            assert set(questions) <= set(claims)
            # Only verbatim source claims are supported by this synthetic client.
            return {'answers': {qid: {'choice': 'supports' if claims[qid] in quotes
                                      else 'not_supported', 'confidence': 1.0}
                                for qid in questions}}

    code_findings = semantic_audit.audit_code(bundle, facts, summary)
    findings, evaluated = semantic_audit.audit_claims(
        QuotedSourceAudit(), bundle, summary, deadline)
    status = semantic_audit.audit_status_for(code_findings, findings, evaluated, False)
    assert status == 'PASS', (code_findings, findings)
    summary['audit_status'] = status
    result = {'summary': summary, 'findings': code_findings + findings, 'repaired': False,
              'claim_input_binding': semantic_drain._claim_input_binding(
                  pid, mid, fp, policy_fp, member['revision'], summary, facts)}
    with led.db:
        semantic_drain._write_result(led, pid, mid, result, fp,
            {m['message_id']: m for m in bundle['members']}, status,
            policy_fp, policy['summary_mode'])


def seed(led, mid=1, pid=1, body='生活環境: 合成の居住記録。', state='full', *,
         proof=True, semantic_quote=None):
    led.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (?,?)',
                   (pid, f'synthetic-{pid}'))
    led.db.commit()
    led.save_messages([_message(mid, body, state, project_id=pid)])
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=?',
                   (stamp('2026-10-17T22:00:00'), mid))
    # The normal synthetic fixture has exhausted history; partial acquisition is explicit.
    led.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 WHERE project_id=?",
                   (pid,))
    led.db.commit()
    saved_body = led.db.execute('SELECT body_text FROM messages WHERE project_id=? AND message_id=?',
                                (pid, mid)).fetchone()[0]
    if proof and state == 'full' and saved_body:
        _raw_proof(led, pid, mid)
        _semantic_proof(led, pid, mid, observation_quote=semantic_quote)


def step(runtime, *, cfg=CFG, admit=lambda: True, seconds=600, **kw):
    led, clock, _ = runtime
    result = context_refresh.run_due(led, cfg, deadline=clock.mono + seconds, admit=admit, **kw)
    clock.wall += 1
    clock.mono += 1
    return result


def finish(runtime, limit=60, *, cfg=CFG, waiting=False):
    for _ in range(limit):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            assert not waiting, 'unfinished evidence must not be reported complete'
            return
        if waiting and result['status'] == 'deferred' and result.get('reason') == 'patient_context_waiting':
            assert result['done'] == 0
            return
        assert result['status'] == 'progressed', result
    pytest.fail('finite synthetic workload did not finish')


def document(led, kind=patient_context_refresh_state.KIND, pid=1):
    row = led.db.execute('SELECT content FROM artifacts WHERE kind=? AND project_id=? '
                         'ORDER BY artifact_id DESC LIMIT 1', (kind, pid)).fetchone()
    return json.loads(row[0]) if row else None


def _prepare_summary(runtime, *, cfg=CFG):
    """Run real source scans and quotation audits before a summary-only fault."""
    led, _, _ = runtime
    for _ in range(60):
        snapshot = patient_context_refresh_sources._snapshot(led.db, 1, cfg)
        sources = [source for source in snapshot['sources'] if context_plan.source_usable(source)]
        assert sources, 'summary preparation requires a nonempty source fixture'
        scans = patient_context_refresh_sources._current_scans(
            led.db, 1, snapshot['sources'], snapshot['contract'], cfg=cfg)
        if all(patient_context_refresh_sources._scan_ready(scans.get(context_plan.source_key(source), {}))
               for source in sources):
            return
        result = step(runtime, cfg=cfg)
        assert result['status'] == 'progressed' and result['lane'] in ('B', 'C'), result
    pytest.fail('synthetic source scans and audits did not finish')


@pytest.mark.parametrize('offsets', [(0, 0, 0), (0, -1, -2)])
@pytest.mark.parametrize('new_arrivals', [False, True])
def test_worker_rotates_ready_patients_despite_clock_and_new_arrivals(runtime, offsets, new_arrivals):
    led, clock, _ = runtime
    for pid in (1, 2, 3):
        seed(led, mid=pid, pid=pid)
    started = clock.wall
    selected = []
    for index, offset in enumerate(offsets):
        clock.wall = started + offset
        if new_arrivals and index:
            seed(led, mid=10 + index, pid=1)
        outcome = step(runtime)
        assert outcome['status'] == 'progressed', outcome
        selected.append(led.db.execute(
            "SELECT project_id FROM artifacts WHERE kind=? AND project_id>0 "
            "ORDER BY artifact_id DESC LIMIT 1", (patient_context_refresh_state.PROGRESS,)).fetchone()[0])
    assert selected == [1, 2, 3]


@pytest.mark.parametrize('content', ['{', '{"last_attempt_unit":true}',
                                      '{"last_attempt_unit":"99"}', '{"last_attempt_unit":-1}'])
def test_worker_ignores_invalid_attempt_order_and_skips_unready_patient(runtime, content):
    from inference_version import CURRENT_VERSION, VERSION_KEY
    led, _, _ = runtime
    seed(led, mid=1, pid=1, proof=False)
    seed(led, mid=2, pid=2)
    seed(led, mid=3, pid=3)
    meta = {VERSION_KEY: CURRENT_VERSION}
    led.artifact_add(patient_context_refresh_state.PROGRESS, content, project_id=2, meta=meta)
    led.artifact_add(patient_context_refresh_state.PROGRESS, '{"last_attempt_unit":1}', project_id=3, meta=meta)
    result = step(runtime)
    assert result['status'] == 'progressed', result
    assert document(led, patient_context_refresh_state.PROGRESS, pid=2)['last_attempt_unit'] == 1
    assert document(led, patient_context_refresh_state.PROGRESS, pid=1) is None


@pytest.mark.parametrize('at,day', [
    ('2026-10-17T12:00:00', '2026-10-17'), ('2026-10-17T22:59:59', '2026-10-17'),
    ('2026-10-17T23:59:59', '2026-10-17'), ('2026-10-18T00:00:00', '2026-10-18'),
    ('2026-10-18T03:29:59', '2026-10-18'), ('2026-10-18T03:30:00', '2026-10-18'),
    ('2026-10-18T04:00:00', '2026-10-18')])
def test_workday_window(at, day):
    """The workday follows the JST calendar, including 00:00 and the old boundary."""
    end = stamp('2026-10-18T00:00:00' if day == '2026-10-17' else '2026-10-19T00:00:00')
    assert patient_context_refresh_state.workday_window(stamp(at)) == (day, end)


@pytest.mark.parametrize('cfg', [None, {'extract': {'patient_context_nightly': {'enabled': False}}},
    {'extract': {'patient_context_nightly': {'enabled': 1}}}])
def test_explicit_off_or_invalid_config_never_touches_queue(runtime, cfg):
    led, _, calls = runtime
    seed(led)
    before = led.db.total_changes
    assert step(runtime, cfg=cfg) == {'status': 'off', 'done': 0}
    assert not calls and led.db.total_changes == before


def test_default_enables_generation_and_private_reading(runtime):
    led, _, _ = runtime
    seed(led)
    assert step(runtime, cfg={})['status'] == 'progressed'
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg={}) is None
    finish(runtime, cfg={})
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg={}) is not None


@pytest.mark.parametrize('at,seconds', [('2026-10-17T23:59:30', 64),
                                      ('2026-10-17T23:00:00', 64)])
def test_insufficient_time_never_starts_inference(runtime, at, seconds):
    led, clock, calls = runtime
    seed(led)
    clock.wall = stamp(at)
    assert step(runtime, seconds=seconds)['status'] == 'deferred'
    assert not calls and document(led) is None


def test_busy_lane_yields_without_work(runtime):
    led, _, calls = runtime
    seed(led)
    assert step(runtime, admit=lambda: False)['status'] == 'deferred'
    assert not calls and document(led) is None


def test_completed_work_does_not_repeat_llm_next_night(runtime):
    led, clock, calls = runtime
    seed(led)
    finish(runtime)
    count = len(calls)
    generated = document(led)['generated_at']
    clock.wall = stamp('2026-10-18T23:00:00')
    assert step(runtime)['status'] == 'complete'
    assert len(calls) == count and document(led)['generated_at'] == generated
    assert patient_context_refresh_readiness.status(led.db, CFG)['complete'] == 1


def test_chunk_checkpoint_survives_next_night_without_repeating_prefix(runtime):
    led, clock, calls = runtime
    body = '合成記録の第一の区間。' * 240 + '\n' + '合成記録の第二の区間。' * 240
    seed(led, body=body)
    clock.wall = stamp('2026-10-17T23:59:00')
    assert len(context_refresh.context_plan.plan_chunks(body)) > 1
    assert step(runtime)['lane'] == 'B'
    saved = document(led, patient_context_refresh_state.SCAN)
    assert saved['cursor'] == 1 and not saved['complete']
    assert document(led) is None, 'partial scan must not be published'
    first_source = calls[-1]['source']
    clock.wall = stamp('2026-10-18T00:00:00')
    assert step(runtime)['lane'] == 'B'
    assert document(led, patient_context_refresh_state.SCAN)['cursor'] == 2
    assert calls[-1]['source'] != first_source
    finish(runtime)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)['facts']


def test_legacy_0330_day_id_does_not_reset_an_existing_chunk_checkpoint(runtime):
    led, clock, calls = runtime
    body = '合成記録の第一の区間。' * 240 + '\n' + '合成記録の第二の区間。' * 240
    seed(led, body=body)
    clock.wall = stamp('2026-10-18T00:30:00')
    assert step(runtime)['lane'] == 'B'
    assert document(led, patient_context_refresh_state.SCAN)['cursor'] == 1
    first_source = calls[-1]['source']
    chunk_ids = [row[0] for row in led.db.execute('SELECT artifact_id FROM artifacts WHERE kind=?', (patient_context_refresh_state.CHUNK,))]
    assert chunk_ids
    with led.db:
        for row in led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind IN (?,?,?)',
                                  (patient_context_refresh_state.KIND, patient_context_refresh_state.SCAN, patient_context_refresh_state.CHUNK)).fetchall():
            meta = json.loads(row['meta'])
            meta['night_id'] = '2026-10-17'
            led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    assert step(runtime)['lane'] == 'B'
    assert document(led, patient_context_refresh_state.SCAN)['cursor'] == 2
    assert calls[-1]['source'] != first_source
    assert all(led.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
               for artifact_id in chunk_ids)


@pytest.mark.parametrize('held', [False, True])
def test_legacy_workday_progress_preserves_retry_and_hold_across_midnight(runtime, monkeypatch, held):
    led, clock, _ = runtime
    seed(led)
    _prepare_summary(runtime)
    clock.wall = stamp('2026-10-17T23:59:30')
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', lambda *_a, **_k: {
        'status': 200, 'finish_reason': 'length', 'text': '{}'})
    assert step(runtime)['status'] == 'failed'
    row = led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? AND project_id=1',
                         (patient_context_refresh_state.PROGRESS,)).fetchone()
    progress, meta = json.loads(row['content']), json.loads(row['meta'])
    progress.update(night_id='2026-10-17', held=held)
    meta['night_id'] = '2026-10-17'
    with led.db:
        led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                       (json.dumps(progress), json.dumps(meta), row['artifact_id']))
    clock.wall = stamp('2026-10-18T00:00:00')
    _, saved, pending = patient_context_refresh_readiness._pending(led.db, 1, CFG, now=clock.wall)
    assert not pending['A'] and saved['held'] is held
    assert saved['attempts'] == progress['attempts'] and saved['next_try'] == progress['next_try']
    _, _, later = patient_context_refresh_readiness._pending(led.db, 1, CFG, now=progress['next_try'] + 1)
    assert later['A'] is (not held)


def test_clock_rollback_and_same_day_restart_do_not_repeat_completed_work(runtime):
    led, clock, calls = runtime
    seed(led)
    clock.wall = stamp('2026-10-18T01:00:00')
    finish(runtime)
    count = len(calls)
    before = document(led)
    clock.wall = stamp('2026-10-17T23:55:00')
    assert step(runtime)['status'] == 'complete'
    clock.wall = stamp('2026-10-18T01:05:00')
    assert step(runtime)['status'] == 'complete'
    assert len(calls) == count and document(led) == before


def test_source_change_during_inference_prevents_publish(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    original = context_refresh.local_llm.admitted_chat
    def race(*args, **kwargs):
        response = original(*args, **kwargs)
        led.db.execute("UPDATE messages SET body_text='編集された合成記録',content_hash='changed' WHERE message_id=1")
        led.db.commit()
        return response
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', race)
    assert step(runtime)['status'] == 'source_changed'
    assert document(led) is None and not led.db.in_transaction


@pytest.mark.parametrize('mutation', [
    "UPDATE messages SET content_hash='edited' WHERE message_id=1",
    'DELETE FROM messages WHERE message_id=1',
    'UPDATE patients SET karte_id=99 WHERE project_id=1',
    'UPDATE patients SET is_archived=1 WHERE project_id=1',
    'UPDATE messages SET project_id=2 WHERE message_id=1',
])
def test_changed_deleted_or_remapped_sources_hide_previous_summary(runtime, mutation):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    led.db.execute(mutation)
    led.db.commit()
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


def test_publication_off_keeps_generated_artifact_private(runtime):
    led, _, _ = runtime
    seed(led)
    shadow = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    finish(runtime, cfg=shadow)
    assert document(led)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=shadow) is None
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


def test_old_distinct_background_items_survive_newer_same_category(runtime):
    led, _, _ = runtime
    seed(led, body='生活環境: 合成の住居は階段のみ。')
    seed(led, 2, body='生活環境: 合成の家族が週末に訪問。')
    finish(runtime)
    facts = document(led)['facts']
    assert {f['message_id'] for f in facts} == {1, 2}


def test_full_source_quote_retains_family_negation_plan_and_date(runtime, monkeypatch):
    led, _, _ = runtime
    body = '2026年10月01日、家族は酸素を使用していない。本人への導入は検討中。'
    seed(led, body=body, semantic_quote='家族は酸素を使用していない。')
    original = context_refresh.local_llm.admitted_chat
    def partial_quote(*args, **kwargs):
        payload = json.loads(args[1])
        if 'source' not in payload:
            return original(*args, **kwargs)
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({
            'patient_context': [{'category': 'devices', 'text': '酸素を使用',
             'evidence': '酸素を使用', 'subject': 'patient', 'details': []}]})}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', partial_quote)
    finish(runtime)
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert view['facts'][0]['quote'] == body
    assert view['summary'][0]['text'] == body
    assert view['facts'][0]['subject'] == 'unspecified'


def test_partial_patient_and_ready_patient_both_proceed(runtime):
    led, _, _ = runtime
    seed(led, pid=1)
    seed(led, mid=2, pid=2)
    seed(led, mid=3, pid=1, body='', state='missing')
    finish(runtime, waiting=True)
    partial = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert partial and document(led, pid=2)
    assert {fact['message_id'] for fact in partial['facts']} == {1}
    assert partial['coverage']['unreflected'] == 1 and partial['coverage']['partial']
    proof = patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)
    assert not proof['complete'] and proof['sources']['incomplete'] == 1


def test_scoped_sources_use_actual_extraction_proof_instead_of_whole_patient_callback(runtime):
    led, _, calls = runtime
    seed(led, pid=1)
    seed(led, mid=2, pid=2)
    seed(led, mid=3, pid=1, proof=False)
    for _ in range(12):
        assert step(runtime, complete=lambda _pid: False)['status'] == 'progressed'
        if document(led, pid=1):
            break
    assert document(led, pid=1)
    assert sum(any(report['project_id'] == 1 for report in call.get('reports', []))
               for call in calls) == 1
    finish(runtime, waiting=True)
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert view and {fact['message_id'] for fact in view['facts']} == {1}
    assert view['coverage']['unreflected'] == 1 and document(led, pid=2)


def test_failure_backoff_does_not_starve_other_patient(runtime, monkeypatch):
    led, clock, calls = runtime
    seed(led, pid=1)
    seed(led, mid=2, pid=2)
    original = context_refresh.local_llm.admitted_chat
    def fail_summary(*args, **kwargs):
        payload = json.loads(args[1])
        if any(report['project_id'] == 1 for report in payload.get('reports', [])):
            return {'status': 200, 'finish_reason': 'length', 'text': '{}'}
        return original(*args, **kwargs)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', fail_summary)
    for _ in range(12):
        result = step(runtime)
        if result['status'] == 'failed':
            break
        assert result['status'] == 'progressed', result
    # the fixed check code is kept (and logged), not just the exception type
    assert result['status'] == 'failed' and result['lane'] == 'A' and result['reason'] == 'nightly_length_stop'
    progress = document(led, patient_context_refresh_state.PROGRESS)
    assert progress['next_try'] > clock.wall and progress['attempts'] == 1
    assert progress['failure'] == 'nightly_length_stop'
    assert patient_context_refresh_readiness.status(led.db, CFG)['failed'] == 1
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    for _ in range(6):
        assert step(runtime)['status'] == 'progressed'
        if document(led, pid=2):
            break
    assert document(led, pid=2)
    assert calls


def test_crossing_midnight_during_inference_commits_once_under_start_day(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    _prepare_summary(runtime)
    clock.wall = stamp('2026-10-17T23:59:30')
    original = context_refresh.local_llm.admitted_chat
    def cross_end(*args, **kwargs):
        response = original(*args, **kwargs)
        clock.wall = stamp('2026-10-18T00:00:01')
        clock.mono += 31
        return response
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', cross_end)
    assert step(runtime)['status'] == 'progressed'
    assert document(led)
    meta = led.db.execute('SELECT meta FROM artifacts WHERE kind=? AND project_id=1', (patient_context_refresh_state.KIND,)).fetchone()
    assert json.loads(meta[0])['night_id'] == '2026-10-17'
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    finish(runtime)
    calls = len(runtime[2])
    assert step(runtime)['status'] == 'complete'
    assert len(runtime[2]) == calls


@pytest.mark.parametrize('condition', ['source', 'deadline', 'admission'])
def test_midnight_does_not_bypass_inflight_safety_guards(runtime, monkeypatch, condition):
    led, clock, _ = runtime
    seed(led)
    clock.wall = stamp('2026-10-17T23:59:30')
    original = context_refresh.local_llm.admitted_chat
    allowed = [True]

    def crossing(*args, **kwargs):
        response = original(*args, **kwargs)
        clock.wall = stamp('2026-10-18T00:00:01')
        clock.mono += 31
        if condition == 'source':
            led.db.execute("UPDATE messages SET body_text='変更された合成本文', content_hash='changed' WHERE message_id=1")
            led.db.commit()
        elif condition == 'deadline':
            clock.mono += 600
        else:
            allowed[0] = False
        return response

    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', crossing)
    result = step(runtime, admit=lambda: allowed[0])
    assert result['status'] == ('source_changed' if condition == 'source' else 'deferred')
    assert document(led) is None and not led.db.in_transaction


def test_commit_lock_wait_can_cross_midnight_without_resending(runtime, monkeypatch):
    led, clock, calls = runtime
    seed(led)
    _prepare_summary(runtime)
    before_calls = len(calls)
    clock.wall = stamp('2026-10-17T23:59:59')
    tries = []

    def acquire():
        # Let pre-dispatch quota bookkeeping pass; contend only the result save.
        if len(calls) == before_calls or len(tries) >= 2:
            return os.open(os.devnull, os.O_RDONLY)
        tries.append(1)
        return None if len(tries) == 1 else os.open(os.devnull, os.O_RDONLY)

    def sleep(seconds):
        clock.wall += seconds
        clock.mono += seconds

    monkeypatch.setattr(patient_context_refresh_state, 'acquire_run_lock', acquire)
    monkeypatch.setattr(patient_context_refresh_state.time, 'sleep', sleep, raising=False)
    assert step(runtime)['status'] == 'progressed'
    assert len(tries) == 2 and len(calls) == before_calls + 1 and document(led)


def test_ordinary_refetch_timestamp_does_not_regenerate(runtime):
    led, _, calls = runtime
    seed(led)
    finish(runtime)
    count = len(calls)
    led.db.execute('UPDATE messages SET updated_seen=updated_seen+100 WHERE message_id=1')
    led.db.commit()
    assert step(runtime)['status'] == 'complete'
    assert len(calls) == count
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)


def test_old_unlabelled_report_is_discovered_without_new_message(runtime):
    led, _, _ = runtime
    body = '合成の家族が暮らしを手伝っていると報告。'
    seed(led, body=body)
    assert step(runtime)['lane'] == 'B'
    assert document(led) is None
    before_count = led.db.execute('SELECT count(*) FROM messages').fetchone()[0]
    finish(runtime)
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert any(f['quote'] == body and f['discovery'] == 'backfill_found' for f in view['facts'])
    assert led.db.execute('SELECT count(*) FROM messages').fetchone()[0] == before_count


def test_new_addition_is_unreflected_not_silently_part_of_old_summary(runtime):
    led, clock, _ = runtime
    seed(led)
    finish(runtime)
    stored = document(led)
    seed(led, mid=2, body='生活環境: 合成の新着報告。')
    led.db.execute('UPDATE messages SET first_seen=? WHERE message_id=2', (clock.wall,))
    led.db.commit()
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert view is not None and view['coverage']['unreflected'] == 1
    assert view['summary'] == stored['summary']
    assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['sources']['pending'] == 1
    assert document(led) == stored  # Additions do not become part of the frozen publication.


def test_model_timeout_is_checkpointed_so_other_patients_can_advance(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    seed(led, mid=2, pid=2)
    original = context_refresh.local_llm.admitted_chat
    def timeout(*args, **kwargs):
        payload = json.loads(args[1])
        if any(f['project_id'] == 1 for f in payload.get('reports', [])):
            raise TimeoutError('synthetic model timeout')
        return original(*args, **kwargs)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', timeout)
    for _ in range(12):
        result = step(runtime)
        if result['status'] == 'failed':
            break
        assert result['status'] == 'progressed', result
    assert result['status'] == 'failed' and result['lane'] == 'A'
    progress = document(led, patient_context_refresh_state.PROGRESS)
    assert progress is not None and progress['next_try'] > 0
    for _ in range(6):
        step(runtime)
        if document(led, pid=2):
            break
    assert document(led, pid=2), 'a timed-out patient must not monopolize every next attempt'


def test_changed_source_resets_exhausted_retries(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led)
    _prepare_summary(runtime)
    original = context_refresh.local_llm.admitted_chat
    def fail_summary(*args, **kwargs):
        if 'reports' in json.loads(args[1]):
            return {'status': 200, 'finish_reason': 'length', 'text': '{}'}
        return original(*args, **kwargs)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', fail_summary)
    for attempt in range(5):
        clock.wall = stamp('2026-10-17T23:00:00') + attempt * 86400
        assert step(runtime)['status'] == 'failed'
    assert document(led, patient_context_refresh_state.PROGRESS)['attempts'] == 5
    before = document(led, patient_context_refresh_state.PROGRESS)
    clock.wall = before['failure_at'] + 86400 - 1
    assert step(runtime)['status'] == 'deferred'
    assert document(led, patient_context_refresh_state.PROGRESS)['attempts'] == 5
    retry_at = before['failure_at'] + 86400
    clock.wall = retry_at
    assert step(runtime)['status'] == 'failed'
    after = document(led, patient_context_refresh_state.PROGRESS)
    assert after['attempts'] == 6 and after['next_try'] == retry_at + 86400
    assert after['failure_at'] == after['last_attempt'] == retry_at
    assert step(runtime)['status'] == 'deferred'
    # New semantic checks still start a fresh retry generation; the same input gets daily retries.
    monkeypatch.setattr(patient_context_refresh_state, '_CHECKS', 'synthetic-changed-checks')
    clock.wall += 86400
    # Stored scans reopen before lane A gets its first attempt under the new checks.
    results = [step(runtime) for _ in range(3)]
    assert [r['status'] for r in results if r.get('lane') == 'A'] == ['failed']
    assert document(led, patient_context_refresh_state.PROGRESS)['attempts'] == 1
    clock.wall += 86400
    results = [step(runtime) for _ in range(3)]
    assert [r['status'] for r in results if r.get('lane') == 'A'] == ['failed']
    assert document(led, patient_context_refresh_state.PROGRESS)['attempts'] == 2
    seed(led, body='生活環境: 更新された合成の背景。')
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    _prepare_summary(runtime)
    clock.wall += 3 * 60 * 60
    clock.mono += 3 * 60 * 60
    assert step(runtime)['status'] == 'progressed'
    assert document(led, patient_context_refresh_state.PROGRESS)['attempts'] == 0
    finish(runtime)
    assert document(led)
    assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


def test_revoked_source_is_not_generated_or_displayed(runtime):
    led, _, calls = runtime
    seed(led)
    step(runtime)
    led.db.execute("UPDATE patients SET fetch_reason='permission_denied' WHERE project_id=1")
    led.db.commit()
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    count = len(calls)
    step(runtime)
    assert len(calls) == count


def test_view_reads_only_quoted_bodies_without_reextracting_history(runtime, monkeypatch):
    import patient_context
    led, _, calls = runtime
    seed(led)
    for mid in range(2, 12):
        seed(led, mid=mid, body=f'合成の未分類記録その{mid}。')
    assert step(runtime)['status'] == 'progressed'
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    finish(runtime)
    before = len(calls)
    monkeypatch.setattr(mcs_queries, 'source_patient_context',
                        lambda *a: pytest.fail('view re-extracted saved history'))
    monkeypatch.setattr(patient_context, 'extract_context',
                        lambda *a: pytest.fail('view parsed saved history'))
    queries = []
    led.db.set_trace_callback(queries.append)
    try:
        view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    finally:
        led.db.set_trace_callback(None)
    assert view is not None and {f['message_id'] for f in view['facts']} == set(range(1, 12))
    assert len(calls) == before
    body_reads = [q for q in queries if q.startswith('SELECT body_text FROM messages')]
    assert len(body_reads) == 11  # Every displayed source is validated once; no history re-extraction.
    assert not any('SELECT m.*' in q for q in queries)
    extraction_reads = [q for q in queries if 'FROM artifacts a JOIN messages m' in q]
    assert len(extraction_reads) == 1, 'metadata aggregation must not query each source separately'


def test_marked_extract_keeps_exact_proof_check(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    seed(led, mid=2, body='合成の未分類記録その2。')
    finish(runtime)
    marked_id = led.db.execute("SELECT artifact_id FROM artifacts WHERE kind='extract_llm' "
                               "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()[0]
    with led.db:
        led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.internal_proof_version',1,"
                       "'$.internal_proof_hash',?) WHERE artifact_id=?", ('9' * 64, marked_id))
    calls_to = _proof_spy(monkeypatch)
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    # Only the marked row defers to the exact witness check; its invalid
    # proof drops the summary and the stored signature no longer matches.
    assert calls_to == [marked_id]
    assert view is None


def _proof_spy(monkeypatch):
    calls_to = []
    real = mcs_queries.selected_artifact_proof_valid
    def spy(db, artifact_id, **kw):
        calls_to.append(artifact_id)
        return real(db, artifact_id, **kw)
    monkeypatch.setattr(mcs_queries, 'selected_artifact_proof_valid', spy)
    return calls_to


def test_source_generator_drops_stale_summary_after_consumer_write(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    seed(led, mid=2, body='合成の未分類記録その2。')
    calls_to = _proof_spy(monkeypatch)
    led.db.execute('BEGIN')
    gen = mcs_queries.patient_context_sources(led.db, 1, hydrate=False)
    try:
        first = next(gen)
        assert led.db.in_transaction
        # Unchanged: the aggregate's newer-marked exclusion stood in, so no
        # per-artifact proof query ran and the summary was adopted.
        assert first['message_id'] == 1 and first['derived_summary_chars'] > 0
        assert calls_to == []
        # A same-connection write between yields must not let the second
        # source reuse the exclusion: the new marker sends its row back
        # through the exact witness check, which rejects it.
        led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.internal_proof_version',1,"
                       "'$.internal_proof_hash',?) WHERE kind='extract_llm' AND message_id=2",
                       ('9' * 64,))
        second = next(gen)
        assert second['message_id'] == 2 and second['derived_summary_chars'] == 0
        assert calls_to == [led.db.execute("SELECT artifact_id FROM artifacts WHERE "
                                           "kind='extract_llm' AND message_id=2").fetchone()[0]]
    finally:
        gen.close()
        led.db.rollback()


def test_source_generator_rechecks_after_rollback(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    seed(led, mid=2, body='合成の未分類記録その2。')
    calls_to = _proof_spy(monkeypatch)
    led.db.execute('BEGIN')
    gen = mcs_queries.patient_context_sources(led.db, 1, hydrate=False)
    try:
        first = next(gen)
        assert led.db.in_transaction and first['derived_summary_chars'] > 0
        assert calls_to == []
        led.db.rollback()
        # The rolled-back baseline cannot stand in: the next row goes
        # through the exact per-artifact check and keeps its summary.
        second = next(gen)
        assert second['derived_summary_chars'] > 0 and calls_to
    finally:
        gen.close()
        led.db.rollback()


def test_source_generator_rechecks_when_stamp_read_fails(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    calls_to = _proof_spy(monkeypatch)
    def fail_stamp(db):
        raise sqlite3.OperationalError('synthetic stamp failure')
    monkeypatch.setattr(mcs_queries, '_message_read_stamp', fail_stamp)
    led.db.execute('BEGIN')
    gen = mcs_queries.patient_context_sources(led.db, 1, hydrate=False)
    try:
        # A failed coherence read is never treated as fresh: every row
        # defers to the exact per-artifact check and keeps its summary.
        first = next(gen)
        assert first['derived_summary_chars'] > 0 and calls_to
    finally:
        gen.close()
        led.db.rollback()


def test_source_generator_propagates_control_exceptions(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    calls_to = _proof_spy(monkeypatch)
    sentinel = KeyboardInterrupt('synthetic cancel')
    def cancel(db):
        raise sentinel
    monkeypatch.setattr(mcs_queries, '_message_read_stamp', cancel)
    led.db.execute('BEGIN')
    gen = mcs_queries.patient_context_sources(led.db, 1)
    try:
        with pytest.raises(KeyboardInterrupt) as exc:
            next(gen)
        assert exc.value is sentinel and calls_to == []
    finally:
        gen.close()
        led.db.rollback()


def test_shadow_can_be_published_without_repeating_inference(runtime):
    led, _, calls = runtime
    seed(led)
    shadow = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    finish(runtime, cfg=shadow)
    generated = document(led)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    before = len(calls)
    assert step(runtime)['lane'] == 'A'
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert view and view['summary'] == generated['summary']
    assert view['generated_at'] == generated['generated_at']
    assert len(calls) == before
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=shadow) is None


@pytest.mark.parametrize('summary', [None, 'invalid', {}, [None], ['invalid'],
    [{'fact_ids': None}], [{'fact_ids': 'invalid'}], [{'fact_ids': [{}]}],
    [{'fact_ids': []}], [{'fact_ids': ['unknown'], 'text': 'invented'}]])
def test_corrupt_summary_shape_is_hidden_without_exception(runtime, summary):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    value = document(led)
    value['summary'] = summary
    led.db.execute('UPDATE artifacts SET content=? WHERE kind=? AND project_id=1',
                   (json.dumps(value), patient_context_refresh_state.KIND))
    led.db.commit()
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


def test_quote_keeps_preceding_family_plan_and_negation_paragraph(runtime, monkeypatch):
    led, _, _ = runtime
    paragraph = '家族についての過去の相談。実施の確認はない。\n支援が必要なら利用を検討。\n合成サービスを利用。'
    body = '合成の別段落。\n\n' + paragraph + '\n\n合成の後続段落。'
    seed(led, body=body)
    # Use a known stored body so HTML normalization is not the subject of this regression.
    led.db.execute('UPDATE messages SET body_text=? WHERE message_id=1', (body,))
    led.db.commit()
    original = context_refresh.local_llm.admitted_chat
    def excerpt(*args, **kwargs):
        if 'source' not in json.loads(args[1]):
            return original(*args, **kwargs)
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({
            'patient_context': [{'category': 'care_services', 'text': '合成サービスを利用。',
                'evidence': '合成サービスを利用。', 'subject': 'patient', 'details': []}]})}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', excerpt)
    finish(runtime)
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert view['summary'][0]['text'] == paragraph
    assert view['facts'][0]['quote'] == paragraph


def test_legacy_null_body_state_is_analyzed_as_full(runtime):
    led, _, calls = runtime
    seed(led)
    led.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=1')
    led.db.commit()
    finish(runtime)
    coverage = document(led)['coverage']
    assert coverage['fetched'] == 1 and coverage['incomplete'] == 0
    assert coverage['analyzed'] == 1 and calls


@pytest.mark.parametrize('state', ['unknown', 'missing', 'snippet'])
def test_incomplete_body_states_are_not_counted_as_analyzed(runtime, state):
    led, _, calls = runtime
    seed(led)
    with led.db:
        led.db.execute('UPDATE messages SET body_state=? WHERE message_id=1', (state,))
    finish(runtime, waiting=True)
    assert document(led) is None and not calls
    proof = patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)
    assert not proof['complete'] and proof['sources']['incomplete'] == 1


def test_summary_unknown_output_flag_prevents_publication(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led)
    original = context_refresh.local_llm.admitted_chat
    def unknown_flag(*args, **kwargs):
        result = original(*args, **kwargs)
        value = json.loads(result['text'])
        value['approved'] = True
        return {**result, 'text': json.dumps(value)}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', unknown_flag)
    assert step(runtime)['status'] == 'failed'
    assert document(led) is None
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


@pytest.mark.parametrize('malformation', ['extra_response_key', 'extra_item_key', 'unsupported_detail'])
def test_scan_invalid_output_never_publishes_partial_findings(runtime, monkeypatch, malformation):
    led, _, _ = runtime
    body = '合成の家族が生活を手伝うとの記録。'
    seed(led, body=body)
    before = document(led)
    def invalid(*args, **kwargs):
        item = {'category': 'living', 'text': body, 'evidence': body,
                'subject': 'unspecified', 'details': []}
        answer = {'patient_context': [item]}
        if malformation == 'extra_response_key':
            answer['approved'] = True
        elif malformation == 'extra_item_key':
            item['clinically_confirmed'] = True
        else:
            item['details'] = [{'key': 'supporter', 'value': '存在しない合成値',
                                'evidence': '原文に存在しない合成引用'}]
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answer)}
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', invalid)
    assert step(runtime)['status'] == 'failed'
    failed = document(led, patient_context_refresh_state.SCAN)
    assert failed['attempts'] == 1 and not failed.get('complete') and not failed.get('facts')
    assert document(led) == before and before is None


def test_failed_source_does_not_block_other_history_in_same_patient(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led, mid=1, body='合成の解析できない記録。')
    seed(led, mid=2, body='合成の後続の支援記録。')
    original = context_refresh.local_llm.admitted_chat
    def poison(*args, **kwargs):
        if json.loads(args[1]).get('source') == '合成の解析できない記録。':
            return {'status': 200, 'finish_reason': 'length', 'text': '{}'}
        return original(*args, **kwargs)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', poison)
    assert step(runtime)['status'] == 'failed'
    assert step(runtime)['status'] == 'progressed'
    for _ in range(5):
        step(runtime)
    assert any(f['message_id'] == 2 for f in document(led)['facts'])
    counts = patient_context_refresh_readiness.status(led.db, CFG)
    assert counts['failed'] == counts['history_pending'] == 1 and counts['complete'] == 0
    row = led.db.execute('SELECT content FROM artifacts WHERE kind=? AND message_id=1',
                         (patient_context_refresh_state.SCAN,)).fetchone()
    assert json.loads(row[0])['next_try'] > clock.wall


def test_family_medication_detail_survives_source_revalidation(runtime):
    led, _, _ = runtime
    seed(led, body='母の薬は3錠残っているとの合成記録。',
         semantic_quote='残っているとの合成記録。')
    source = next(mcs_queries.patient_context_sources(led.db, 1))
    body = source['body_text']
    items = [{'category': 'medication_management', 'subject': 'family',
              'text': body, 'evidence': body,
              'details': [{'key': 'residual_quantity', 'value': '3錠', 'evidence': body}]}]
    facts = context_plan.source_facts(source, items, night_id='2026-10-17', discovery='backfill_found', found_at=patient_context_refresh_state.time.time())
    assert facts[0]['details'] == items[0]['details']
    assert facts[0]['subject'] == 'unspecified' and facts[0]['reported_subject'] == 'family'
    assert context_plan.validated_source_facts(source, facts) == facts


def test_new_backfill_evidence_rearms_exhausted_summary(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led, body='生活環境: 合成の既知の記録。')
    _prepare_summary(runtime)
    original = context_refresh.local_llm.admitted_chat
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', lambda *a, **k: {
        'status': 200, 'finish_reason': 'length', 'text': '{}'})
    for attempt in range(5):
        clock.wall = stamp('2026-10-17T23:00:00') + attempt * 86400
        assert step(runtime)['status'] == 'failed'
    # Reobserving the same known source adds no input and cannot reset its retry hold.
    with led.db:
        led.db.execute('UPDATE messages SET updated_seen=updated_seen+100 WHERE message_id=1')
    assert step(runtime)['status'] == 'deferred'
    assert document(led, patient_context_refresh_state.PROGRESS)['attempts'] == 5
    seed(led, mid=2, body='合成の新たに点検する支援記録。')
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', original)
    assert step(runtime)['lane'] == 'B'
    assert document(led, patient_context_refresh_state.PROGRESS)['attempts'] == 0  # A new source contributes new input.
    clock.wall += 3 * 60 * 60
    clock.mono += 3 * 60 * 60
    finish(runtime)
    assert {f['message_id'] for f in document(led)['facts']} == {1, 2}


def test_summary_budget_preserves_complete_source_reports_in_detail(runtime, monkeypatch):
    led, _, _ = runtime
    monkeypatch.setattr(context_refresh.context_plan, 'MAX_FACTS', 2)
    for mid in range(1, 5):
        seed(led, mid=mid, body=f'生活環境: 合成の独立した過去報告その{mid}。')
    finish(runtime)
    stored = document(led)
    selected = {f['message_id'] for f in stored['facts']}
    assert len(stored['facts']) == 2 and 1 <= len(selected) <= 2 and len(stored['summary']) <= 2
    assert len({fact['id'] for fact in stored['facts']}) == 2  # The cap counts atomic assertions, not posts.
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert {f['message_id'] for f in view['facts']} == {1, 2, 3, 4}
    assert view['summary'] == stored['summary']
    omitted = min({1, 2, 3, 4} - selected)
    row = led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
                         'WHERE kind=? AND message_id=?', (patient_context_refresh_state.SCAN, omitted)).fetchone()
    content, meta = json.loads(row['content']), json.loads(row['meta'])
    for invalid_content, invalid_meta in [({**content, 'complete': False}, meta),
                                         (content, {**meta, 'source_sig': 'old-revision'})]:
        led.db.execute('UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?',
                       (json.dumps(invalid_content), json.dumps(invalid_meta), row['artifact_id']))
        led.db.commit()
        current = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
        assert current is None
        assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']
        assert document(led)['summary'] == stored['summary']


def test_linked_summary_checkpoint_has_no_fake_message_reference(runtime):
    led, _, calls = runtime
    seed(led)
    seed(led, mid=2, pid=2)
    led.db.execute("UPDATE patients SET karte_id=10 WHERE project_id=1")
    led.db.commit()
    led.karte_summary_store(1, 10, {
        "comment": "合成の連携記録には居住環境が記されています。",
        "updated_at": "2026-10-16T10:00:00+09:00"})
    finish(runtime)
    row = led.db.execute("SELECT message_id,content FROM artifacts WHERE kind=? "
                         "AND project_id=1 AND message_id IS NULL", (patient_context_refresh_state.SCAN,)).fetchone()
    assert row is not None
    assert json.loads(row[1])["complete"] and json.loads(row[1])["audited"]
    assert led.db.execute("SELECT SUM(shadow_count) FROM ledger_relation_guards").fetchone()[0] == 0
    view = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert any(f["source"] == "karte_summary" for f in view["facts"])
    assert patient_context_refresh_readiness.context_view_input(led.db, 2, cfg=CFG)
    count = len(calls)
    finish(runtime)
    assert len(calls) == count


def test_revalidation_rejects_changed_posted_date_text(runtime):
    led, _, _ = runtime
    seed(led)
    finish(runtime)
    source = next(mcs_queries.patient_context_sources(led.db, 1))
    facts = document(led)["facts"]
    facts[0]["posted_at"] = "2099-01-01T00:00:00+09:00"
    assert context_plan.validated_source_facts(source, facts) == facts[1:]


def test_fully_quoted_lines_still_scan_and_keep_distinct_summary_assertions(runtime):
    led, _, calls = runtime
    seed(led, body="生活環境: 合成の住宅。\nADL: 合成の歩行記録。")
    finish(runtime)
    assert sum('source' in call for call in calls) == 1
    summary = document(led)["summary"]
    assert len(summary) >= 2
    assert len({s['fact_ids'][0] for s in summary}) == len(summary)
    assert len(document(led)["facts"]) >= 2
    count = len(calls)
    finish(runtime)
    assert len(calls) == count


@pytest.mark.parametrize("source_changed", [False, True])
def test_other_connection_commit_is_rechecked_without_repeating_inference(runtime, monkeypatch, source_changed):
    import sqlite3
    led, _, calls = runtime
    seed(led)
    _prepare_summary(runtime)
    before_calls = len(calls)
    other = sqlite3.connect(led.db.execute("PRAGMA database_list").fetchone()[2])
    original = patient_context_refresh_sources._snapshot
    raced = False
    def snapshot(*args, **kwargs):
        nonlocal raced
        result = original(*args, **kwargs)
        if len(calls) > before_calls and kwargs.get("hydrate") is False and not raced:
            raced = True
            if source_changed:
                other.execute("UPDATE messages SET content_hash='edited-concurrently' WHERE message_id=1")
            else:
                other.execute("UPDATE patients SET patient_name='synthetic-renamed' WHERE project_id=1")
            other.commit()
        return result
    monkeypatch.setattr(patient_context_refresh_sources, "_snapshot", snapshot)
    try:
        result = step(runtime)
        assert result["status"] == ("source_changed" if source_changed else "progressed")
        assert (document(led) is not None) is (not source_changed)
        assert len(calls) == before_calls + 1
    finally:
        other.close()


def test_source_checkpoint_reads_are_batched(runtime):
    led, _, _ = runtime
    for mid in range(1, 25):
        seed(led, mid=mid)
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    statements = []
    led.db.set_trace_callback(statements.append)
    try:
        facts, total = patient_context_refresh_sources._all_facts(led.db, 1, snapshot, "2026-10-17")
        coverage = patient_context_refresh_sources._coverage(led.db, 1, snapshot["sources"], snapshot["contract"], facts)
    finally:
        led.db.set_trace_callback(None)
    assert total == 24 and coverage["fetched"] == 24
    # Checkpoints, chunk proofs and current raw-source attestation are batched
    # for all 24 sources (two additional SELECTs, never per-fact lookups).
    assert sum(s.startswith("SELECT") for s in statements) <= 8


@pytest.mark.parametrize("body", [" \t\n", "&nbsp;<br>", "\u3000\u00a0\n"])
def test_whitespace_ingestion_does_not_leave_a_permanent_scan_candidate(runtime, body):
    led, clock, _ = runtime
    seed(led, body=body)
    seed(led, mid=2, pid=2)
    # A fetched full post normalized to empty text has no unknown evidence or
    # attachment. It must finish without starting an empty source scan.
    finish(runtime)
    assert led.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == ""
    assert patient_context_refresh_readiness.status(led.db, CFG)["history_pending"] == 0
    assert patient_context_refresh_readiness._inference_ready(led, 1, CFG, deadline=clock.mono + 60)
    assert not any(patient_context_refresh_readiness._pending(led.db, 1, CFG, now=clock.wall)[2].values())
    assert patient_context_refresh_readiness.context_view_input(led.db, 2, cfg=CFG)


def test_history_scan_hydrates_only_the_selected_post(runtime, monkeypatch):
    led, _, _ = runtime
    for mid in range(1, 9):
        seed(led, mid=mid)
    original = mcs_queries.source_patient_context
    hydrated = []
    def read(db, message, **kwargs):
        hydrated.append(message["message_id"])
        return original(db, message, **kwargs)
    monkeypatch.setattr(mcs_queries, "source_patient_context", read)
    assert step(runtime)["lane"] == "B"
    assert len(hydrated) == 1


def test_unbrokered_context_refresh_request_sends_pinned_slot(monkeypatch):
    sent = []
    def request(endpoint, method, body, timeout, deadline):
        sent.append(body)
        return 200, {}, json.dumps({"choices": [{"message": {"content": "{}"},
                                                "finish_reason": "stop"}]}).encode()
    monkeypatch.setattr(context_refresh.local_llm, "bounded_request", request)
    monkeypatch.setattr(context_refresh.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(context_refresh.local_llm, "resolve", lambda cfg: ("http://127.0.0.1:8080", "synthetic"))
    with context_refresh.local_llm.pinned_slot(2):
        assert patient_context_quality_controller._ask({}, {}, patient_context_refresh_state.time.monotonic() + 600, lambda: True) == {}
    assert len(sent) == 1
    assert sent[0]["id_slot"] == 2


@pytest.mark.parametrize('reason', ['forbidden', 'permission_denied', 'access_revoked'])
def test_status_counts_only_patients_the_worker_would_work_on(runtime, reason):
    # an explicitly refused patient is out of scope for both; another fetch failure stays in scope
    led, _, calls = runtime
    seed(led, mid=1, pid=1)
    seed(led, mid=2, pid=2)
    seed(led, mid=3, pid=3)
    led.db.execute('UPDATE patients SET fetch_reason=? WHERE project_id=2', (reason,))
    led.db.execute("UPDATE patients SET fetch_reason='timeout' WHERE project_id=3")
    led.db.commit()
    assert patient_context_refresh_readiness.status(led.db, CFG)['initial_pending'] == 2
    finish(runtime, waiting=True)
    assert calls
    assert {r[0] for r in led.db.execute('SELECT DISTINCT project_id FROM artifacts WHERE kind=?',
                                       (patient_context_refresh_state.SCAN,))} == {1, 3}
    assert document(led, pid=2) is None and document(led, pid=3)
    assert patient_context_refresh_readiness.context_view_input(led.db, 3, cfg=CFG)['coverage']['partial']
    counts = patient_context_refresh_readiness.status(led.db, CFG)
    assert counts['initial_pending'] == 0 and counts['complete'] == 1
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 3, CFG)['complete']


def test_turn_wait_expiry_defers_without_charging_the_patient(monkeypatch):
    def busy(prompt, error_out=None, **kw):
        if error_out is not None:
            error_out["kind"] = "busy"
        return None
    monkeypatch.setattr(context_refresh.local_llm, "chat", busy)
    monkeypatch.setattr(context_refresh.local_llm, "admission_enabled", lambda: False)
    monkeypatch.setattr(context_refresh.local_llm, "resolve", lambda cfg: ("http://127.0.0.1:8080", "synthetic"))
    with pytest.raises(patient_context_refresh_state.Deferred, match="nightly_turn_busy"):
        patient_context_quality_controller._ask({}, {}, patient_context_refresh_state.time.monotonic() + 600, lambda: True)


def test_finished_inference_waits_briefly_for_the_run_lock(runtime, monkeypatch):
    led, clock, calls = runtime
    seed(led)
    _prepare_summary(runtime)
    before_calls = len(calls)
    tries = []
    def contended():
        if len(calls) == before_calls or len(tries) >= 4:
            return os.open(os.devnull, os.O_RDONLY)
        tries.append(1)
        return os.open(os.devnull, os.O_RDONLY) if len(tries) > 3 else None
    def sleep(seconds):
        clock.mono += seconds
        clock.wall += seconds
    monkeypatch.setattr(patient_context_refresh_state, 'acquire_run_lock', contended)
    monkeypatch.setattr(patient_context_refresh_state.time, 'sleep', sleep, raising=False)
    assert step(runtime)['status'] == 'progressed'
    assert len(calls) == before_calls + 1 and len(tries) == 4 and document(led)
    # a holder that never yields costs at most the short cap, not the unit deadline
    seed(led, mid=2, pid=2)
    monkeypatch.setattr(patient_context_refresh_state, 'acquire_run_lock', lambda: None)
    before = clock.mono
    assert step(runtime)['status'] == 'deferred'
    assert clock.mono - before <= 61 and document(led, pid=2) is None


PROSE = '本日訪問しました。合成の記録として、ご本人は落ち着いて過ごされていました。' * 60


def _scan_chat(monkeypatch, reply):
    """Stub only scan calls; summaries keep the fixture's grounded answer."""
    original = context_refresh.local_llm.admitted_chat
    scans = []

    def chat(owner, text, **kwargs):
        payload = json.loads(text)
        if 'reports' in payload:
            return original(owner, text, **kwargs)
        scans.append(payload)
        return reply(payload)
    monkeypatch.setattr(context_refresh.local_llm, 'admitted_chat', chat)
    return scans


def _quote(body):
    return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
        {'category': 'living', 'text': body, 'evidence': body, 'subject': 'unspecified', 'details': []}]})}


def test_a_length_stopped_scan_is_replanned_finer_instead_of_retried_unchanged(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body=PROSE)
    scans = _scan_chat(monkeypatch, lambda p: _quote(p['source']) if len(p['source']) <= 1500
                       else {'status': 200, 'finish_reason': 'length', 'text': '{'})
    finish(runtime)                                  # every unit progresses; nothing is charged
    scan = document(led, patient_context_refresh_state.SCAN)
    assert scan['complete'] and scan['split'] == 1 and scan.get('attempts', 0) == 0
    assert len(scans[0]['source']) > 1500 and all(len(p['source']) <= 1500 for p in scans[1:])
    assert ''.join(p['source'] for p in scans[1:]) == PROSE   # the finer plan covers every char


def test_the_finest_plan_still_stopping_is_an_ordinary_failure(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body=PROSE)
    _scan_chat(monkeypatch, lambda p: {'status': 200, 'finish_reason': 'length', 'text': '{'})
    for _ in range(12):
        result = step(runtime)
        if result['status'] == 'failed':
            break
    assert result['reason'] == 'nightly_length_stop'
    scan = document(led, patient_context_refresh_state.SCAN)
    assert scan['attempts'] == 1 and 1 <= scan.get('split', 0) <= context_refresh.context_plan.MAX_SCAN_SPLIT
    assert not scan.get('complete')
    lanes = patient_context_refresh_readiness.status(led.db, CFG)['lanes']
    assert lanes['B'] == {'length_stop': 1, 'unsupported': 0, 'other_failure': 0, 'split': 1, 'repair': 0}
    assert lanes['A'] == lanes['C'] == dict.fromkeys(lanes['B'], 0)


def test_unsupported_items_get_one_repair_with_reasons_but_no_text(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body='合成の訪問で、ご本人は落ち着いていました。')     # unlabelled: needs inference
    def reply(p):
        if 'rejected' in p:
            return _quote(p['source'])
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
            {'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
             'subject': 'unspecified', 'details': []}]})}
    scans = _scan_chat(monkeypatch, reply)
    finish(runtime)
    assert [p.get('rejected') for p in scans] == [None, [{'index': 0, 'reason': 'not_verbatim_or_invalid'}]]
    assert document(led, patient_context_refresh_state.SCAN)['complete']
    assert document(led, patient_context_refresh_state.SCAN)['repairs'] == 1
    assert patient_context_refresh_readiness.lane_metrics(led.db)['lanes']['B']['repair'] == 1


def test_a_failed_repair_stays_an_unsupported_failure(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body='合成の訪問で、ご本人は落ち着いていました。')
    scans = _scan_chat(monkeypatch, lambda p: {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(
        {'patient_context': [{'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
                              'subject': 'unspecified', 'details': []}]})})
    for _ in range(8):
        result = step(runtime)
        if result['status'] == 'failed':
            break
    assert result['reason'] == 'nightly_unsupported_items'
    assert len(scans) == 2                           # one answer, one bounded repair
    lanes = patient_context_refresh_readiness.lane_metrics(led.db)['lanes']
    assert lanes['B']['unsupported'] == 1 and lanes['B']['repair'] == 0


def test_a_repair_that_stops_at_the_budget_is_replanned_too(runtime, monkeypatch):
    led, _, _ = runtime
    seed(led, body=PROSE)
    def reply(p):
        if len(p['source']) <= 1500:
            return _quote(p['source'])
        if 'rejected' in p:
            return {'status': 200, 'finish_reason': 'length', 'text': '{'}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
            {'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
             'subject': 'unspecified', 'details': []}]})}
    scans = _scan_chat(monkeypatch, reply)
    finish(runtime)
    scan = document(led, patient_context_refresh_state.SCAN)
    assert scan['complete'] and scan['split'] == 1 and scan.get('attempts', 0) == 0
    assert 'rejected' in scans[1] and len(scans[2]['source']) <= 1500


@pytest.mark.parametrize('repaired, failed', [(False, True), (True, False)])
def test_a_repair_must_keep_what_the_first_answer_grounded(runtime, monkeypatch, repaired, failed):
    led, _, _ = runtime
    first = '合成の訪問で、ご本人は落ち着いていました。'
    seed(led, body=first + '合成の家族は同居しています。')
    grounded = {'category': 'living', 'text': first, 'evidence': first, 'subject': 'unspecified', 'details': []}
    def reply(p):
        if 'rejected' in p:      # an empty repair drops the grounded item; a full one keeps it
            return {'status': 200, 'finish_reason': 'stop',
                    'text': json.dumps({'patient_context': [grounded] if repaired else []})}
        return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'patient_context': [
            grounded, {'category': 'living', 'text': '合成の言い換え', 'evidence': '合成の言い換え',
                       'subject': 'unspecified', 'details': []}]})}
    _scan_chat(monkeypatch, reply)
    results = [step(runtime) for _ in range(6)]
    reasons = [r.get('reason') for r in results if r['status'] == 'failed']
    scan = document(led, patient_context_refresh_state.SCAN)
    if failed:   # the chunk is not completed by dropping what was grounded
        assert reasons[:1] == ['nightly_unsupported_items'] and not scan.get('complete')
    else:
        assert not reasons and scan['complete'] and len(scan['facts']) == 1


def test_health_counts_context_progress_without_patients(runtime, monkeypatch):
    import extract_llm
    import health_report
    led, _, _ = runtime
    seed(led, body='生活環境: 合成の居住記録。')
    seed(led, mid=2, pid=2, body='生活環境: 合成の別記録。')
    finish(runtime)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    out = health_report.patient_context_health(led.db, CFG, 10.0, 'synthetic-source')
    assert out['status'] == 'recorded' and out['publication'] is True
    assert out['published'] == 2 and out['active_patients'] == 2
    complete = sum(extract_llm.patient_extraction_complete(led, pid) for pid in (1, 2))
    assert out['extraction_complete'] == complete
    assert out['focus_targets'] == min(out['focus_eligible'], extract_llm._FOCUS_PATIENTS)
    assert 'synthetic-1' not in json.dumps(out) and set(out['lanes']) == {'A', 'B', 'C'}
    # a recorded block is reused within the period; a stale or other-db one is recounted
    monkeypatch.setattr(health_report, 'prev_health', lambda: {'patient_context': {**out, 'published': 9}})
    assert health_report.patient_context_health(led.db, CFG, 20.0, 'synthetic-source')['published'] == 9
    assert health_report.patient_context_health(led.db, CFG, 20.0, 'other')['published'] == 2
    # publication always follows the current config, even on a reused block
    private = {'extract': {'patient_context_nightly': {'enabled': True, 'publication': False}}}
    reused = health_report.patient_context_health(led.db, private, 20.0, 'synthetic-source')
    assert reused['published'] == 9 and reused['publication'] is False
    assert health_report.patient_context_health(led.db, None, 20.0, 'synthetic-source')['publication'] is None
    # a new extraction generation recounts instead of reusing the old counts
    version = extract_llm.EXTRACT_VERSION
    monkeypatch.setattr(extract_llm, 'EXTRACT_VERSION', version + 1)
    monkeypatch.setattr(extract_llm_context, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_errors, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_prepare, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_progress, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_proofs, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_provider, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_qc, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_resident, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_selection, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_store, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    assert health_report.patient_context_health(led.db, CFG, 20.0, 'synthetic-source')['published'] == 2
    monkeypatch.setattr(extract_llm, 'EXTRACT_VERSION', version)
    monkeypatch.setattr(extract_llm_context, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_errors, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_prepare, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_progress, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_proofs, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_provider, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_qc, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_resident, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_selection, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(extract_llm_store, 'EXTRACT_VERSION', extract_llm.EXTRACT_VERSION)
    monkeypatch.setattr(health_report, 'prev_health', lambda: {})
    monkeypatch.setattr(health_report, 'PATIENT_CONTEXT_S', 0)
    assert health_report.patient_context_health(led.db, None, 30.0, 'synthetic-source') == {
        'status': 'unknown', 'observed_at': 30.0, 'reason': 'observation_limit', 'publication': None}


@pytest.mark.parametrize('at', [
    '2026-10-17T00:00:00', '2026-10-17T12:00:00', '2026-10-17T23:59:59',
])
def test_initial_context_unit_can_start_at_any_hour(runtime, at):
    led, clock, calls = runtime
    seed(led)
    clock.wall = stamp('2026-10-16T23:00:00')
    _prepare_summary(runtime)
    before_calls = len(calls)
    clock.wall = stamp(at)
    result = step(runtime)
    assert result['status'] == 'progressed' and result['lane'] == 'A'
    assert len(calls) == before_calls + 1 and document(led) is not None
    progress = document(led, patient_context_refresh_state.PROGRESS)
    assert progress['night_id'] == '2026-10-17'


@pytest.mark.parametrize('field', ['held', '_quality_held'])
def test_daily_retry_never_releases_protected_holds(field):
    progress = {'attempts': 5, 'checks': patient_context_refresh_state._CHECKS,
                'failure': 'nightly_timeout', 'next_try': 1000, 'failure_at': 1000, field: True}
    assert not patient_context_refresh_readiness._ready(progress, 1000 + 10 * 86400)


@pytest.mark.parametrize('attempts', [5, 6, 100])
def test_daily_retry_legacy_schedule_and_new_attempt_boundary(attempts):
    progress = {'attempts': attempts, 'checks': patient_context_refresh_state._CHECKS,
                'failure': 'nightly_timeout', 'next_try': 1000}
    assert not patient_context_refresh_readiness._ready(progress, 1000 + 86400 - 1)
    assert patient_context_refresh_readiness._ready(progress, 1000 + 86400)
    progress.update(last_attempt=500, next_try=500 + 86400)
    assert not patient_context_refresh_readiness._ready(progress, 500 + 86400 - 1)
    assert patient_context_refresh_readiness._ready(progress, 500 + 86400)


@pytest.mark.parametrize('field,value', [('attempts', True), ('next_try', float('nan')),
    ('next_try', float('inf')), ('next_try', True), ('failure_at', float('inf')),
    ('failure_at', True), ('failure_at', '1000'), ('failure_at', 10**1000)])
def test_daily_retry_keeps_unverified_dates_and_counters_waiting(field, value):
    progress = {'attempts': 5, 'checks': patient_context_refresh_state._CHECKS,
                'failure': 'nightly_timeout', 'next_try': 1000, 'failure_at': 1000, field: value}
    assert not patient_context_refresh_readiness._ready(progress, 1e9)


def test_other_lanes_cannot_postpone_an_exhausted_summary_daily_retry():
    progress = {'attempts': 5, 'checks': patient_context_refresh_state._CHECKS,
                'failure': 'nightly_timeout', 'failure_at': 1000,
                'next_try': 1000 + 86400, 'last_attempt': 1000 + 86399}
    assert patient_context_refresh_readiness._ready(progress, 1000 + 86400)
    progress['last_attempt'] += 86400
    assert patient_context_refresh_readiness._ready(progress, 1000 + 86400)
