"""Synthetic cloud context finite paid repair and adoption contracts."""
import extract_llm_context
import extract_llm_engine
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store

import patient_context_quality_controller
import patient_context_quality_receipts
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import patient_context_plan as context_plan
import mcs_queries
import json
import time
import pytest
import extract_llm as worker
import cloud_inference
import patient_context_refresh as context_refresh
from test_patient_context_refresh import document, seed
import semantic_jev as _source_repair_jev
from anthropic_context_testkit import (
    CFG, _REPAIR_BED, _REPAIR_HOME, _SOURCE_REPAIR_CLIENT, _SOURCE_REPAIR_JEV, _SOURCE_REPAIR_PROVIDER, cloud, drain, runtime, source_repair, step
)

__all__ = ["runtime", "cloud", "source_repair"]


@pytest.mark.parametrize("kind", ["patient_context_source_quality_repair_v1",
                                  "patient_context_source_quality_repair_candidate_v1"])
def test_source_repair_slot_is_bound_to_raw_occurrence_across_contract_changes(runtime, kind):
    import patient_context_artifacts as artifacts
    from inference_version import CURRENT_VERSION, VERSION_KEY

    ledger, _clock, _local_calls = runtime
    seed(ledger)
    occurrence = {"source_binding": "a" * 64, "first_seen": 123.0,
                  "scope": {"project_id": 1, "project_type": "medical", "karte_id": None, "is_archived": 0}}
    meta = {VERSION_KEY: CURRENT_VERSION, "repair_input_key": "b" * 64, "source_key": 1,
            "source_sig": "c" * 64, "source_binding": occurrence["source_binding"],
            "source_occurrence_version": 1, "source_occurrence": occurrence, "body_length": 12}
    ledger.artifact_add(kind, '{}', project_id=1, message_id=1, meta=meta)
    def spent(key, current):
        return artifacts.other_repair_spent(ledger.db, 1, 1, key, source_sig="c" * 64,
                                            source_occurrence=current, body_size=12)
    assert not spent("b" * 64, occurrence)  # same key can resume its finite plan
    assert spent("d" * 64, occurrence)      # a new profile cannot create another right
    changed = {**occurrence, "first_seen": 124.0}
    assert not spent("d" * 64, changed)    # a different raw occurrence remains distinct
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=? WHERE kind=? AND project_id=1",
                          (json.dumps({**meta, "source_occurrence": None}), kind))
    with pytest.raises(ValueError):
        spent("d" * 64, occurrence)


def test_source_normal_fail_repairs_all_units_preserving_original_chunks_and_supported_fact(runtime, source_repair):
    led, _, _ = runtime
    # Stop before any repair POST, retaining exactly the original extraction.
    for _ in range(20):
        result = step(runtime)
        if led.artifacts('patient_context_source_quality_fail_v1', project_id=1):
            break
        assert result['status'] in ('progressed','deferred')
    else:
        pytest.fail('independent source coverage oracle did not find the omitted bed')
    originals = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                     (patient_context_refresh_state.CHUNK,))]
    supported = {fact['id']:fact for row in originals for fact in json.loads(row[1])['facts']}
    assert {fact['text'] for fact in supported.values()} == {_REPAIR_HOME}
    drain(runtime)
    assert len(source_repair['sent']) == 2
    assert [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                (patient_context_refresh_state.CHUNK,))] == originals
    assert len(led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)) == 1
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    scan = patient_context_refresh_sources._current_scans(led.db, 1, snapshot['sources'], snapshot['contract'], cfg=CFG)[1]
    assert scan['accepted_candidate_ref'] and scan['audited']
    facts = list(patient_context_refresh_sources._scan_facts(led.db, 1, snapshot['sources'][0], snapshot['contract'], scan))
    assert {fact['text'] for fact in facts} == {_REPAIR_HOME, _REPAIR_BED}
    for fid, fact in supported.items():
        actual = next(value for value in facts if value['id'] == fid)
        assert patient_context_refresh_state._facts_sig([actual]) == patient_context_refresh_state._facts_sig([fact])
    reserves = led.artifacts('cloud_usage_reserve', project_id=1)
    assert len(reserves) == 2
    assert len({json.loads(row['meta'])['unit_key'] for row in reserves}) == 2
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)


@pytest.mark.parametrize('stop', ['none', 'length', 'capacity'])
@pytest.mark.parametrize('tamper', ['answer', 'missing_proof', 'duplicate_proof', 'strip_provider'])
def test_openai_source_repair_replays_high_response_before_publication(runtime, source_repair, monkeypatch, stop, tamper):
    from contextlib import nullcontext

    led, _, _ = runtime
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode':'on', 'provider':'openai', 'project_ids':[1],
                                           'routes':['patient_context']})
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda cfg: worker.cloud_policy.resolve_settings(cfg, worker.HOME)[0])
    monkeypatch.setattr(worker.cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
    sends = []
    real_client = worker.openai_client.OpenAIClient
    class Client(real_client):
        def request(self, method, body, timeout, **kwargs):
            prompt = json.loads(body['input'])
            sends.append(body)
            items = [{'category':'living','subject':'patient','text':text,'evidence':text,'details':[]}
                     for text in (_REPAIR_HOME, _REPAIR_BED) if text in prompt['source']]
            if stop == 'capacity' and len(sends) == 1:
                items = items[:1] * (context_refresh.context_plan.MAX_SCAN_ITEMS + 1)
            length = stop == 'length' and len(sends) == 1
            return 200, {}, json.dumps({'status':'incomplete' if length else 'completed',
                **({'incomplete_details':{'reason':'max_output_tokens'}} if length else {}),
                'model':self.model, 'service_tier':'default',
                'output':[{'type':'message','role':'assistant','content':[{'type':'output_text',
                    'text':json.dumps({'patient_context':items}, ensure_ascii=False)}]}],
                'usage':{'input_tokens':2,'output_tokens':1,'total_tokens':3,
                         'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}}, ensure_ascii=False).encode()
    monkeypatch.setattr(worker.openai_client, 'OpenAIClient', Client)
    class Initial:
        def __init__(self, _ledger, cfg, **kwargs):
            self.current = kwargs['current']
            self.contract = cloud_inference.cloud_provider_contract(cfg, route='patient_context', purpose='patient_context')
            self.model = self.contract['model']
            self.possibly_sent = False
        def guard(self):
            if not self.current():
                raise cloud_inference.CloudFallback('synthetic_stale')
        def commit_guard(self):
            self.guard()
        def discard(self):
            pass
        def __call__(self, prompt, **kwargs):
            self.guard()
            payload = json.loads(prompt)
            if 'reports' in payload:
                answer = {'fact_ids':[row['id'] for row in payload['reports'][:12]]}
            else:
                items = ([{'category':'living','subject':'patient','text':_REPAIR_HOME,
                           'evidence':_REPAIR_HOME,'details':[]}] if _REPAIR_HOME in payload['source'] else [])
                answer = {'patient_context':items}
            self.possibly_sent = True
            return {'status':200, 'finish_reason':'stop', 'text':json.dumps(answer)}
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', lambda *args, **kwargs:
                        _SOURCE_REPAIR_PROVIDER(*args, **kwargs) if kwargs.get('on_reserved_tx') else Initial(*args, **kwargs))
    drain(runtime)
    assert sends and all(body['model'] == 'gpt-6.1-sol' and body['reasoning'] == {'effort':'high'}
                         and body['max_output_tokens'] == 32768 for body in sends)
    slots = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert slots and all(json.loads(row['meta'])['repair_provider']['model'] == 'gpt-6.1-sol' for row in slots)
    import mcs_view
    for row in slots:
        stored = json.loads(row['content'])
        _meta, visible = mcs_view._artifact_json(row)
        assert 'repair_response' not in stored and '__mcs_internal_proof_v1' not in visible
        if stored['part'] == 0:
            assert '__mcs_internal_proof_v1' in stored
    visible_context = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert visible_context and '__mcs_internal_proof_v1' not in json.dumps(visible_context)
    import notify_views
    card = notify_views._patient_context_list(led.db, 1, 'background', cfg=CFG,
                                               since=None, now=time.time())
    assert '__mcs_internal_proof_v1' not in json.dumps(card)
    header = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)[0]
    if tamper != 'strip_provider':
        first = next(row for row in slots if json.loads(row['content'])['part'] == 0)
        doc = json.loads(first['content'])
        if tamper == 'answer':
            doc['__mcs_internal_proof_v1']['answer_text'] += ' '
        elif tamper == 'missing_proof':
            doc.pop('__mcs_internal_proof_v1')
        else:
            doc['repair_response'] = doc['__mcs_internal_proof_v1']
        with led.db:
            led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(doc), first['artifact_id']))
        changed = [dict(row) for row in led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1)]
        with pytest.raises(ValueError):
            patient_context_refresh_sources._verify_source_repair_rows(led.db, 1, 1, json.loads(header['meta']), changed)
    else:
        with led.db:
            for row in [header, *slots]:
                meta = json.loads(row['meta'])
                meta.pop('repair_provider')
                led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), row['artifact_id']))
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


@pytest.mark.parametrize('failure', ['timeout','crash_after_reserve','save_failure'])
def test_uncertain_source_repair_unit_is_never_resent_on_restart_or_another_day(runtime, source_repair, monkeypatch, failure):
    led, clock, _ = runtime
    if failure == 'timeout':
        source_repair['fault'] = 'timeout'
    elif failure == 'crash_after_reserve':
        reserve = worker.cloud_policy.usage_reserver
        def factory(*args, **kwargs):
            original = reserve(*args, **kwargs)
            def uncertain(body, timeout):
                original(body, timeout)
                raise RuntimeError('synthetic process boundary after durable reserve')
            return uncertain
        monkeypatch.setattr(worker.cloud_policy, 'usage_reserver', factory)
    else:
        monkeypatch.setattr(patient_context_refresh_sources, '_source_repair_update_tx',
            lambda *args: (_ for _ in ()).throw(ValueError('nightly_synthetic_save_failure')))
    for _ in range(25):
        step(runtime)
        if led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1):
            break
    else:
        pytest.fail('source repair never consumed its unit')
    before = len(source_repair['sent'])
    rights = [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
        (patient_context_refresh_state.SOURCE_REPAIR, 'cloud_usage_reserve'))]
    for _ in range(3):
        clock.wall += 86400
        step(runtime)
    assert len(source_repair['sent']) == before
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
        (patient_context_refresh_state.SOURCE_REPAIR, 'cloud_usage_reserve'))] == rights
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


def test_source_repair_sealing_checkpoint_resumes_without_repeating_a_completed_post(runtime, source_repair, monkeypatch):
    led, _, _ = runtime
    original = patient_context_quality_controller._source_repair_seal
    monkeypatch.setattr(patient_context_quality_controller, '_source_repair_seal', lambda *a: ('deferred', None))
    for _ in range(25):
        step(runtime)
        rows = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)
        if rows and json.loads(rows[0]['content'])['phase'] == 'sealing':
            break
    else:
        pytest.fail('completed source range was not durably marked for seal')
    before = len(source_repair['sent'])
    monkeypatch.setattr(patient_context_quality_controller, '_source_repair_seal', original)
    drain(runtime)
    assert len(source_repair['sent']) == before == 2


def test_paid_source_repair_resumes_after_candidate_save_failure_without_a_second_post(runtime, source_repair, monkeypatch):
    led, clock, _ = runtime
    update = patient_context_refresh_sources._source_repair_update_tx
    monkeypatch.setattr(patient_context_refresh_sources, '_source_repair_update_tx',
                        lambda *_args: (_ for _ in ()).throw(ValueError('synthetic save failure')))
    for _ in range(25):
        step(runtime)
        if led.artifacts('cloud_usage_result', project_id=1):
            break
    else:
        pytest.fail('paid repair did not reach its durable receipt')
    assert len(source_repair['sent']) == 1
    reserved = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert len(reserved) == 1 and json.loads(reserved[0]['content'])['phase'] == 'reserved'
    assert not patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    monkeypatch.setattr(patient_context_refresh_sources, '_source_repair_update_tx', update)
    clock.wall += 3600  # Keep the existing failed-save backoff; do not erase its state.
    drain(runtime)
    assert len(source_repair['sent']) == len(led.artifacts('cloud_usage_reserve', project_id=1)) == 2
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)


@pytest.mark.parametrize('tamper', ['duplicate_slot','duplicate_header','receipt','new_revision','scope','key','budget'])
def test_source_repair_rejects_changed_evidence_before_adoption(runtime, source_repair, monkeypatch, tamper):
    led, clock, _ = runtime
    original = patient_context_quality_controller._source_repair_seal
    monkeypatch.setattr(patient_context_quality_controller, '_source_repair_seal', lambda *a: ('deferred', None))
    for _ in range(25):
        step(runtime)
        if len(source_repair['sent']) == 2:
            break
    else:
        pytest.fail('two source units were not completed')
    if tamper == 'key':
        monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *a, **k: None)
    elif tamper == 'budget':
        monkeypatch.setattr(worker, 'load_config', lambda: {**CFG, 'semantic':{**CFG['semantic'], 'daily_request_budget':0}})
        monkeypatch.setattr(extract_llm_context, 'load_config', worker.load_config)
        monkeypatch.setattr(extract_llm_prepare, 'load_config', worker.load_config)
        monkeypatch.setattr(extract_llm_provider, 'load_config', worker.load_config)
        monkeypatch.setattr(extract_llm_qc, 'load_config', worker.load_config)
        monkeypatch.setattr(extract_llm_resident, 'load_config', worker.load_config)
        monkeypatch.setattr(extract_llm_runner, 'load_config', worker.load_config)
        monkeypatch.setattr(extract_llm_selection, 'load_config', worker.load_config)
        monkeypatch.setattr(extract_llm_store, 'load_config', worker.load_config)
        monkeypatch.setattr(cloud_inference, 'load_config', lambda: {**CFG, 'semantic':{**CFG['semantic'], 'daily_request_budget':0}})
    with led.db:
        if tamper in ('duplicate_slot','duplicate_header'):
            led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                           'SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts WHERE kind=? LIMIT 1',
                           (patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE if tamper == 'duplicate_slot' else patient_context_refresh_state.SOURCE_REPAIR,))
        elif tamper == 'receipt':
            led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.request_fp',?) WHERE kind='cloud_usage_result'", ('f' * 64,))
        elif tamper == 'new_revision':
            revision = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
            row = led.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()
            led.db.execute("INSERT INTO message_revisions VALUES(?,?,?,?,?,?)", (1, revision + 1, row[0], row[0], 'full', clock.wall))
        elif tamper == 'scope':
            led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
    monkeypatch.setattr(patient_context_quality_controller, '_source_repair_seal', original)
    for _ in range(3):
        clock.wall += 61
        step(runtime)
    assert not any(json.loads(row['content']).get('accepted_candidate_ref') for row in led.artifacts(patient_context_refresh_state.SCAN, project_id=1))
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None


def test_source_repair_never_reinjects_rejected_seed_and_preserves_local_union(runtime, monkeypatch):
    led, clock, _ = runtime
    other = '合成の申し送りです。'
    seed(led, body=_REPAIR_HOME + other + _REPAIR_BED)
    source = list(mcs_queries.patient_context_sources(led.db, 1))[0]
    def record(text):
        return {'category':'living','subject':'patient','text':text,'evidence':text,'details':[]}
    source['items'] = [record(other)]
    protected = context_plan.source_facts(source, [record(_REPAIR_HOME)], night_id='synthetic', discovery='new_report', found_at=patient_context_refresh_state.time.time())
    asked = []
    def answer(*args, **kwargs):
        asked.append(True)
        return {'patient_context':[record(_REPAIR_BED)]}
    monkeypatch.setattr(patient_context_quality_controller, '_ask', answer)
    repaired = patient_context_quality_controller._scan(CFG, source, {}, 'synthetic', clock.mono + 600, lambda: True,
        source_repair={'codes':['missing'],'protected':protected})
    assert {fact['text'] for fact in repaired['facts']} == {_REPAIR_HOME, _REPAIR_BED}
    assert patient_context_refresh_state._facts_sig([next(f for f in repaired['facts'] if f['id'] == protected[0]['id'])]) == patient_context_refresh_state._facts_sig(protected)
    local = patient_context_quality_controller._scan(CFG, source, {}, 'synthetic', clock.mono + 600, lambda: True)
    assert {fact['text'] for fact in local['facts']} == {other, _REPAIR_BED}
    assert len(asked) == 2


@pytest.mark.parametrize('choice,returned', [('supports', True), ('not_supported', True), ('not_supported', False)])
def test_historical_karte_repair_retains_original_question_and_answer(runtime, cloud, monkeypatch, choice, returned):
    import semantic_audit
    import semantic_policy
    import semantic_runtime
    led, clock, _ = runtime
    seed(led, body='')
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=17 WHERE project_id=1')
    led.karte_summary_store(1, 17, {'comment': _REPAIR_HOME, 'updated_at': '2026-10-16T09:00:00+09:00'})
    source = next(item for item in mcs_queries.patient_context_sources(led.db, 1) if item['source'] == 'karte_summary')
    assert not source['historical']
    record = {'category': 'living', 'subject': 'patient', 'text': _REPAIR_HOME, 'evidence': _REPAIR_HOME, 'details': []}
    facts = context_plan.source_facts(source, [record], night_id='synthetic', discovery='new_report', found_at=patient_context_refresh_state.time.time())
    assert len(facts) == 1 and facts[0]['historical'] is False
    contract = patient_context_refresh_state._contract(CFG, 1)
    occurrence = patient_context_refresh_sources._source_occurrence(led.db, 1, source)
    meta = {'source_key': context_plan.source_key(source), 'source_sig': context_plan.source_signature(source),
            'contract': contract, 'provider': contract['provider'], 'model': contract['provider']['model'],
            'source_occurrence_version': 1, 'source_occurrence': occurrence,
            'source_binding': occurrence['source_binding'], 'unit_key': '1' * 64, 'chunk_key': '2' * 64}
    checkpoint = {'complete': True, 'audited': False, 'cursor': 1, 'chunks': 1,
                  'source_occurrence_version': 1, 'source_occurrence': occurrence}
    with led.db:
        patient_context_refresh_state._replace(led, patient_context_refresh_state.CHUNK, 1,
            {'chunk_span': [0, len(source['body_text'])], 'part': 0, 'parts': 1, 'facts': facts}, meta, 0)
        patient_context_refresh_state._replace(led, patient_context_refresh_state.SCAN, 1, checkpoint, meta, 0)
    calls = []
    def judge(body, timeout):
        calls.append(body)
        answers = {}
        for qid, question in body['questions'].items():
            options = question['criteria']
            selected = choice if 'supports' in options else 'complete'
            answers[qid] = {'type': 'choice', 'choice': selected, 'confidence': 1.0,
                           'probabilities': {option: float(option == selected) for option in options}}
        return 200, {}, json.dumps({'model': _source_repair_jev.JEV_MODEL, 'answers': answers,
                                   'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
    monkeypatch.setattr(_source_repair_jev, 'JevClient',
                        lambda **kwargs: _SOURCE_REPAIR_JEV(post_fn=judge, **kwargs))
    assert patient_context_quality_controller._quality_step(led, 1, CFG, 'synthetic', {}, clock.mono + 600,
                                         lambda: True, source=source)[0] == 'pending'
    assessment = patient_context_refresh_state._quality_assessment(semantic_policy.runtime_config(CFG)[0])
    led.db.execute('BEGIN')
    try:
        before = patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, meta['source_key'], assessment)
    finally:
        led.db.rollback()
    fid = facts[0]['id']
    original = semantic_runtime.patient_source_question_evidence(led.db, 1, before['identity'], [fid])
    assert original['answers'][fid]['choice'] == choice and original['references'][fid]
    retained = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
        'WHERE kind IN (?,?) ORDER BY artifact_id', ('semantic_usage', 'patient_context_source_quality_v1'))]
    led.karte_summary_store(1, 17, {'comment': '本人は合成の住居へ転居しました。',
                                  'updated_at': '2026-10-17T09:00:00+09:00'})
    historical = next(mcs_queries.patient_context_sources(led.db, 1, mid=meta['source_key']))
    assert historical['historical']
    led.db.execute('BEGIN')
    try:
        after = patient_context_refresh_sources._source_quality_capture(led.db, 1, CFG, meta['source_key'], assessment)
    finally:
        led.db.rollback()
    assert after['identity']['source_input_key'] == before['identity']['source_input_key']
    asked = []
    def response(_cfg, payload, *_args, **_kwargs):
        asked.append(json.loads(json.dumps(payload)))
        return {'patient_context': [record] if returned else []}
    monkeypatch.setattr(patient_context_quality_controller, '_ask', response)
    repaired = patient_context_quality_controller._scan(CFG, historical, {}, 'synthetic', clock.mono + 600, lambda: True,
        source_repair={'codes': ['not_supported'], 'protected': facts if choice == 'supports' else [],
                      'historical_by_id': {fact['id']: {'historical': fact['historical']} for fact in before['facts']}})
    assert len(asked) == 1 and asked[0]['already_found'] == [] and 'historical_by_id' not in asked[0]
    if returned:
        assert len(repaired['facts']) == 1 and repaired['facts'][0]['historical'] is False
        pages = list(semantic_audit.patient_source_quality_pages(historical, repaired['facts'],
            source_input_key=before['identity']['source_input_key'], assessment=assessment))
        assert pages[0]['question_keys'][fid] == before['identity']['question_keys'][fid]
        reused = semantic_runtime.patient_source_question_evidence(led.db, 1, after['identity'], [fid])
        assert reused == original and len(calls) == 1
        if choice == 'supports':
            assert repaired['facts'][0] is facts[0]
    else:
        assert repaired['facts'] == []  # The historical map never reinjects a refused fact.
    assert [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts '
        'WHERE kind IN (?,?) ORDER BY artifact_id', ('semantic_usage', 'patient_context_source_quality_v1'))] == retained


def test_source_repair_forbids_the_scan_helpers_hidden_second_question(runtime, monkeypatch):
    led, clock, _ = runtime
    seed(led, body=_REPAIR_HOME)
    source = list(mcs_queries.patient_context_sources(led.db, 1))[0]
    asked = []
    def invalid(*args, **kwargs):
        asked.append(True)
        return {'patient_context':[{'category':'living','subject':'patient','text':'not in source','evidence':'not in source','details':[]}]}
    monkeypatch.setattr(patient_context_quality_controller, '_ask', invalid)
    with pytest.raises(ValueError, match='nightly_unsupported_items'):
        patient_context_quality_controller._scan(CFG, source, {}, 'synthetic', clock.mono + 600, lambda: True,
            source_repair={'codes':['missing'],'protected':[]})
    assert asked == [True]


def test_unchanged_missing_answer_is_not_rerolled_for_a_staged_source_candidate(runtime, source_repair):
    led, clock, _ = runtime
    source_repair['fault'] = 'omit_bed'
    for _ in range(25):
        step(runtime)
        if len(source_repair['sent']) == 2:
            break
    coverage_requests = sum(request['state']['target']['id'] == 'source_coverage'
                            for request in led.synthetic_jev_requests)
    for _ in range(5):
        clock.wall += 86400
        step(runtime)
    assert len(source_repair['sent']) == 2
    assert sum(request['state']['target']['id'] == 'source_coverage'
               for request in led.synthetic_jev_requests) == coverage_requests == 1
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('changed_row', ['source','other'])
def test_unexpected_trigger_write_cannot_be_blessed_as_a_source_repair_owner_write(runtime, source_repair, changed_row):
    led, clock, _ = runtime
    before_body = led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0]
    led.db.execute('CREATE TEMP TABLE synthetic_side_effect(value INTEGER)')
    led.db.execute('INSERT INTO synthetic_side_effect VALUES(0)')
    change = ("UPDATE messages SET body_text=body_text || '変更' WHERE message_id=1;" if changed_row == 'source'
              else 'UPDATE synthetic_side_effect SET value=value+1;')
    # TEMP keeps this entirely synthetic while allowing the trigger to see
    # the test-only side-effect table on the same connection.
    led.db.execute('CREATE TEMP TRIGGER synthetic_unexpected_repair_write AFTER UPDATE OF content ON artifacts '
        "WHEN OLD.kind='patient_context_source_quality_repair_candidate_v1' "
        "AND json_extract(NEW.content,'$.phase')='complete' BEGIN " + change + ' END')
    led.db.commit()
    for _ in range(25):
        step(runtime)
        if source_repair['sent']:
            break
    for _ in range(3):
        clock.wall += 61
        step(runtime)
    assert len(source_repair['sent']) == 1
    assert led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0] == before_body
    assert led.db.execute('SELECT value FROM synthetic_side_effect').fetchone()[0] == 0
    slots = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert slots and all(json.loads(row['content'])['phase'] == 'reserved' for row in slots)
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('changed_row', ['source', 'scope', 'other', 'scan', 'progress', 'epoch'])
def test_unexpected_write_during_source_adoption_rolls_back_and_retains_stage(runtime, source_repair, monkeypatch, changed_row):
    led, _, _ = runtime
    with led.db:
        led.db.execute("INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,next_try,created_at,updated_at) "
                       "VALUES('semantic_sched',0,0,'{\"analysis_epoch\":7}','done',0,1,1)")
    led.db.execute('CREATE TEMP TABLE source_adoption_sentinel(value INTEGER)')
    led.db.execute('INSERT INTO source_adoption_sentinel VALUES(0)')
    led.db.commit()
    condition = ("NEW.kind='patient_context_scan_v1' AND "
                 "json_type(NEW.content,'$.accepted_candidate_ref')='object'")
    table, event = 'artifacts', 'INSERT'
    if changed_row == 'source':
        action = "UPDATE messages SET body_text='synthetic changed during adoption' WHERE message_id=1;"
    elif changed_row == 'scope':
        action = 'UPDATE patients SET karte_id=99 WHERE project_id=1;'
    elif changed_row == 'other':
        action = 'UPDATE source_adoption_sentinel SET value=value+1;'
    elif changed_row == 'scan':
        action = "UPDATE artifacts SET content=json_set(content,'$.audited',json('false')) WHERE artifact_id=NEW.artifact_id;"
    elif changed_row == 'progress':
        condition = (f"NEW.kind='{patient_context_refresh_state.PROGRESS}' AND EXISTS(SELECT 1 FROM artifacts WHERE "
                     "kind='patient_context_scan_v1' AND project_id=1 AND "
                     "json_type(content,'$.accepted_candidate_ref')='object')")
        action = "UPDATE artifacts SET content=json_set(content,'$.summary_pending',json('false')) WHERE artifact_id=NEW.artifact_id;"
    else:
        table, event = 'fetch_jobs', 'UPDATE'
        condition = ("NEW.kind='semantic_sched' AND json_extract(NEW.payload,'$.analysis_epoch')<1000 "
                     "AND EXISTS(SELECT 1 FROM artifacts WHERE kind='patient_context_scan_v1' AND project_id=1 "
                     "AND json_type(content,'$.accepted_candidate_ref')='object')")
        action = "UPDATE fetch_jobs SET payload=json_set(payload,'$.analysis_epoch',1000) WHERE job_id=NEW.job_id;"
    led.db.executescript('CREATE TEMP TRIGGER synthetic_source_adoption AFTER ' + event + ' ON ' + table
                        + ' WHEN ' + condition + ' BEGIN ' + action + ' END;')
    original_replace = patient_context_refresh_state._replace
    observed = []
    def observe(owner, kind, pid, content, meta, mid=None, **kwargs):
        if kind == patient_context_refresh_state.SCAN and content.get('accepted_candidate_ref') is not None:
            assert owner.db.in_transaction
            observed.append({
                'body': owner.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0],
                'scope': tuple(owner.db.execute('SELECT project_type,karte_id,is_archived FROM patients WHERE project_id=1').fetchone()),
                'scan': [tuple(row) for row in owner.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (patient_context_refresh_state.SCAN,))],
                'progress': [tuple(row) for row in owner.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (patient_context_refresh_state.PROGRESS,))],
                'epoch': tuple(owner.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()),
                'stage': [tuple(row) for row in owner.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
                                                               (patient_context_refresh_state.SOURCE_REPAIR, patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE))]})
        return original_replace(owner, kind, pid, content, meta, mid, **kwargs)
    monkeypatch.setattr(patient_context_refresh_state, '_replace', observe)
    for _ in range(35):
        result = step(runtime)
        if observed:
            break
    assert observed and result['status'] == 'source_changed' and result['done'] == 0
    before = observed[0]
    assert led.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0] == before['body']
    assert tuple(led.db.execute('SELECT project_type,karte_id,is_archived FROM patients WHERE project_id=1').fetchone()) == before['scope']
    assert led.db.execute('SELECT value FROM source_adoption_sentinel').fetchone()[0] == 0
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (patient_context_refresh_state.SCAN,))] == before['scan']
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind=? AND project_id=1', (patient_context_refresh_state.PROGRESS,))] == before['progress']
    assert tuple(led.db.execute("SELECT job_id,payload FROM fetch_jobs WHERE kind='semantic_sched'").fetchone()) == before['epoch']
    assert [tuple(row) for row in led.db.execute('SELECT * FROM artifacts WHERE kind IN (?,?) ORDER BY artifact_id',
                                               (patient_context_refresh_state.SOURCE_REPAIR, patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE))] == before['stage']
    assert len(source_repair['sent']) == 2 and not led.db.in_transaction
    assert not any(json.loads(row['content']).get('accepted_candidate_ref') for row in led.artifacts(patient_context_refresh_state.SCAN, project_id=1))
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('changed', ['claim', 'revision', 'key', 'scope'])
def test_source_repair_losing_its_claim_after_the_budget_reservation_cannot_post(runtime, source_repair, monkeypatch, changed):
    led, clock, _ = runtime
    reserve = worker.cloud_policy.usage_reserver
    def factory(*args, **kwargs):
        original = reserve(*args, **kwargs)
        def changed_after_commit(body, timeout):
            reservation = original(body, timeout)
            assert not led.db.in_transaction
            if changed == 'key':
                monkeypatch.setattr(worker.cloud_policy, 'api_key', lambda *a, **k: None)
            with led.db:
                if changed == 'claim':
                    led.db.execute("UPDATE fetch_jobs SET payload=json_set(payload,'$.token','synthetic-other-owner') WHERE kind=?",
                                   (patient_context_refresh_state.QUALITY_CLAIM,))
                elif changed == 'revision':
                    seq = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
                    digest = led.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
                    led.db.execute('INSERT INTO message_revisions VALUES(?,?,?,?,?,?)',
                                   (1, seq + 1, digest, digest, 'full', clock.wall))
                elif changed == 'scope':
                    led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
            return reservation
        return changed_after_commit
    monkeypatch.setattr(worker.cloud_policy, 'usage_reserver', factory)
    for _ in range(25):
        step(runtime)
        if led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1):
            break
    assert not source_repair['sent']
    headers = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)
    slots = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1)
    reserves = led.artifacts('cloud_usage_reserve', project_id=1)
    results = led.artifacts('cloud_usage_result', project_id=1)
    assert len(headers) == len(slots) == len(reserves) == len(results) == 1
    slot = json.loads(slots[0]['meta'])
    reservation, result = json.loads(reserves[0]['meta']), json.loads(results[0]['meta'])
    assert json.loads(slots[0]['content'])['phase'] == 'reserved'
    assert slot['reservation_artifact_id'] == reserves[0]['artifact_id']
    assert slot['reservation_id'] == reservation['reservation_id'] == result['reservation_id']
    assert result['http_class'] == 'not_sent' and result['cost_usd'] == '0'
    assert worker.cloud_policy.usage_report(led)['unresolved_requests'] == 0
    original = [(item['artifact_id'], item['content'], item['meta']) for item in (headers[0], slots[0], reserves[0])]
    # The original input keeps its spent right and unresolved unit slot. A real
    # new revision may start B separately; it cannot redispatch this old unit.
    for _ in range(2):
        clock.wall += 61
        step(runtime)
    assert [tuple(led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE artifact_id=?', (aid,)).fetchone())
            for aid, _content, _meta in original] == original
    assert led.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='cloud_usage_reserve' "
                          "AND json_extract(meta,'$.owner.repair_input_key')=?",
                          (slot['repair_input_key'],)).fetchone()[0] == 1
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('stop', ['length_once','capacity_once','thinking_length_once'])
def test_source_repair_persists_a_known_stop_before_continuing_finite_children(runtime, source_repair, stop):
    led, clock, _ = runtime
    source_repair['fault'] = stop
    for _ in range(25):
        step(runtime)
        if source_repair['sent']:
            break
    slots = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR_CANDIDATE, project_id=1)
    assert len(slots) == 1 and json.loads(slots[0]['content'])['phase'] == 'split'
    original_unit = json.loads(slots[0]['meta'])['unit_key']
    header = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)[0]
    assert json.loads(header['content'])['split'] == 1
    clock.wall += 86400
    drain(runtime)
    reservations = [json.loads(row['meta']) for row in led.artifacts('cloud_usage_reserve', project_id=1)]
    assert len(reservations) == len(source_repair['sent'])
    assert len({row['unit_key'] for row in reservations}) == len(reservations)
    assert sum(row['unit_key'] == original_unit for row in reservations) == 1
    results = led.artifacts('cloud_usage_result', project_id=1)
    assert len(results) == len(reservations)
    assert all(json.loads(row['meta'])['error_kind'] is None for row in results)
    assert len(led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)) == 1
    assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('stage', ['summary', 'repair'])
def test_thinking_only_length_holds_summary_and_consumes_no_second_repair(runtime, cloud, monkeypatch, stage):
    led, clock, _ = runtime
    facts = [{'id':fid,'category':'living','text':f'完全合成の居住記録{fid}です。',
              'source':'chat','project_id':1,'message_id':1,'posted_at':None,
              'posted_at_ts':1,'subject':'patient'} for fid in ('a','b')]
    seed(led, body=''.join(fact['text'] for fact in facts))
    calls, results = [], []

    def post(body, timeout):
        assert not led.db.in_transaction
        calls.append(body)
        return 200, {}, json.dumps({'model':worker.anthropic_client.MODEL,'stop_reason':'max_tokens',
            'content':[{'type':'thinking','thinking':'synthetic-hidden'}],
            'usage':{'input_tokens':10,'output_tokens':10}}).encode()

    client = _SOURCE_REPAIR_CLIENT('synthetic', post_fn=post)

    def length_only(prompt, *, deadline, max_tokens):
        return client.complete(prompt, deadline=deadline, max_tokens=max_tokens,
            after_result=lambda reservation, meta, **_answer:results.append(meta))

    deadline = clock.mono + 600
    if stage == 'summary':
        with pytest.raises(ValueError, match='nightly_length_stop'):
            patient_context_quality_controller._summary(CFG, facts, deadline, lambda:True, provider=length_only)
    else:
        # Seed the negative assessment/owner boundary; retain the real one-use repair reservation.
        from contextlib import nullcontext
        monkeypatch.setattr(patient_context_quality_receipts, '_quality_owns', lambda *args:True)
        monkeypatch.setattr(patient_context_quality_receipts, '_quality_failed', lambda *args:True)
        monkeypatch.setattr(patient_context_quality_receipts, '_quality_answers', lambda *args:{
            'a':{'choice':'supports','confidence':1},'b':{'choice':'important_missing','confidence':1}})
        monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
        monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
        monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)
        monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_: nullcontext(True))
        monkeypatch.setattr(worker.anthropic_client.AnthropicClient, 'request',
                            lambda _self, method, body, timeout, **kwargs: post(body, timeout))
        monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', _SOURCE_REPAIR_PROVIDER)
        identity = {'fact_ids':['a','b'],'input_key':'1'*64,'audit_key':'2'*64,
                    'candidate_key':'3'*64,'failure_key':'4'*64}
        doc = {'summary':[{'text':facts[0]['text'],'fact_ids':['a']}]}
        args = (led, 1, CFG, 'synthetic-night', doc, identity, 'synthetic-claim',
                lambda *args:True, deadline, lambda:True, facts, {}, .9, {'groups':[],'relations':[]})
        assert patient_context_quality_controller._quality_repair_candidate(*args) == ('held', None)
        assert len(led.artifacts(patient_context_refresh_state.QUALITY_REPAIR, project_id=1)) == 1
        results.extend(json.loads(row['meta']) for row in led.artifacts('cloud_usage_result', project_id=1))
        assert patient_context_quality_controller._quality_repair_candidate(*args) == ('held', None)
        assert not led.artifacts(patient_context_refresh_state.QUALITY_REPAIR_CANDIDATE, project_id=1)
    assert len(calls) == len(results) == 1 and results[0]['error_kind'] is None
    assert results[0]['usage']['output_tokens'] == 10 and 'synthetic-hidden' not in json.dumps(results)
    assert document(led) is None and not led.artifacts(patient_context_refresh_state.QUALITY_PASS, project_id=1)


@pytest.mark.parametrize('changed', ['revision','first_seen','scope'])
def test_new_raw_occurrence_restarts_b_without_deleting_prior_original_chunks(runtime, cloud, changed):
    led, clock, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    for generation in (2, 3):
        old = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                   (patient_context_refresh_state.CHUNK,))]
        with led.db:
            if changed == 'revision':
                led.db.execute('BEGIN IMMEDIATE')
                before = context_refresh.dependency_loss.capture_tx(led.db, 1, 1)
                context_refresh.dependency_loss.record_tx(led.db, before)
                previous = led.db.execute('SELECT COALESCE(MAX(seq),1) FROM message_revisions WHERE message_id=1').fetchone()[0]
                body_hash = led.db.execute('SELECT content_hash FROM messages WHERE message_id=1').fetchone()[0]
                led.db.execute('INSERT INTO message_revisions VALUES(?,?,?,?,?,?)',
                               (1, previous + 1, body_hash, body_hash, 'full', clock.wall))
            elif changed == 'first_seen':
                led.db.execute('UPDATE messages SET first_seen=first_seen+1 WHERE message_id=1')
            else:
                led.db.execute('UPDATE patients SET karte_id=? WHERE project_id=1', (generation,))
        assert patient_context_refresh_readiness._pending(led.db, 1, CFG)[2]['B']
        assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
        if changed == 'revision':
            assert step(runtime)['reason'] == 'patient_context_waiting'
            assert [tuple(row) for row in led.db.execute(
                'SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                (patient_context_refresh_state.CHUNK,))] == old
            return
        drain(runtime)
        current = {row[0]:tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=?',
                                                             (patient_context_refresh_state.CHUNK,))}
        assert all(current[row[0]] == row for row in old)
        assert len(current) == len(old) + 1
        assert patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']
    keys = [json.loads(row['meta'])['unit_key'] for row in led.artifacts(patient_context_refresh_state.CHUNK, project_id=1)]
    assert len(keys) == len(set(keys)) == 3


def test_scope_aba_holds_an_unproven_original_cursor_without_reposting_or_overwriting(runtime, cloud):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=99 WHERE project_id=1')
    drain(runtime)
    originals = [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                     (patient_context_refresh_state.CHUNK,))]
    calls = len(cloud[0])
    with led.db:
        led.db.execute('UPDATE patients SET karte_id=NULL WHERE project_id=1')
    step(runtime)
    assert len(cloud[0]) == calls
    assert [tuple(row) for row in led.db.execute('SELECT artifact_id,content,meta FROM artifacts WHERE kind=? ORDER BY artifact_id',
                                                (patient_context_refresh_state.CHUNK,))] == originals
    checkpoint = led.artifacts(patient_context_refresh_state.SCAN, project_id=1)[-1]
    stored = json.loads(checkpoint['content'])
    assert stored['held'] and stored['failure'] == 'nightly_original_unit_unverified'
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('field', ['invalidated','source_key'])
def test_new_occurrence_marker_never_uses_duplicate_metadata_as_legacy(runtime, cloud, field):
    led, _, _ = runtime
    seed(led, body='見出しのない完全合成の居住記録。')
    drain(runtime)
    original = led.db.execute('SELECT artifact_id,meta FROM artifacts WHERE kind=?', (patient_context_refresh_state.CHUNK,)).fetchone()
    value = '0' if field == 'invalidated' else '1'
    malformed = '{"' + field + '":' + value + ',' + original['meta'][1:-1] + ',"' + field + '":' + value + '}'
    with led.db:
        led.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (malformed, original['artifact_id']))
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    assert not patient_context_refresh_readiness.inference_coverage(led.db, 1, CFG)['complete']


@pytest.mark.parametrize('save_interrupted', [False, True])
def test_openai_summary_high_repair_is_audited_published_and_rechecked(runtime, cloud, monkeypatch, tmp_path, save_interrupted):
    """A paid repair remains bound to its response through publication and later reads."""
    from contextlib import nullcontext

    led, _, _ = runtime
    monkeypatch.setitem(CFG, 'cloud_llm', {'mode': 'on', 'provider': 'openai', 'project_ids': [1],
                                           'routes': ['patient_context']})
    monkeypatch.setattr(worker, 'HOME', str(tmp_path))
    monkeypatch.setattr(extract_llm_engine, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_prepare, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_provider, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_resident, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_selection, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_source_contract, 'HOME', worker.HOME)
    monkeypatch.setattr(extract_llm_store, 'HOME', worker.HOME)
    monkeypatch.setattr(cloud_inference, 'resolve_cloud_settings', lambda cfg: worker.cloud_policy.resolve_settings(cfg, worker.HOME)[0])
    monkeypatch.setattr(worker.cloud_policy, 'provider_key', lambda *_args: 'synthetic-key')
    monkeypatch.setattr(worker.cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    monkeypatch.setattr(worker.cloud_backlog, 'request_slot', lambda *_args: nullcontext(True))
    monkeypatch.setattr(worker.cloud_backlog, 'owner_alive', lambda: True)

    class Initial:
        def __init__(self, _ledger, cfg, **kwargs):
            self.current = kwargs['current']
            self.contract = cloud_inference.cloud_provider_contract(cfg, route='patient_context', purpose='patient_context')
            self.model = self.contract['model']
            self.possibly_sent = False

        def guard(self):
            if not self.current():
                raise cloud_inference.CloudFallback('synthetic_stale')

        def commit_guard(self):
            self.guard()

        def discard(self):
            pass

        def __call__(self, prompt, **kwargs):
            self.guard()
            payload = json.loads(prompt)
            if 'reports' in payload:
                assert len(payload['reports']) == 2
                answer = {'fact_ids': [payload['reports'][0]['id']]}
            else:
                answer = {'patient_context': [
                    {'category': 'living', 'subject': 'patient', 'text': text,
                     'evidence': text, 'details': []}
                    for text in (_REPAIR_HOME, _REPAIR_BED) if text in payload['source']]}
            self.possibly_sent = True
            return {'status': 200, 'finish_reason': 'stop', 'text': json.dumps(answer)}

    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', lambda *args, **kwargs:
                        _SOURCE_REPAIR_PROVIDER(*args, **kwargs)
                        if kwargs.get('purpose') == 'quality_repair' else Initial(*args, **kwargs))
    sends = []
    real_client = worker.openai_client.OpenAIClient

    class Client(real_client):
        def request(self, method, body, timeout, **kwargs):
            assert timeout > 0 and not led.db.in_transaction
            prompt = json.loads(body['input'])
            assert prompt['previous'] and prompt['findings'] and len(prompt['reports']) == 2
            sends.append(body)
            answer = json.dumps({'fact_ids': [row['id'] for row in prompt['reports']]})
            return 200, {}, json.dumps({'status': 'completed', 'model': self.model, 'service_tier': 'default',
                'output': [{'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': answer}]}],
                'usage': {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3,
                          'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}}).encode()

    monkeypatch.setattr(worker.openai_client, 'OpenAIClient', Client)

    def judge(body, timeout):
        assert timeout > 0 and not led.db.in_transaction
        led.synthetic_jev_requests.append(body)
        answers = {}
        for qid, question in body['questions'].items():
            options = question['criteria']
            choice = ('supports' if 'supports' in options else
                      'complete' if 'complete' in options else 'important_missing')
            assert choice in options
            answers[qid] = {'type': 'choice', 'choice': choice, 'confidence': 1.0,
                            'probabilities': {option: float(option == choice) for option in options}}
        return 200, {}, json.dumps({'model': _source_repair_jev.JEV_MODEL, 'answers': answers,
                                   'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()

    monkeypatch.setattr(_source_repair_jev, 'JevClient', lambda **kwargs: _SOURCE_REPAIR_JEV(post_fn=judge, **kwargs))
    seed(led, body=_REPAIR_HOME + _REPAIR_BED)
    if save_interrupted:
        prove = patient_context_quality_receipts._new_summary_repair_response
        monkeypatch.setattr(patient_context_quality_receipts, '_new_summary_repair_response',
                            lambda *_args: (_ for _ in ()).throw(ValueError('synthetic save failure')))
        for _ in range(25):
            step(runtime)
            if led.artifacts('cloud_usage_result', project_id=1):
                break
        else:
            pytest.fail('paid summary repair did not reach its durable receipt')
        assert len(sends) == 1 and not led.artifacts(patient_context_refresh_state.QUALITY_REPAIR_CANDIDATE, project_id=1)
        monkeypatch.setattr(patient_context_quality_receipts, '_new_summary_repair_response', prove)
        runtime[1].wall += 3600
    drain(runtime)
    assert len(sends) == 1
    assert sends[0]['model'] == 'gpt-6.1-sol' and sends[0]['reasoning'] == {'effort': 'high'}
    assert sends[0]['max_output_tokens'] == 32768
    assert led.artifacts(patient_context_refresh_state.QUALITY_FAIL, project_id=1)
    assert led.artifacts(patient_context_refresh_state.QUALITY_PASS, project_id=1)
    candidates = led.artifacts(patient_context_refresh_state.QUALITY_REPAIR_CANDIDATE, project_id=1)
    assert len(candidates) == 1
    candidate = candidates[0]
    content, candidate_meta = json.loads(candidate['content']), json.loads(candidate['meta'])
    assert candidate_meta['repair_version'] == 1 and len(content['fact_ids']) == 2
    assert '__mcs_internal_proof_v1' in content and 'repair_response' not in content
    import mcs_view
    _meta, visible = mcs_view._artifact_json(candidate)
    assert '__mcs_internal_proof_v1' not in visible
    doc, meta = patient_context_refresh_state._latest(led.db, patient_context_refresh_state.KIND, 1)
    assert [entry['fact_ids'][0] for entry in doc['summary']] == content['fact_ids']
    assert meta['quality_repair_ref']['artifact_id'] == candidate['artifact_id']
    visible_context = patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    assert visible_context and '__mcs_internal_proof_v1' not in json.dumps(visible_context)
    import notify_views
    card = notify_views._patient_context_list(led.db, 1, 'background', cfg=CFG,
                                               since=None, now=time.time())
    assert '__mcs_internal_proof_v1' not in json.dumps(card)
    step(runtime)
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) and len(sends) == 1

    content['__mcs_internal_proof_v1']['answer_text'] += ' '
    with led.db:
        led.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                       (json.dumps(content), candidate['artifact_id']))
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG) is None
    assert len(sends) == 1
