"""Synthetic ownership and shared-budget tests for patient summary quality."""
import time
import os
import json

import pytest

import patient_context_refresh as refresh
import extract_llm as worker
import cloud_inference
import semantic_jev
import semantic_runtime as semantic_runtime
from test_patient_context_refresh import runtime as runtime, seed, finish, step, document, CFG
from semantic_jev import JevError
from extract_testkit import _ledger, _message

_REAL_CLOUD_PROVIDER = cloud_inference.CloudInferenceUnit


REQUEST_IDENTITY = {'input_key': '1' * 64, 'candidate_key': '2' * 64,
                    'failure_key': '3' * 64, 'audit_key': '4' * 64}
REQUEST_BODY = {'state': {'target': {'facts': [{'id': 'q0', 'fact': {'id': 'a' * 64}}]}},
                'questions': {'q0': {}}}


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
            guard=lambda stage: guards.append((stage, led.db.in_transaction)), daily_request_budget=2, identity=REQUEST_IDENTITY)
        reservation = reserve(REQUEST_BODY, 2.0)
        assert reservation is not None
        assert guards == [('jev_reserve', True), ('jev_reserved', True)]
        [row] = led.artifacts('semantic_usage')
        assert row['project_id'] == 1
        other = semantic_runtime.patient_summary_usage_reserver(led, project_id=2, guard=lambda _: None,
            daily_request_budget=2, identity=REQUEST_IDENTITY)
        with pytest.raises(JevError) as caught:
            other(REQUEST_BODY, 2.0)
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
                                                         daily_request_budget=3000, identity=REQUEST_IDENTITY)
        with pytest.raises(RuntimeError, match='patient_summary_transaction_active'):
            reserve(REQUEST_BODY, 2.0)
        assert led.db.in_transaction and led.db.execute('SELECT count(*) FROM synthetic_tx_marker').fetchone()[0] == 1
        led.db.rollback()
        def revoked(stage):
            if stage == 'jev_reserved':
                raise ValueError('synthetic_scope_changed')
        reserve = semantic_runtime.patient_summary_usage_reserver(led, project_id=1, guard=revoked,
                                                         daily_request_budget=3000, identity=REQUEST_IDENTITY)
        with pytest.raises(ValueError, match='synthetic_scope_changed'):
            reserve(REQUEST_BODY, 2.0)
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
                                                         daily_request_budget=3000, identity=REQUEST_IDENTITY)
        backlog(REQUEST_BODY, 2.0)
        other = semantic_runtime.patient_summary_usage_reserver(led, project_id=2, guard=lambda _: None,
            daily_request_budget=3000, identity=REQUEST_IDENTITY)
        with pytest.raises(JevError) as refused:
            other(REQUEST_BODY, 2.0)
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
        queries = []
        led.db.set_trace_callback(queries.append)
        try:
            identity = refresh._quality_input(led.db, 1, snapshot, all_facts, quality_state, summary,
                                              {'model': 'synthetic'}, state)
        finally:
            led.db.set_trace_callback(None)
        assert len([sql for sql in queries if 'message_revisions' in sql]) == 1
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


@pytest.fixture
def quality_owner(runtime, monkeypatch):
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
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *_: 'synthetic-anthropic-key')
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: 'synthetic-jev-key')
    monkeypatch.setattr(semantic_candidate, 'loaded_source_contract', lambda: {'synthetic': 'b' * 64})
    sends, behavior = [], {}
    client = semantic_jev.JevClient
    def post(body, timeout):
        assert not led.db.in_transaction and timeout > 0
        sends.append(list(body['questions']))
        if behavior.get('timeout') or behavior.get('partial') and len(sends) > 1:
            raise TimeoutError('synthetic response lost')
        answers = {}
        for index, (qid, question) in enumerate(body['questions'].items()):
            criteria = question['criteria']
            choice = ('supports' if 'supports' in criteria else 'omission_ok')
            if behavior.get('negative') or behavior.get('partial'):
                choice = 'not_supported' if 'not_supported' in criteria else 'important_missing'
            if behavior.get('partial') and index == 1:
                choice = 'invalid'
            answers[qid] = {'type': 'choice', 'choice': choice, 'confidence': 1.0,
                'probabilities': {option: float(option == choice) for option in criteria}}
        return 200, {}, json.dumps({'model': semantic_jev.JEV_MODEL, 'answers': answers,
                                    'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
    def make_client(**kwargs):
        assert kwargs['max_attempts'] == 1, 'a timeout cannot silently repeat a quality judgement'
        return client(post_fn=post, **kwargs)
    monkeypatch.setattr(semantic_jev, 'JevClient', make_client)
    return led, clock, cfg, doc, sends, behavior


def test_quality_owner_calls_jev_once_and_rechecks_saved_pass(quality_owner):
    led, clock, cfg, doc, sends, _ = quality_owner
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
    with pytest.raises(ValueError, match='patient_summary_quality_unresolved_request'):
        refresh._quality_step(led, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
    assert len(sends) == 1


def test_requested_cloud_with_missing_key_falls_back_to_local(runtime, monkeypatch):
    led, _, local_calls = runtime
    seed(led, body='生活環境: 完全合成の居住記録。')
    cfg = {**CFG, 'cloud_llm': {'mode': 'on', 'project_ids': [1], 'routes': ['patient_context']}}
    monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *_: None)
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker, '_writer_current', lambda **_kwargs: True)
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *_: None)
    result = refresh.run_due(led, cfg, deadline=runtime[1].mono + 600, admit=lambda: True)
    assert result['status'] == 'progressed' and local_calls
    assert refresh._contract(cfg, 1).get('provider') == cloud_inference.cloud_provider_contract()
    for _ in range(60):
        result = step(runtime, cfg=cfg)
        if result['status'] == 'complete':
            break
    assert result['status'] == 'complete'
    assert refresh.context_view_input(led.db, 1, cfg=cfg)


@pytest.fixture
def repair(tmp_path, monkeypatch):
    from contextlib import nullcontext
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
    repair_cfg = {'cloud_llm': {'mode': 'on', 'provider': 'anthropic', 'project_ids': [1]}}
    monkeypatch.setattr(worker, '_cloud_mode', lambda *_: 'on' if enabled[0] else 'off')
    monkeypatch.setattr(worker.cloud_policy, 'budget_block_reason', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker, 'load_config', lambda: repair_cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: repair_cfg)
    def capture(db, pid, cfg, night_id, repaired, contract, projection):
        assert db.in_transaction and pid == 1 and contract == assessment
        key = refresh._digest(sorted((row['fact_ids'][0], row['text']) for row in repaired))
        return {'identity': {**identity, 'candidate_key': key,
                 'failure_key': refresh._digest([input_key, key, assessment]), 'audit_key': 'e' * 64}}
    monkeypatch.setattr(refresh, '_quality_capture', capture)
    def request(_client, method, body, timeout, *, deadline):
        assert method == 'POST' and timeout > 0 and not led.db.in_transaction and current[0]
        [logical] = led.artifacts(refresh.QUALITY_REPAIR, project_id=1)
        [financial] = led.artifacts('cloud_usage_reserve', project_id=1)
        logical_meta, financial_meta = json.loads(logical['meta']), json.loads(financial['meta'])
        assert logical_meta['reservation_artifact_id'] == financial['artifact_id']
        assert logical_meta['reservation_id'] == financial_meta['reservation_id']
        assert logical_meta['request_fp'] == financial_meta['request_fp']
        sent.append(json.loads(body['messages'][0]['content']))
        if responses[0] == 'timeout':
            raise TimeoutError('synthetic transport uncertainty')
        return 200, {}, json.dumps({'model': worker.anthropic_client.MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': json.dumps({'fact_ids': responses})}],
            'usage': {'input_tokens': 2, 'output_tokens': 1}}).encode()
    monkeypatch.setattr(worker.anthropic_client.AnthropicClient, 'request', request)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _REAL_CLOUD_PROVIDER)
    def run():
        return refresh._quality_repair_candidate(led, 1, repair_cfg, '2026-10-27',
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


@pytest.mark.parametrize('damage', ['none', 'answer', 'receipt', 'audit'])
def test_distinct_summary_repair_requires_exact_response_and_receipt(monkeypatch, damage):
    original = {'input_key': 'a' * 64, 'audit_key': 'b' * 64, 'failure_key': 'c' * 64}
    repaired = {'candidate_key': 'd' * 64, 'audit_key': 'e' * 64}
    fact_id = 'f' * 64
    text = json.dumps({'fact_ids': [fact_id]})
    proof = {'version': 1, 'answer_text': text, 'reservation_artifact_id': 17,
             'reservation_id': '1' * 32, 'contract': {'backend': 'openai', 'model': 'synthetic', 'prompt': '2' * 16},
             'night_id': 'synthetic-night', 'original_audit_key': original['audit_key'],
             'original_ids': ['3' * 64], 'receipt_sig': '4' * 64}
    content = {'fact_ids': [fact_id], '__mcs_internal_proof_v1': proof}
    meta = {'input_key': original['input_key'], 'original_failure_key': original['failure_key'],
            'candidate_key': repaired['candidate_key'], 'audit_key': repaired['audit_key']}
    def evidence(_db, **kwargs):
        assert kwargs['prompt'] == 'synthetic prompt'
        assert kwargs['owner'] == {'night_id': 'synthetic-night', 'source_sig': original['input_key']}
        return {'witness': '4' * 64}
    monkeypatch.setattr(cloud_inference, 'verify_cloud_repair_evidence', evidence)
    if damage == 'answer':
        proof['answer_text'] = json.dumps({'fact_ids': ['5' * 64]})
    elif damage == 'receipt':
        proof['receipt_sig'] = '6' * 64
    elif damage == 'audit':
        meta['audit_key'] = '7' * 64
    if damage == 'none':
        assert refresh._summary_repair_response(None, 1, original, repaired, content, meta, 'synthetic prompt') == proof
    else:
        with pytest.raises(ValueError):
            refresh._summary_repair_response(None, 1, original, repaired, content, meta, 'synthetic prompt')


def test_private_repair_proof_only_uses_reserved_field():
    proof = {'answer_text': 'synthetic-private-answer'}
    assert refresh._repair_response({'__mcs_internal_proof_v1': proof}) == proof
    assert refresh._repair_response({'fact_ids': ['a' * 64]}) is None
    for value in ({'repair_response': proof},
                  {'repair_response': proof, '__mcs_internal_proof_v1': proof}):
        with pytest.raises(ValueError):
            refresh._repair_response(value)


def test_private_repair_candidate_is_omitted_from_view_and_aggregate_export():
    import export_schema
    import mcs_view

    content = {'fact_ids': ['a' * 64],
               '__mcs_internal_proof_v1': {'answer_text': 'synthetic-private-answer'}}
    _meta, visible = mcs_view._artifact_json({'meta': '{}', 'content': json.dumps(content)})
    assert visible == {'fact_ids': ['a' * 64]}
    record = {'type': 'message', 'contract': 'mcs-read-model/1',
              'snapshot_generation_id': 'synthetic-generation', 'project_id': 1, 'message_id': 1,
              '__mcs_internal_proof_v1': content['__mcs_internal_proof_v1']}
    projected = export_schema.project_record(record)
    assert projected['content_omitted'] is True and 'synthetic-private-answer' not in json.dumps(projected)
    with pytest.raises(ValueError):
        export_schema.validate_record(record)


def test_summary_repair_slot_is_not_reset_by_profile_or_ambiguous_legacy(repair):
    led, _run, _facts, _responses, _sent, _enabled, _current = repair
    from inference_version import CURRENT_VERSION, VERSION_KEY
    led.artifact_add(refresh.QUALITY_REPAIR, '{}', project_id=1,
                     meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '1' * 64,
                           'candidate_key': '2' * 64, 'repair_scope': '3' * 64})
    assert refresh._other_summary_repair_spent(led.db, 1, {'input_key': '4' * 64, 'repair_scope': '3' * 64})
    assert not refresh._other_summary_repair_spent(led.db, 1, {'input_key': '4' * 64, 'repair_scope': '5' * 64})
    led.artifact_add(refresh.QUALITY_REPAIR, '{}', project_id=1,
                     meta={VERSION_KEY: CURRENT_VERSION, 'input_key': '6' * 64, 'candidate_key': '7' * 64})
    assert refresh._other_summary_repair_spent(led.db, 1, {'input_key': '4' * 64, 'repair_scope': '5' * 64})


@pytest.mark.parametrize('strip_candidate_marker', [False, True])
def test_summary_profiled_repair_cannot_hide_by_dropping_its_publication_ref(repair, strip_candidate_marker):
    led, _run, _facts, _responses, _sent, _enabled, _current = repair
    identity = {'input_key':'a' * 64, 'audit_key':'b' * 64}
    candidate_meta = {'input_key':identity['input_key'], 'audit_key':identity['audit_key']}
    if not strip_candidate_marker:
        candidate_meta['repair_version'] = 1
    led.artifact_add(refresh.QUALITY_REPAIR_CANDIDATE, json.dumps({'fact_ids':['c' * 64]}),
                     project_id=1, meta=candidate_meta)
    led.artifact_add('cloud_usage_reserve', '{}', project_id=1, meta={
        'route':'patient_context', 'unit_key':'summary_quality_repair',
        'owner':{'source_sig':identity['input_key']}, 'profile':'sol-high'})
    assert not refresh._summary_repair_publication(led.db, 1, {}, {}, {}, {'identity':identity}, {}, {}, 0.8)


def test_summary_high_repair_retains_response_receipt_for_resume(repair, monkeypatch, tmp_path):
    from contextlib import nullcontext

    led, _run, facts, _responses, _sent, _enabled, _current = repair
    cfg = {'cloud_llm': {'mode':'on', 'provider':'openai', 'project_ids':[1]}}
    monkeypatch.setattr(worker, 'HOME', str(tmp_path))
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker.cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.cloud_policy, 'block_reason', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _REAL_CLOUD_PROVIDER)
    posts = []
    real_client = worker.openai_client.OpenAIClient
    class Client(real_client):
        def request(self, method, body, timeout, **kwargs):
            posts.append(body)
            return 200, {}, json.dumps({'status':'completed', 'model':self.model, 'service_tier':'default',
                'output':[{'type':'message','role':'assistant','content':[{'type':'output_text',
                    'text':json.dumps({'fact_ids':[facts[1]['id']]})}]}],
                'usage':{'input_tokens':2,'output_tokens':1,'total_tokens':3,
                         'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}}).encode()
    monkeypatch.setattr(worker.openai_client, 'OpenAIClient', Client)
    fail = json.loads(led.artifacts(refresh.QUALITY_FAIL)[0]['meta'])
    identity = {key:fail[key] for key in ('input_key','candidate_key','failure_key','audit_key')}
    identity['fact_ids'] = [fact['id'] for fact in facts]
    claim_row = led.db.execute('SELECT payload,next_try FROM fetch_jobs WHERE kind=? AND project_id=1',
                               (refresh.QUALITY_CLAIM,)).fetchone()
    claim = (json.loads(claim_row['payload'])['token'], claim_row['next_try'])
    summary = [{'text':facts[0]['quote'], 'fact_ids':[facts[0]['id']]}]
    def run(night):
        return refresh._quality_repair_candidate(led, 1, cfg, night,
            {'summary':summary, 'temporal_view':{}}, identity, claim, lambda *_: True,
            time.monotonic() + 600, lambda: True, facts, {'model':'synthetic'}, 0.8,
            {'groups':{}, 'relations':[]})
    status, candidate = run('synthetic-night')
    assert status == 'candidate' and len(posts) == 1 and candidate['repair_ref']
    assert posts[0]['model'] == 'gpt-6.1-sol' and posts[0]['reasoning'] == {'effort':'high'}
    assert posts[0]['max_output_tokens'] == 32768
    status, resumed = run('synthetic-next-night')
    assert status == 'candidate' and resumed['repair_ref'] == candidate['repair_ref'] and len(posts) == 1
    row = led.artifacts(refresh.QUALITY_REPAIR_CANDIDATE)[0]
    content = json.loads(row['content'])
    content['__mcs_internal_proof_v1']['answer_text'] += ' '
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(content), row['artifact_id']))
    assert run('synthetic-next-night') == ('held', None) and len(posts) == 1


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
    [reservation] = led.artifacts('cloud_usage_reserve', project_id=1)
    [result] = led.artifacts('cloud_usage_result', project_id=1)
    assert json.loads(result['meta'])['reservation_id'] == json.loads(reservation['meta'])['reservation_id']
    assert json.loads(result['meta'])['http_class'] == 'transport'
    original = [(r['artifact_id'], r['content'], r['meta']) for r in (reservation, result)]
    assert run() == ('held', None) and len(sent) == 1
    assert [(r['artifact_id'], r['content'], r['meta']) for r in
            (led.artifacts('cloud_usage_reserve', project_id=1)[0],
             led.artifacts('cloud_usage_result', project_id=1)[0])] == original


@pytest.mark.parametrize('refusal', ['capacity', 'budget_race', 'run_lock', 'hook_failure'])
def test_summary_repair_reservation_refusal_keeps_both_allowances_unspent(repair, monkeypatch, refusal):
    from contextlib import nullcontext
    led, run, _, _, sent, _, _ = repair
    with monkeypatch.context() as limited:
        if refusal == 'capacity':
            limited.setattr(worker.cloud_backlog, 'request_slot', lambda *_: nullcontext(False))
        elif refusal == 'budget_race':
            limited.setattr(worker.cloud_policy, 'budget_block_reason',
                            lambda owner, *_args, **_kwargs: 'daily_cap' if owner.db.in_transaction else None)
        elif refusal == 'run_lock':
            limited.setattr(refresh, 'acquire_run_lock', lambda: None)
        else:
            led.db.execute("CREATE TRIGGER synthetic_repair_failure AFTER INSERT ON artifacts "
                           "WHEN NEW.kind='patient_context_summary_quality_repair_v1' "
                           "BEGIN SELECT RAISE(ABORT,'synthetic reservation abort'); END")
            led.db.commit()
        assert run() == ('deferred', None) and not sent
        assert not led.db.in_transaction
        assert not led.artifacts(refresh.QUALITY_REPAIR, project_id=1)
        assert not led.artifacts('cloud_usage_reserve', project_id=1)
        assert not led.artifacts('cloud_usage_result', project_id=1)
    if refusal == 'hook_failure':
        led.db.execute('DROP TRIGGER synthetic_repair_failure')
        led.db.commit()
    assert run()[0] == 'candidate' and len(sent) == 1
    assert len(led.artifacts(refresh.QUALITY_REPAIR, project_id=1)) == 1
    assert len(led.artifacts('cloud_usage_reserve', project_id=1)) == 1


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


@pytest.mark.parametrize('fault', ['checkpoint', 'timeout', 'partial'])
def test_unknown_quality_request_survives_later_tick_reopen_day_and_cleanup(quality_owner, monkeypatch, fault):
    from contextlib import closing
    import ledger
    import maintenance
    led, clock, cfg, doc, sends, behavior = quality_owner
    if fault == 'checkpoint':
        behavior['negative'] = True
        original = refresh._quality_store_answers
        def denied(*args, **kwargs):
            with monkeypatch.context() as patch:
                patch.setattr(refresh, 'acquire_run_lock', lambda: None)
                return original(*args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(refresh, '_quality_store_answers', denied)
            with pytest.raises(refresh.Deferred, match='patient_summary_quality_lock'):
                refresh._quality_step(led, 1, cfg, '2026-10-17', doc, clock.mono + 600, lambda: True)
        assert not led.artifacts(refresh.QUALITY) and not led.artifacts(refresh.QUALITY_FAIL)
    else:
        behavior[fault] = True
        assert refresh._quality_step(led, 1, cfg, '2026-10-17', doc, clock.mono + 600,
                                     lambda: True)[0] == 'unknown'
        assert not led.artifacts(refresh.QUALITY_SKIP)
    count = 2 if fault == 'partial' else 1
    assert len(sends) == count
    if fault == 'partial':
        assert len(led.artifacts(refresh.QUALITY)) == len(led.artifacts(refresh.QUALITY_FAIL)) == 1
        assert sends[0] == ['q0', 'q1'] and sends[1] == ['q1']
    clock.wall += 9 * 86400
    maintenance.prune_history(led.db, now=time.time() + 9 * 86400)
    # A resolved negative can be pruned from usage; its immutable QUALITY/FAIL remains.
    assert len(led.artifacts('semantic_usage')) >= 1
    path = led.db.execute('PRAGMA database_list').fetchone()[2]
    with closing(ledger.Ledger(path)) as reopened:
        status, _ = refresh._quality_step(reopened, 1, cfg, '2026-10-17', doc,
                                           clock.mono + 600, lambda: True)
        assert status == 'unknown'
    assert len(sends) == count
    assert not led.artifacts(refresh.QUALITY_PASS)
