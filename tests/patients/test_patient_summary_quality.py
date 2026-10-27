"""Synthetic ownership and shared-budget tests for patient summary quality."""
import time
import os
import json
from types import SimpleNamespace

import pytest

import patient_context_refresh as refresh
import extract_llm as worker
import semantic_runtime as semantic_runtime
from test_patient_context_refresh import runtime as runtime, seed, finish, document, CFG
from semantic_jev import JevError
from extract_testkit import _ledger, _message


def test_context_refuses_borrowed_transaction_without_touching_it(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.db.execute('CREATE TABLE synthetic_tx_marker(value TEXT)')
        led.db.commit()
        led.db.execute("INSERT INTO synthetic_tx_marker VALUES('pending')")
        result = refresh.run_due(led, {}, deadline=time.monotonic() + 300, admit=lambda: True)
        assert result == {'status': 'deferred', 'done': 0, 'reason': 'transaction_active'}
        assert led.db.in_transaction
        assert led.db.execute('SELECT count(*) FROM synthetic_tx_marker').fetchone()[0] == 1
        led.db.rollback()
        assert led.db.execute('SELECT count(*) FROM synthetic_tx_marker').fetchone()[0] == 0
    finally:
        led.close()


def test_patient_summary_usage_is_shared_bounded_and_owned(tmp_path):
    led = _ledger(tmp_path)
    try:
        guards = []
        reserve = semantic_runtime.patient_summary_usage_reserver(led, project_id=1,
            guard=lambda stage: guards.append((stage, led.db.in_transaction)), daily_request_budget=2)
        reservation = reserve({'synthetic': True}, 2.0)
        assert reservation is not None
        assert guards == [('jev_reserve', True), ('jev_reserved', True)]
        [row] = led.artifacts('semantic_usage')
        assert row['project_id'] == 1
        with pytest.raises(JevError) as caught:
            reserve({'synthetic': True}, 2.0)
        assert caught.value.kind == 'budget_exceeded' and caught.value.detail == 'daily_cap'
        assert len(led.artifacts('semantic_usage')) == 1
        assert not led.db.in_transaction
    finally:
        led.close()


def test_patient_summary_usage_keeps_borrowed_transaction_and_rejects_changed_scope(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.db.execute('CREATE TABLE synthetic_tx_marker(value TEXT)')
        led.db.commit()
        led.db.execute("INSERT INTO synthetic_tx_marker VALUES('pending')")
        reserve = semantic_runtime.patient_summary_usage_reserver(led, project_id=1, guard=lambda _: None,
                                                         daily_request_budget=3000)
        with pytest.raises(RuntimeError, match='patient_summary_transaction_active'):
            reserve({'synthetic': True}, 2.0)
        assert led.db.in_transaction and led.db.execute('SELECT count(*) FROM synthetic_tx_marker').fetchone()[0] == 1
        led.db.rollback()
        def revoked(stage):
            if stage == 'jev_reserved':
                raise ValueError('synthetic_scope_changed')
        reserve = semantic_runtime.patient_summary_usage_reserver(led, project_id=1, guard=revoked,
                                                         daily_request_budget=3000)
        with pytest.raises(ValueError, match='synthetic_scope_changed'):
            reserve({'synthetic': True}, 2.0)
        assert not led.artifacts('semantic_usage') and not led.db.in_transaction
    finally:
        led.close()


def test_quality_backlog_keeps_existing_realtime_reserve(tmp_path):
    from semantic_policy import BACKLOG_BUDGET_RESERVE
    import math
    led = _ledger(tmp_path)
    try:
        with led.db:
            led.artifact_add_tx('semantic_usage', '{}', meta={'jev_requests': 2699})
        cap = 3000 - math.ceil(3000 * BACKLOG_BUDGET_RESERVE)
        assert cap == 2700
        backlog = semantic_runtime.patient_summary_usage_reserver(led, project_id=1, guard=lambda _: None,
                                                         daily_request_budget=3000)
        backlog({'synthetic': True}, 2.0)
        with pytest.raises(JevError) as refused:
            backlog({'synthetic': True}, 2.0)
        assert refused.value.detail == 'daily_cap'
        realtime = semantic_runtime.manual_usage_reserver(led, operation='jev_value', daily_request_budget=3000)
        assert realtime({'synthetic': True}, 2.0) is not None
    finally:
        led.close()


def test_quality_identity_binds_actual_body_and_revision_not_just_stored_hash(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.save_messages([_message(1, '生活環境: 合成の居住記録。', project_id=1)])
        led.db.execute('BEGIN')
        snapshot = refresh._snapshot(led.db, 1, {}, hydrate=True)
        source = next(s for s in snapshot['sources'] if s['source'] == 'chat')
        selected, _, state, facts, quality_state = refresh._all_facts(
            led.db, 1, snapshot, '2026-10-27', include_all=True)
        assert facts
        summary = [{'text': refresh.context_plan.summary_text(selected[0]), 'fact_ids': [selected[0]['id']]}]
        original = refresh._quality_input(led.db, 1, snapshot, facts, quality_state, summary, {'model': 'synthetic'}, state)
        changed = {**source, 'body_text': source['body_text'] + '\n\n別の合成追記。'}
        changed_snapshot = {**snapshot, 'sources': [changed]}
        with pytest.raises(ValueError, match='patient_summary_quality_source'):
            refresh._quality_input(led.db, 1, changed_snapshot, facts, quality_state, summary,
                                   {'model': 'synthetic'}, state)
        led.db.rollback()
        max_seq = led.db.execute('SELECT COALESCE(MAX(seq),0) FROM message_revisions WHERE message_id=1').fetchone()[0]
        led.db.execute('INSERT INTO message_revisions(message_id,seq,content_hash,body_state,observed_at) '
                       'VALUES(1,?,?,?,?)', (max_seq + 1, source['content_hash'], 'full', time.time()))
        led.db.commit()
        led.db.execute('BEGIN')
        snapshot = refresh._snapshot(led.db, 1, {}, hydrate=True)
        revision_changed = refresh._quality_input(led.db, 1, snapshot, facts, quality_state, summary,
                                                  {'model': 'synthetic'}, state)
        assert revision_changed['input_key'] != original['input_key']
        changed_assessment = refresh._quality_input(led.db, 1, snapshot, facts, quality_state, summary,
                                                   {'model': 'synthetic-new'}, state)
        assert changed_assessment['input_key'] == revision_changed['input_key']
        assert changed_assessment['audit_key'] != revision_changed['audit_key']
        assert changed_assessment['failure_key'] != revision_changed['failure_key']
        led.db.rollback()
    finally:
        led.close()


def test_quality_identity_includes_fact_outside_selection_cap(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.save_messages([_message(mid, f'生活環境: 合成の居住記録{mid}。', project_id=1)
                           for mid in range(1, 258)])
        led.db.execute('BEGIN')
        snapshot = refresh._snapshot(led.db, 1, {}, hydrate=True)
        selected, total, state, all_facts, quality_state = refresh._all_facts(
            led.db, 1, snapshot, '2026-10-27', include_all=True)
        assert total == len(all_facts) == 257 and len(selected) == 256
        summary = [{'text': refresh.context_plan.summary_text(selected[0]), 'fact_ids': [selected[0]['id']]}]
        identity = refresh._quality_input(led.db, 1, snapshot, all_facts, quality_state, summary,
                                          {'model': 'synthetic'}, state)
        omitted = {fact['id'] for fact in all_facts} - {fact['id'] for fact in selected}
        assert len(omitted) == 1 and omitted <= set(identity['fact_ids'])
        led.db.rollback()
    finally:
        led.close()


def test_quality_audit_binds_display_order_but_failure_identity_does_not(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.save_messages([_message(mid, f'生活環境: 合成の記録{mid}。', project_id=1) for mid in (1, 2)])
        led.db.execute('BEGIN')
        snapshot = refresh._snapshot(led.db, 1, {}, hydrate=True)
        selected, _, state, all_facts, quality_state = refresh._all_facts(
            led.db, 1, snapshot, '2026-10-27', include_all=True)
        assert len(selected) == len(all_facts) == 2
        summary = [{'text': refresh.context_plan.summary_text(fact), 'fact_ids': [fact['id']]}
                   for fact in selected]
        first = refresh._quality_input(led.db, 1, snapshot, all_facts, quality_state, summary,
                                       {'model': 'synthetic'}, state)
        reversed_order = refresh._quality_input(led.db, 1, snapshot, all_facts, quality_state,
                                                list(reversed(summary)), {'model': 'synthetic'}, state)
        assert first['candidate_key'] == reversed_order['candidate_key']
        assert first['failure_key'] == reversed_order['failure_key']
        assert first['audit_key'] != reversed_order['audit_key']
        led.db.rollback()
    finally:
        led.close()


def test_quality_denominator_keeps_historical_karte(tmp_path):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        led.db.execute('UPDATE patients SET karte_id=123 WHERE project_id=1')
        led.db.commit()
        for number in (1, 2):
            led.artifact_add('karte_summary', json.dumps({'comment': f'生活環境: 合成記録{number}。',
                'updated_at': f'2026-10-{number:02}T00:00:00+09:00'}, ensure_ascii=False),
                project_id=1, meta={'karte_id': 123})
        led.db.execute('BEGIN')
        snapshot = refresh._snapshot(led.db, 1, {}, hydrate=True)
        selected, total, state, all_facts, quality_state = refresh._all_facts(
            led.db, 1, snapshot, '2026-10-27', include_all=True)
        assert total == len(selected) == 1 and len(all_facts) == 2
        assert sum(bool(fact.get('historical')) for fact in all_facts) == 1
        summary = [{'text': refresh.context_plan.summary_text(selected[0]), 'fact_ids': [selected[0]['id']]}]
        identity = refresh._quality_input(led.db, 1, snapshot, all_facts, quality_state, summary,
                                          {'model': 'synthetic'}, state)
        assert len(identity['fact_ids']) == 2
        led.db.rollback()
    finally:
        led.close()


def test_quality_answer_checkpoint_is_claimed_idempotent_and_source_fenced(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        monkeypatch.setattr(refresh, 'acquire_run_lock', lambda: os.open(os.devnull, os.O_RDONLY))
        identity = {'input_key': 'a' * 64, 'candidate_key': 'd' * 64, 'failure_key': 'f' * 64,
                    'audit_key': 'b' * 64, 'fact_ids': ['c' * 64]}
        claim = refresh._quality_claim(led, 1, identity['input_key'], time.monotonic() + 60, lambda *_: True)
        assert claim is not None
        assert refresh._quality_claim(led, 1, identity['input_key'], time.monotonic() + 60, lambda *_: True) is None
        answers = {'c' * 64: {'choice': 'important_missing', 'confidence': 0.95}}
        assert refresh._quality_store_answers(led, 1, identity, claim, answers, lambda *_: True, 0.8, set())
        assert refresh._quality_store_answers(led, 1, identity, claim, answers, lambda *_: True, 0.8, set())
        assert refresh._quality_failed(led.db, 1, identity)
        assert refresh._quality_failure_allows_remainder(led.db, 1, identity)
        assert not refresh._quality_failure_allows_remainder(led.db, 1, {**identity, 'audit_key': '9' * 64})
        assert len(led.artifacts(refresh.QUALITY)) == 1
        with pytest.raises(ValueError, match='patient_summary_quality_answers_conflict'):
            refresh._quality_store_answers(led, 1, identity, claim,
                                           {'c' * 64: {'choice': 'omission_ok', 'confidence': 0.95}},
                                           lambda *_: True, 0.8, set())
        with pytest.raises(ValueError, match='patient_summary_quality_source_changed'):
            refresh._quality_store_answers(led, 1, identity, claim, answers, lambda *_: False, 0.8, set())
        assert refresh._quality_answers(led.db, 1, identity['audit_key'], ['c' * 64]) == answers
        refresh._quality_release(led, 1, identity['input_key'], claim)
        assert not led.db.execute('SELECT 1 FROM fetch_jobs WHERE kind=?', (refresh.QUALITY_CLAIM,)).fetchone()
    finally:
        led.close()


def test_quality_pass_requires_every_source_bound_question(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        monkeypatch.setattr(refresh, 'acquire_run_lock', lambda: os.open(os.devnull, os.O_RDONLY))
        identity = {'input_key': 'a' * 64, 'candidate_key': 'd' * 64, 'failure_key': 'f' * 64,
                    'audit_key': 'b' * 64, 'fact_ids': ['c' * 64, 'e' * 64]}
        claim = refresh._quality_claim(led, 1, identity['input_key'], time.monotonic() + 60, lambda *_: True)
        refresh._quality_store_answers(led, 1, identity, claim,
            {'c' * 64: {'choice': 'supports', 'confidence': 0.99}}, lambda *_: True, 0.9, {'c' * 64})
        with pytest.raises(ValueError, match='patient_summary_quality_incomplete'):
            refresh._quality_mark_pass(led, 1, identity, claim, {'c' * 64}, lambda *_: True, 0.9)
        assert not refresh._quality_passed(led.db, 1, identity)
        refresh._quality_store_answers(led, 1, identity, claim,
            {'e' * 64: {'choice': 'omission_ok', 'confidence': 0.99}}, lambda *_: True, 0.9, {'c' * 64})
        assert refresh._quality_mark_pass(led, 1, identity, claim, {'c' * 64}, lambda *_: True, 0.9)
        assert refresh._quality_passed(led.db, 1, identity)
        # A PASS marker alone must not survive a partial restore of its answers.
        led.db.execute("DELETE FROM artifacts WHERE kind=? AND json_extract(meta,'$.fact_id')=?",
                       (refresh.QUALITY, 'e' * 64))
        led.db.commit()
        with pytest.raises(ValueError, match='patient_summary_quality_incomplete'):
            refresh._quality_mark_pass(led, 1, identity, claim, {'c' * 64}, lambda *_: True, 0.9)
        refresh._quality_release(led, 1, identity['input_key'], claim)
    finally:
        led.close()


def test_quality_retry_and_hard_failure_stay_distinct_from_budget_defer(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        monkeypatch.setattr(refresh, 'acquire_run_lock', lambda: os.open(os.devnull, os.O_RDONLY))
        identity = {'input_key': 'a' * 64, 'candidate_key': 'd' * 64, 'failure_key': 'f' * 64,
                    'audit_key': 'b' * 64, 'fact_ids': ['c' * 64]}
        claim = refresh._quality_claim(led, 1, identity['input_key'], time.monotonic() + 60, lambda *_: True)
        page = 'e' * 64
        before = refresh._quality_retry(led.db, 1, identity['audit_key'], page)
        assert before == {'attempts': 0, 'next_try': 0, 'held': False}
        # Resource/budget deferrals do not write a failure record.
        with pytest.raises(ValueError):
            refresh._quality_record_failure(led, 1, identity, claim, page, 'resource', lambda *_: True)
        assert refresh._quality_retry(led.db, 1, identity['audit_key'], page) == before
        refresh._quality_record_failure(led, 1, identity, claim, page, 'retry', lambda *_: True)
        state = refresh._quality_retry(led.db, 1, identity['audit_key'], page)
        assert state['attempts'] == 1 and state['next_try'] > time.time() and not state['held']
        refresh._quality_record_failure(led, 1, identity, claim, page, 'failed', lambda *_: True)
        state = refresh._quality_retry(led.db, 1, identity['audit_key'], page)
        assert state['attempts'] == 2 and state['held']
        refresh._quality_release(led, 1, identity['input_key'], claim)
    finally:
        led.close()


@pytest.mark.parametrize('kind', [refresh.QUALITY, refresh.QUALITY_FAIL, refresh.QUALITY_PASS,
                                  refresh.QUALITY_RETRY, refresh.QUALITY_REPAIR])
def test_malformed_quality_receipt_never_looks_like_absent_work(tmp_path, kind):
    led = _ledger(tmp_path)
    try:
        led.ensure_patient(1)
        artifact = led.artifact_add(kind, '{}', project_id=1, meta={})
        led.db.execute("UPDATE artifacts SET meta='{broken' WHERE artifact_id=?", (artifact,))
        led.db.commit()
        with pytest.raises(ValueError, match='patient_summary_quality_metadata_invalid'):
            refresh._quality_integrity(led.db, 1)
    finally:
        led.close()


def test_quality_owner_calls_jev_once_and_rechecks_saved_pass(runtime, monkeypatch):
    import semantic_candidate
    import semantic_jev
    led, clock, _ = runtime
    clock.mono = time.monotonic()
    seed(led, body='生活環境: 完全合成の居住記録。')
    finish(runtime)
    doc = document(led)
    assert doc and doc['summary']
    cfg = {**CFG, 'cloud_llm': {'mode': 'off'},
           'semantic': {'mode': 'assist', 'project_ids': [1], 'daily_request_budget': 3000}}
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *_: 'synthetic-anthropic-key')
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: 'synthetic-jev-key')
    monkeypatch.setattr(semantic_candidate, 'candidate_fingerprint', lambda _: 'a' * 64)
    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', {'synthetic': 'b' * 64})
    sends = []
    class Client:
        supports_attempt_hooks = True
        requests_made = 0
        usage_totals = {}
        last_error = None
        def __init__(self, **kwargs):
            self.attempt_timeout = kwargs['attempt_timeout']
        def evaluate(self, state, questions, deadline, *, before_attempt, after_result, reserve_fn, transport):
            assert not led.db.in_transaction
            before_attempt()
            reserve_fn({'synthetic': True}, 1.0)
            before_attempt()
            sends.append(sorted(questions))
            self.requests_made += 1
            answers = {qid: {'choice': 'supports' if 'supports' in question['criteria'] else 'omission_ok',
                             'confidence': 0.99}
                       for qid, question in questions.items()}
            after_result()
            return {'answers': answers}
    monkeypatch.setattr(semantic_jev, 'JevClient', Client)
    status, identity = refresh._quality_step(led, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert status == 'pending' and len(sends) == 1
    assert len(led.artifacts('semantic_usage')) == 1
    status, identity = refresh._quality_step(led, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert status == 'passed' and len(sends) == 1 and refresh._quality_passed(led.db, 1, identity)
    answers = led.artifacts(refresh.QUALITY)
    assert {json.loads(row['meta'])['fact_id'] for row in answers} == set(identity['fact_ids'])
    assert len(answers) == len(identity['fact_ids']) == 2
    answer = answers[0]
    led.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (answer['artifact_id'],))
    led.db.commit()
    with pytest.raises(ValueError, match='patient_summary_quality_incomplete'):
        refresh._quality_step(led, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert len(sends) == 1


def test_requested_cloud_with_missing_key_does_not_fall_back_to_local(runtime, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body='生活環境: 完全合成の居住記録。')
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'project_ids': [1]}}
    monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *_: None)
    result = refresh.run_due(led, cfg, deadline=runtime[1].mono + 600, admit=lambda: True)
    assert result['status'] == 'deferred' and not local_calls
    assert refresh._contract(cfg, 1).get('provider') == worker._cloud_contract()
    assert not document(led)


@pytest.fixture
def repair(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    led.ensure_patient(1)
    monkeypatch.setattr(refresh, 'acquire_run_lock', lambda: os.open(os.devnull, os.O_RDONLY))
    assessment = {'model': 'synthetic'}
    facts = [
        {'id': fid, 'project_id': 1, 'category': 'living', 'posted_at': None, 'subject': 'patient',
         'text': text, 'quote': text, 'details': [], 'view_bucket': 'current'}
        for fid, text in [('a' * 64, '合成の現在情報。'), ('b' * 64, '合成の重要な変更。')]]
    summary = [{'text': facts[0]['quote'], 'fact_ids': [facts[0]['id']]}]
    input_key = 'c' * 64
    candidate_key = refresh._digest(sorted((row['fact_ids'][0], row['text']) for row in summary))
    identity = {'input_key': input_key, 'candidate_key': candidate_key,
                'failure_key': refresh._digest([input_key, candidate_key, assessment]),
                'audit_key': 'd' * 64, 'fact_ids': [f['id'] for f in facts]}
    claim = refresh._quality_claim(led, 1, input_key, time.monotonic() + 600, lambda *_: True)
    refresh._quality_store_answers(led, 1, identity, claim,
        {facts[0]['id']: {'choice': 'supports', 'confidence': 0.99},
         facts[1]['id']: {'choice': 'important_missing', 'confidence': 0.99}}, lambda *_: True, 0.8,
        {facts[0]['id']})
    responses, sent, enabled, current = [facts[1]['id']], [], [True], [True]
    monkeypatch.setattr(worker, '_cloud_mode', lambda *_: 'on' if enabled[0] else 'off')
    monkeypatch.setattr(worker.cloud_policy, 'budget_block_reason', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, 'load_config', lambda: {})
    def capture(db, pid, cfg, night_id, repaired, contract, projection):
        assert db.in_transaction and pid == 1 and contract == assessment
        key = refresh._digest(sorted((row['fact_ids'][0], row['text']) for row in repaired))
        return {'identity': {**identity, 'candidate_key': key,
                 'failure_key': refresh._digest([input_key, key, assessment]), 'audit_key': 'e' * 64}}
    monkeypatch.setattr(refresh, '_quality_capture', capture)
    class Provider:
        def __init__(self, ledger, cfg, **kwargs):
            assert ledger is led and kwargs['route'] == 'patient_context'
            self.current = kwargs['current']
            self.client = SimpleNamespace(max_attempts=kwargs.get('max_attempts', 3))
        def __call__(self, prompt, **kwargs):
            assert not led.db.in_transaction and self.current() and self.client.max_attempts == 1
            sent.append(json.loads(prompt))
            if responses[0] == 'timeout':
                raise worker.CloudFallback('synthetic_timeout')
            return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps({'fact_ids': responses})}
        def commit_guard(self):
            if not self.current():
                raise worker.CloudFallback('cloud_unit_changed')
    monkeypatch.setattr(worker, '_CloudProvider', Provider)
    def run():
        return refresh._quality_repair_candidate(led, 1, {}, '2026-10-27',
            {'summary': summary, 'temporal_view': {}}, identity, claim, lambda *_: current[0],
            time.monotonic() + 600, lambda: True, facts, assessment, 0.8,
            {'groups': {}, 'relations': []})
    yield led, run, facts, responses, sent, enabled, current
    led.close()


def test_failed_candidate_repairs_once_and_resume_reuses_saved_ids(repair):
    led, run, facts, _, sent, _, _ = repair
    status, result = run()
    assert status == 'candidate' and result['fact_ids'] == [facts[1]['id']]
    assert len(sent) == 1 and sent[0]['findings'] == [{'fact_id': facts[1]['id'], 'code': 'important_missing'}]
    assert len(led.artifacts(refresh.QUALITY_REPAIR)) == 1
    [saved] = led.artifacts(refresh.QUALITY_REPAIR_CANDIDATE)
    assert json.loads(saved['content']) == {'fact_ids': [facts[1]['id']]}
    assert '合成の重要な変更。' not in saved['content']
    assert run()[0] == 'candidate' and len(sent) == 1


def test_budget_unavailable_does_not_spend_repair(repair):
    led, run, _, _, sent, enabled, _ = repair
    enabled[0] = False
    assert run() == ('deferred', None) and not sent and not led.artifacts(refresh.QUALITY_REPAIR)


def test_exact_repair_prompt_over_cost_cap_does_not_spend_repair(repair, monkeypatch):
    led, run, _, _, sent, _, _ = repair
    seen = []
    def unavailable(_ledger, _settings, **kwargs):
        seen.append(kwargs)
        return 'daily_cost_cap'
    monkeypatch.setattr(worker.cloud_policy, 'budget_block_reason', unavailable)
    assert run() == ('deferred', None) and not sent and not led.artifacts(refresh.QUALITY_REPAIR)
    assert seen and seen[0]['max_tokens'] == 1600 and seen[0]['input_bound'] > 0


def test_unknown_after_send_spends_repair_and_does_not_retry(repair):
    led, run, _, responses, sent, _, _ = repair
    responses[:] = ['timeout']
    assert run() == ('held', None) and len(sent) == 1
    assert len(led.artifacts(refresh.QUALITY_REPAIR)) == 1
    assert run() == ('held', None) and len(sent) == 1


@pytest.mark.parametrize('bad', ['same', 'unknown'])
def test_same_or_unknown_selection_is_not_a_candidate(repair, bad):
    led, run, facts, responses, sent, _, _ = repair
    responses[:] = [facts[0]['id'] if bad == 'same' else 'f' * 64]
    assert run() == ('held', None) and len(sent) == 1
    assert len(led.artifacts(refresh.QUALITY_REPAIR)) == 1
    assert not led.artifacts(refresh.QUALITY_REPAIR_CANDIDATE)


def test_changed_scope_before_send_does_not_spend_repair(repair):
    led, run, _, _, sent, _, current = repair
    current[0] = False
    assert run() == ('deferred', None) and not sent and not led.artifacts(refresh.QUALITY_REPAIR)


def test_partial_audit_does_not_start_repair(repair):
    led, run, facts, _, sent, _, _ = repair
    rows = led.artifacts(refresh.QUALITY, project_id=1)
    target = next(row for row in rows if json.loads(row["meta"]).get("fact_id") == facts[0]["id"])
    led.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (target["artifact_id"],))
    led.db.commit()
    assert run() == ("deferred", None)
    assert not sent and not led.artifacts(refresh.QUALITY_REPAIR)


def test_corrupt_repair_receipt_does_not_grant_a_new_attempt(repair):
    led, run, _, _, sent, _, _ = repair
    led.db.execute("INSERT INTO artifacts(kind,project_id,content,meta,created_at) VALUES(?,?,?,?,?)",
                   (refresh.QUALITY_REPAIR, 1, '{}', '{', time.time()))
    led.db.commit()
    assert run() == ('held', None)
    assert not sent
